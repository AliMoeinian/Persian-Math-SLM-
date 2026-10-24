"""Summaries retain missing usage as unknown, never silently zero."""

import math
import statistics
from typing import Any


def distribution(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "median": None, "p95": None}
    ordered = sorted(values)
    return {
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
    }


def usage_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"calls": len(calls)}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens", "cost"):
        values = [c.get("metadata", {}).get("usage", {}).get(name) for c in calls]
        known = [v for v in values if type(v) in (int, float) and math.isfinite(v)]
        result[name] = {
            "reported_sum": sum(known) if known else None,
            "reported_calls": len(known),
            "missing_calls": len(calls) - len(known),
        }
    result["latency_seconds"] = distribution(
        [c["latency_seconds"] for c in calls if "latency_seconds" in c]
    )
    return result
