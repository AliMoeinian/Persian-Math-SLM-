"""Explicit, validated run settings; no credentials in configuration."""

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Config:
    model: str
    mode: str
    language: str
    provider: str
    temperature: float = 0.0
    max_tokens: int = 2048
    timeout_seconds: float = 120.0
    max_attempts: int = 3
    retry_base_seconds: float = 1.0
    abs_tolerance: float = 1e-6
    rel_tolerance: float = 1e-9

    def __post_init__(self) -> None:
        if not self.model or not self.provider:
            raise ValueError("Explicit model and provider required")
        if self.mode not in {"numeric", "equation"} or self.language not in {"fa", "en"}:
            raise ValueError("Invalid mode/language")
        if self.temperature != 0:
            raise ValueError("Paper baseline requires temperature=0")
        if not (type(self.max_tokens) is int and 1 <= self.max_tokens <= 131072):
            raise ValueError("Invalid max_tokens")
        if not (type(self.max_attempts) is int and 1 <= self.max_attempts <= 5):
            raise ValueError("max_attempts must be between 1 and 5")
        for value in (
            self.timeout_seconds,
            self.retry_base_seconds,
            self.abs_tolerance,
            self.rel_tolerance,
        ):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid timing or tolerance")
        if self.timeout_seconds == 0 or self.timeout_seconds > 600:
            raise ValueError("Timeout must be in (0, 600]")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_config(path: Path) -> Config:
    return Config(**json.loads(path.read_text(encoding="utf-8")))
