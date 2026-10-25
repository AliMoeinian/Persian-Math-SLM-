"""Sequential, append-and-fsync inference with strict resume identity."""

import fcntl
import json
import os
import re
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .client import APIError, Completion
from .config import Config
from .data import Problem, fingerprint, load_problems
from .evaluation import correct, summarize
from .parsing import parse_response
from .prompts import messages, prompt_hashes, templates, translation_messages


class Client(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> Completion: ...


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (ValueError, UnicodeError) as exc:
        raise ValueError(
            "Corrupt predictions file; preserve it and recover before resuming"
        ) from exc
    return rows


def revision() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def source_hashes() -> dict[str, str]:
    return {p.name: fingerprint(p) for p in sorted(Path(__file__).parent.glob("*.py"))}


def infer(problem: Problem, config: Config, client: Client) -> dict[str, Any]:
    start = time.perf_counter()
    calls: list[dict[str, Any]] = []
    text = problem.text
    content = ""
    status = "ok"
    for stage in ["translation", "solving"] if config.language == "en" else ["solving"]:
        request = (
            translation_messages(text)
            if stage == "translation"
            else messages(text, config.mode, config.language)
        )
        call: dict[str, Any] = {"stage": stage, "messages": request}
        try:
            completion = client.complete(request)
            call.update(
                {
                    "content": completion.content,
                    "metadata": completion.metadata,
                    "attempts": completion.metadata.get("attempts", 1),
                    "latency_seconds": completion.metadata.get("latency_seconds", 0.0),
                }
            )
            calls.append(call)
            if stage == "translation":
                if not completion.content.strip():
                    status = "empty_translation"
                    break
                text = completion.content
            else:
                content = completion.content
        except APIError as exc:
            call.update(
                {
                    "error": exc.category,
                    "attempts": exc.attempts,
                    "latency_seconds": exc.elapsed,
                    "metadata": exc.metadata,
                }
            )
            calls.append(call)
            status = "api_error"
            break

    parsed = parse_response(content, config.mode)
    length = len(problem.text.split())
    return {
        "id": problem.id,
        "text": problem.text,
        "answer": problem.answer,
        "translated_text": text if config.language == "en" and stage == "solving" else None,
        "output": content,
        "status": status,
        "calls": calls,
        "parsed": asdict(parsed),
        "correct": correct(parsed.value, problem.answer, config),
        "latency_seconds": time.perf_counter() - start,
        "features": {
            "pure_template": problem.pure_template,
            "number_count": problem.number_count,
            "operation_count": len(re.findall(r"[+*/-]", problem.pure_template)),
            "text_length_bin": "short_0_20"
            if length <= 20
            else "medium_21_40"
            if length <= 40
            else "long_41_plus",
        },
    }


def run(
    dataset: Path,
    output: Path,
    config: Config,
    client: Client,
    *,
    limit: int | None = None,
    split: str,
    resume: bool = False,
) -> dict[str, Any]:
    problems = load_problems(dataset)
    if split not in {"validation", "test", "smoke"}:
        raise ValueError("Specify split: validation, test, or smoke")
    if limit is not None and (limit < 1 or limit > len(problems)):
        raise ValueError("Limit must be between 1 and dataset size")
    selected = problems[:limit] if limit is not None else problems
    identity: dict[str, Any] = {
        "config": config.to_dict(),
        "dataset_sha256": fingerprint(dataset),
        "selected_ids": [p.id for p in selected],
        "split": split,
        "prompt_hashes": prompt_hashes(),
        "source_hashes": source_hashes(),
        "schema_version": 1,
    }
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Run directory is locked by another process") from exc
        manifest_path = output / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            if not resume or manifest["identity"] != identity:
                raise ValueError(
                    "Existing run: use --resume with identical data, code, prompts, config"
                )
        else:
            if any(p.name != ".lock" for p in output.iterdir()):
                raise ValueError("Refusing to overwrite a nonempty run directory")
            write_json(
                manifest_path,
                {
                    "identity": identity,
                    "prompts": templates(),
                    "git_revision": revision(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "dataset_path": str(dataset.resolve()),
                    "full_dataset_size": len(problems),
                    "is_full_test": split == "test" and len(selected) == len(problems),
                },
            )
        predictions = output / "predictions.jsonl"
        rows = read_rows(predictions)
        ids = [r["id"] for r in rows]
        if len(set(ids)) != len(ids) or ids != identity["selected_ids"][: len(ids)]:
            raise ValueError("Recorded IDs do not match the selected ordered population")
        # Failed records are terminal: resume never silently reselects/retries bad outcomes.
        try:
            with predictions.open("a", encoding="utf-8") as stream:
                for problem in selected[len(rows) :]:
                    row = infer(problem, config, client)
                    stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    rows.append(row)
        finally:
            summary = summarize(rows, len(selected), config)
            write_json(output / "summary.json", summary)
            temporary = output / "failures.jsonl.tmp"
            temporary.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows if not r["correct"]),
                encoding="utf-8",
            )
            temporary.replace(output / "failures.jsonl")
    return summary
