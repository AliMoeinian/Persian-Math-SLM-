"""OpenRouter transport with bounded infrastructure retries and per-attempt evidence."""

import http.client
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .config import Config

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_safe(value: Any) -> Any:
    """Nonfinite provider numbers are unknown rather than invalid persisted JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def response_metadata(data: Any) -> dict[str, Any]:
    """Preserve response evidence only, never request headers or authentication."""
    if not isinstance(data, dict):
        return {"usage": {}}
    metadata = {k: data[k] for k in ("id", "model", "provider", "choices") if k in data}
    metadata["usage"] = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return dict(json_safe(metadata))


@dataclass
class Completion:
    content: str
    metadata: dict[str, Any]


class APIError(RuntimeError):
    def __init__(
        self, category: str, attempts: int, elapsed: float, metadata: dict[str, Any] | None = None
    ) -> None:
        super().__init__(category)
        self.category, self.attempts, self.elapsed = category, attempts, elapsed
        self.metadata = metadata or {}


class OpenRouterClient:
    def __init__(
        self,
        config: Config,
        *,
        api_key: str | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.config = config
        self.key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.key:
            raise ValueError("Set OPENROUTER_API_KEY securely in environment settings")
        self.opener, self.sleep, self.clock = opener, sleep, clock

    def complete(self, messages: list[dict[str, str]]) -> Completion:
        config = self.config
        payload = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "provider": {
                "only": [config.provider],
                "allow_fallbacks": False,
                "require_parameters": True,
            },
            "usage": {"include": True},
            "stream": False,
        }
        request = urllib.request.Request(
            ENDPOINT,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        started_at, start = utc_now(), self.clock()
        records: list[dict[str, Any]] = []
        for attempt in range(1, config.max_attempts + 1):
            attempt_started_at, attempt_start = utc_now(), self.clock()
            metadata: dict[str, Any] = {"usage": {}}
            category: str | None = None
            status: int | None = None
            retry_after: float | None = None
            content: str | None = None
            finish_reason = None
            body = b""
            try:
                with self.opener(request, timeout=config.timeout_seconds) as response:
                    status = getattr(response, "status", 200)
                    body = response.read()
            except urllib.error.HTTPError as exc:
                status, category = exc.code, f"http_{exc.code}"
                header = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    value = float(header) if header else None
                    if value is not None and math.isfinite(value):
                        retry_after = min(60.0, max(0.0, value))
                except ValueError:
                    pass
                try:
                    body = exc.read()
                except (OSError, ValueError, http.client.HTTPException):
                    pass
                finally:
                    exc.close()
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
                category = "transport_error"
            response_end = self.clock()
            # JSON decoding/validation is local processing, not network response time.
            if body:
                try:
                    data = json.loads(body)
                    metadata = response_metadata(data)
                    choices = data.get("choices") if isinstance(data, dict) else None
                    if category is None:
                        if (
                            not isinstance(data, dict)
                            or data.get("error")
                            or not isinstance(choices, list)
                            or not choices
                            or not isinstance(choices[0], dict)
                        ):
                            category = "invalid_response"
                        else:
                            choice = choices[0]
                            finish_reason = choice.get("finish_reason")
                            message = choice.get("message")
                            candidate = (
                                message.get("content") if isinstance(message, dict) else None
                            )
                            if not isinstance(candidate, str):
                                category = "missing_content"
                            else:
                                content = candidate
                except (ValueError, TypeError, KeyError):
                    if category is None:
                        category = "invalid_response"
            elif category is None:
                category = "invalid_response"
            # An HTTP error can also carry usage and finish evidence.
            choices = metadata.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                finish_reason = choices[0].get("finish_reason")
            processing_end = self.clock()
            record = {
                "attempt_index": attempt,
                "started_at": attempt_started_at,
                "completed_at": utc_now(),
                "http_status": status,
                "response_seconds": response_end - attempt_start,
                "response_processing_seconds": processing_end - response_end,
                "outcome": "success" if category is None else "failure",
                "error": category,
                "response": dict(metadata),
                "finish_reason": finish_reason,
                "usage": metadata["usage"],
                "retry_wait_seconds": 0.0,
                "retry_wait_planned_seconds": 0.0,
            }
            records.append(record)
            retryable = category == "transport_error" or status in RETRYABLE_HTTP
            if category is not None and retryable and attempt < config.max_attempts:
                delay = (
                    retry_after
                    if retry_after is not None
                    else min(60.0, config.retry_base_seconds * 2 ** (attempt - 1))
                )
                wait_start = self.clock()
                self.sleep(delay)
                record["retry_wait_seconds"] = self.clock() - wait_start
                record["retry_wait_planned_seconds"] = delay
                continue
            elapsed = self.clock() - start
            metadata.update(
                {
                    "attempts": attempt,
                    "attempt_records": records,
                    "latency_seconds": elapsed,
                    "timing": {
                        "started_at": started_at,
                        "completed_at": utc_now(),
                        "request_seconds": elapsed,
                        "retry_wait_seconds": sum(r["retry_wait_seconds"] for r in records),
                    },
                    "finish_reason": finish_reason,
                    "reasoning_policy": "provider_default",
                }
            )
            if category is not None:
                raise APIError(category, attempt, elapsed, metadata)
            assert content is not None
            metadata.update(
                response_id=metadata.get("id"), response_choices=metadata.get("choices")
            )
            return Completion(content, metadata)
        raise AssertionError("Unreachable")
