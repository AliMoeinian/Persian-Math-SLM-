"""Read both supplied PMWP encodings without modifying benchmark files."""

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Problem:
    id: int
    text: str
    answer: float
    pure_template: str
    number_count: int


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_problems(path: Path) -> list[Problem]:
    raw = path.read_text(encoding="utf-8-sig")
    if raw.lstrip().startswith("["):
        rows = json.loads(raw)
    else:
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not isinstance(rows, list) or not rows:
        raise ValueError("Dataset must contain a nonempty list of problems")
    result = []
    ids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each problem must be an object")
        identifier, text, answer = row.get("id"), row.get("text"), row.get("answer")
        if type(identifier) is not int or identifier in ids:
            raise ValueError("Problem IDs must be unique integers")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Empty or invalid text for {identifier}")
        if (
            isinstance(answer, bool)
            or not isinstance(answer, (int, float))
            or not math.isfinite(answer)
        ):
            raise ValueError(f"Invalid gold answer for {identifier}")
        if not isinstance(row.get("pure_template", ""), str):
            raise ValueError("Invalid pure_template")
        numbers = row.get("number_list", [])
        if not isinstance(numbers, list):
            raise ValueError("Invalid number_list")
        ids.add(identifier)
        result.append(
            Problem(identifier, text, float(answer), row.get("pure_template", ""), len(numbers))
        )
    return result
