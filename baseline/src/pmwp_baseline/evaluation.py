"""Paper metrics plus explicitly separate diagnostics over all selected problems."""

import math
from typing import Any

from .config import Config
from .telemetry import distribution, usage_summary


def correct(value: float | None, gold: float, config: Config) -> bool:
    return (
        value is not None
        and math.isfinite(value)
        and math.isclose(value, gold, abs_tol=config.abs_tolerance, rel_tol=config.rel_tolerance)
    )


def wilson(successes: int, count: int) -> list[float] | None:
    if not count:
        return None
    z = 1.959963984540054
    p = successes / count
    denominator = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denominator
    margin = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denominator
    return [100 * max(0, center - margin), 100 * min(1, center + margin)]


def summarize(rows: list[dict[str, Any]], expected: int, config: Config) -> dict[str, Any]:
    if expected < 1 or len(rows) > expected or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Invalid result population")
    successes = sum(r["correct"] for r in rows)
    calls = [call for row in rows for call in row["calls"]]
    groups: dict[str, Any] = {}
    for dimension in ("pure_template", "text_length_bin", "number_count", "operation_count"):
        bins: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            bins.setdefault(str(row["features"][dimension]), []).append(row)
        groups[dimension] = {
            key: {
                "n": len(group),
                "correct": sum(r["correct"] for r in group),
                "accuracy_percent": 100 * sum(r["correct"] for r in group) / len(group),
            }
            for key, group in sorted(bins.items())
        }
    total_usage = usage_summary(calls)
    reported_cost = total_usage["cost"]["reported_sum"]
    return {
        "cost_per_correct_reported_usd": reported_cost / successes
        if reported_cost is not None and successes
        else None,
        "total_usage": total_usage,
        "status": "complete" if len(rows) == expected else "partial",
        "expected": expected,
        "recorded": len(rows),
        "unrun": expected - len(rows),
        "metric": "AnsAcc" if config.mode == "numeric" else "EqValueAcc",
        "correct": successes,
        "accuracy_percent": 100 * successes / expected,
        "accuracy_ci95_wilson_percent": wilson(successes, expected)
        if len(rows) == expected
        else None,
        "invalid_predictions": sum(not r["parsed"]["valid"] for r in rows),
        "format_compliant": sum(r["parsed"]["format_compliant"] for r in rows),
        "unsimplified_equations": sum(r["parsed"]["unsimplified"] is True for r in rows),
        "api_failed_examples": sum(r["status"] == "api_error" for r in rows),
        "retry_attempts": sum(c.get("attempts", 1) - 1 for c in calls),
        "end_to_end_latency_seconds": distribution([r["latency_seconds"] for r in rows]),
        "telemetry": {
            stage: usage_summary([c for c in calls if c["stage"] == stage])
            for stage in ("translation", "solving")
        },
        "usage_caveat": "Provider-reported usage excludes unknown charges from failed attempts.",
        "breakdown": groups,
    }
