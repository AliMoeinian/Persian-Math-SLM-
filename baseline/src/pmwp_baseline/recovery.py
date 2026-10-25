"""A separate bounded recovery pass, selecting infrastructure failures only."""

import copy
import fcntl
import json
import os
from pathlib import Path
from typing import Any

from .client import Completion
from .config import Config
from .data import Problem
from .evaluation import summarize
from .prompts import prompt_hashes
from .runner import Client, infer, read_rows, source_hashes, write_json

RECOVERABLE = {
    "transport_error",
    "http_408",
    "http_429",
    "http_500",
    "http_502",
    "http_503",
    "http_504",
}


def eligible(row: dict[str, Any]) -> bool:
    return (
        row["status"] == "api_error"
        and bool(row["calls"])
        and row["calls"][-1].get("error") in RECOVERABLE
    )


class ReplaySuccessfulStages:
    """Reuse successful translation; never send it to the provider again."""

    def __init__(self, row: dict[str, Any], client: Client) -> None:
        self.cached = iter(row["calls"][:-1])
        self.client = client

    def complete(self, messages: list[dict[str, str]]) -> Completion:
        cached = next(self.cached, None)
        if cached is None:
            return self.client.complete(messages)
        if cached["messages"] != messages or "error" in cached:
            raise ValueError("Cached stage does not match the original request")
        metadata = copy.deepcopy(cached["metadata"])
        metadata.update(attempts=0, latency_seconds=0.0, reused_from_parent=True)
        return Completion(cached["content"], metadata)


def recover(source: Path, destination: Path, client: Client) -> dict[str, Any]:
    manifest = json.loads((source / "manifest.json").read_text())
    if "recovery_parent" in manifest:
        raise ValueError("This command supports one recovery pass from an original run")
    config = Config(**manifest["identity"]["config"])
    if manifest["identity"]["prompt_hashes"] != prompt_hashes():
        raise ValueError("Recovery must use original prompt files")
    rows = read_rows(source / "predictions.jsonl")
    if [r["id"] for r in rows] != manifest["identity"]["selected_ids"]:
        raise ValueError("Finish the original pass before starting recovery")
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any(p.name != ".lock" for p in destination.iterdir()):
            raise ValueError("Recovery destination must be empty")
        manifest["recovery_parent"] = str(source.resolve())
        manifest["recovery_source_hashes"] = source_hashes()
        manifest["telemetry_schema_version"] = 2
        manifest["recovery_ids"] = [r["id"] for r in rows if eligible(r)]
        write_json(destination / "manifest.json", manifest)
        recovered = []
        try:
            with (destination / "predictions.jsonl").open("a", encoding="utf-8") as stream:
                for original in rows:
                    row = copy.deepcopy(original)
                    if eligible(original):
                        features = original["features"]
                        problem = Problem(
                            original["id"],
                            original["text"],
                            original["answer"],
                            features["pure_template"],
                            features["number_count"],
                        )
                        row = infer(problem, config, ReplaySuccessfulStages(original, client))
                        # Latency/usage summaries describe cumulative work, including parent calls.
                        row["calls"] = copy.deepcopy(original["calls"]) + [
                            c
                            for c in row["calls"]
                            if not c.get("metadata", {}).get("reused_from_parent")
                        ]
                        current_timing = row["timing"]
                        parent_timing = original.get("timing") or {}
                        initial = parent_timing.get("initial_pass_processing_seconds")
                        prior = parent_timing.get("cumulative_processing_seconds")
                        recovery_seconds = current_timing["initial_pass_processing_seconds"]
                        row["timing"] = {
                            "started_at": parent_timing.get("started_at"),
                            "completed_at": current_timing["completed_at"],
                            "recovery_started_at": current_timing["started_at"],
                            "initial_pass_processing_seconds": initial,
                            "initial_pass_outcome": parent_timing.get("initial_pass_outcome"),
                            "recovery_processing_seconds": recovery_seconds,
                            "cumulative_processing_seconds": prior + recovery_seconds
                            if prior is not None
                            else None,
                            "includes_interpass_idle_time": False,
                        }
                        # Keep this legacy alias local to the latest infer call, never cumulative.
                        row["recovery_previous_status"] = original["status"]
                    stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    recovered.append(row)
        finally:
            summary = summarize(recovered, len(rows), config)
            summary["recovery_submitted_ids"] = manifest["recovery_ids"]
            write_json(destination / "summary.json", summary)
            (destination / "failures.jsonl").write_text(
                "".join(
                    json.dumps(r, ensure_ascii=False) + "\n" for r in recovered if not r["correct"]
                ),
                encoding="utf-8",
            )
    return summary
