"""Explicit timing populations and provider-reported usage over individual attempts."""

import math
import statistics
from typing import Any, TypeGuard

USAGE_PATHS = {
    "prompt_tokens": ("prompt_tokens",),
    "completion_tokens": ("completion_tokens",),
    "total_tokens": ("total_tokens",),
    "cost": ("cost",),
    "reasoning_tokens": ("completion_tokens_details", "reasoning_tokens"),
    "cached_prompt_tokens": ("prompt_tokens_details", "cached_tokens"),
    "cache_write_tokens": ("prompt_tokens_details", "cache_write_tokens"),
}


def distribution(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p95": None}
    ordered = sorted(values)
    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
    }


def number(value: Any) -> TypeGuard[int | float]:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def usage_value(usage: dict[str, Any], path: tuple[str, ...]) -> float | int | None:
    value: Any = usage
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value if number(value) else None


def attempts_for(call: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = call.get("metadata") or {}
    if metadata.get("reused_from_parent"):
        return []
    records = metadata.get("attempt_records")
    if isinstance(records, list):
        return records
    # Legacy runs retain final usage only. Never invent earlier usage or timings.
    count = max(1, call.get("attempts", 1))
    records = [{"usage": {}, "outcome": "unknown", "legacy": True} for _ in range(count - 1)]
    records.append(
        {
            "usage": metadata.get("usage") or {},
            "legacy": True,
            "outcome": "failure" if call.get("error") else "success",
            "finish_reason": metadata.get("finish_reason"),
        }
    )
    return records


def partition(values: list[tuple[float, bool]]) -> dict[str, Any]:
    return {
        "all": distribution([v for v, _ in values]),
        "success": distribution([v for v, ok in values if ok]),
        "failure": distribution([v for v, ok in values if not ok]),
    }


def usage_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    records = [r for c in calls for r in attempts_for(c)]
    result: dict[str, Any] = {
        "calls": len(calls),
        "attempts": len(records),
        "legacy_attempts": sum(bool(r.get("legacy")) for r in records),
    }
    for name, path in USAGE_PATHS.items():
        values = [usage_value(r.get("usage") or {}, path) for r in records]
        known = [v for v in values if v is not None]
        result[name] = {
            "reported_sum": sum(known) if known else None,
            "reported_attempts": len(known),
            "missing_attempts": len(records) - len(known),
            "complete": bool(records) and len(known) == len(records),
        }
    comparable = [
        r["usage"]
        for r in records
        if all(
            usage_value(r.get("usage") or {}, (key,)) is not None
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        )
    ]
    result["token_consistency"] = {
        "comparable_attempts": len(comparable),
        "mismatched_attempts": sum(
            u["prompt_tokens"] + u["completion_tokens"] != u["total_tokens"] for u in comparable
        ),
    }
    request_values = []
    waits = []
    for call in calls:
        timing = (call.get("metadata") or {}).get("timing") or {}
        value = timing.get("request_seconds", call.get("latency_seconds"))
        if number(value):
            request_values.append((value, not bool(call.get("error"))))
        if number(timing.get("retry_wait_seconds")):
            waits.append(timing["retry_wait_seconds"])
    response_values = [
        (r["response_seconds"], r["outcome"] == "success")
        for r in records
        if number(r.get("response_seconds"))
    ]
    processing = [
        r["response_processing_seconds"]
        for r in records
        if number(r.get("response_processing_seconds"))
    ]
    result["timing"] = {
        "request_seconds": partition(request_values),
        "attempt_response_seconds": partition(response_values),
        "missing_attempt_response_timings": len(records) - len(response_values),
        "response_processing_seconds": distribution(processing),
        "retry_wait_seconds_per_request": distribution(waits),
        "reported_retry_wait_seconds_sum": sum(waits) if waits else None,
        "missing_request_wait_timings": len(calls) - len(waits),
    }
    result["outcomes"] = {
        kind: sum(r["outcome"] == kind for r in records)
        for kind in ("success", "failure", "unknown")
    }
    return result


def problem_timing(rows: list[dict[str, Any]]) -> dict[str, Any]:
    initial, recovery, cumulative = [], [], []
    missing = 0
    for row in rows:
        timing = row.get("timing") or {}
        initial_value = timing.get("initial_pass_processing_seconds")
        if number(initial_value):
            initial.append((initial_value, timing["initial_pass_outcome"] == "success"))
        else:
            missing += 1
        if number(timing.get("recovery_processing_seconds")):
            recovery.append((timing["recovery_processing_seconds"], row["status"] == "ok"))
        if number(timing.get("cumulative_processing_seconds")):
            cumulative.append(timing["cumulative_processing_seconds"])
    return {
        "initial_pass_processing_seconds": partition(initial),
        "recovery_processing_seconds": partition(recovery),
        "cumulative_processing_seconds": distribution(cumulative),
        "missing_initial_pass_timings": missing,
    }
