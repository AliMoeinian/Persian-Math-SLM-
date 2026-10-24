"""Versioned prompt text; only English solving templates are published wording."""

from hashlib import sha256
from importlib.resources import files


def templates() -> dict[str, str]:
    root = files("pmwp_baseline").joinpath("prompts")
    return {
        name: root.joinpath(name).read_text(encoding="utf-8").rstrip("\n")
        for name in (
            "numeric.en.txt",
            "numeric.fa.txt",
            "equation.en.txt",
            "equation.fa.txt",
            "system.txt",
            "translation.txt",
        )
    }


def prompt_hashes() -> dict[str, str]:
    return {k: sha256(v.encode()).hexdigest() for k, v in templates().items()}


def messages(problem: str, mode: str, language: str) -> list[dict[str, str]]:
    if mode not in {"numeric", "equation"} or language not in {"fa", "en"}:
        raise ValueError("Unsupported mode or language")
    t = templates()
    return [
        {"role": "system", "content": t["system.txt"]},
        {"role": "user", "content": t[f"{mode}.{language}.txt"].format(problem=problem)},
    ]


def translation_messages(problem: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": templates()["translation.txt"].format(problem=problem)}]
