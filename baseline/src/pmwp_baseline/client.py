"""OpenRouter HTTPS client with bounded retries and sanitized error reporting."""

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


@dataclass
class Completion:
    content: str
    metadata: dict[str, Any]


class APIError(RuntimeError):
    def __init__(
        self, category: str, attempts: int, elapsed: float, metadata: dict[str, Any] | None = None
    ) -> None:
        super().__init__(category)
        self.category = category
        self.attempts = attempts
        self.elapsed = elapsed
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
        start = self.clock()
        for attempt in range(1, config.max_attempts + 1):
            retryable = False
            retry_after = None
            try:
                with self.opener(request, timeout=config.timeout_seconds) as response:
                    data = json.loads(response.read())
                metadata = {
                    k: data[k]
                    for k in ("id", "model", "provider", "usage", "choices")
                    if isinstance(data, dict) and k in data
                }
                metadata["usage"] = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                choices = data.get("choices")
                if data.get("error") or not isinstance(choices, list) or not choices:
                    raise APIError("invalid_response", attempt, self.clock() - start, metadata)
                choice = choices[0]
                content = choice.get("message", {}).get("content")
                if not isinstance(content, str):
                    raise APIError("missing_content", attempt, self.clock() - start, metadata)
                return Completion(
                    content,
                    {
                        "response_id": data.get("id"),
                        "response_choices": choices,
                        "model": data.get("model"),
                        "provider": data.get("provider"),
                        "usage": data.get("usage") if isinstance(data.get("usage"), dict) else {},
                        "finish_reason": choice.get("finish_reason"),
                        "attempts": attempt,
                        "latency_seconds": self.clock() - start,
                    },
                )
            except urllib.error.HTTPError as exc:
                category = f"http_{exc.code}"
                retryable = exc.code in {408, 429, 500, 502, 503, 504}
                header = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    retry_after = min(60.0, max(0.0, float(header))) if header else None
                except ValueError:
                    pass
                exc.close()
            except (urllib.error.URLError, TimeoutError, OSError):
                category, retryable = "transport_error", True
            except (ValueError, KeyError, TypeError, AttributeError):
                category = "invalid_response"
            if not retryable or attempt == config.max_attempts:
                raise APIError(category, attempt, self.clock() - start)
            self.sleep(
                retry_after
                if retry_after is not None
                else min(60.0, config.retry_base_seconds * 2 ** (attempt - 1))
            )
        raise AssertionError("Unreachable")
