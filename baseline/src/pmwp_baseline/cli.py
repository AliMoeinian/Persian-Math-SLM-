"""CLI commands are offline unless explicitly invoking run."""

import argparse
import json
import random
import sys
from dataclasses import asdict
from pathlib import Path

from .client import OpenRouterClient
from .config import Config, load_config
from .data import fingerprint, load_problems
from .evaluation import correct, summarize
from .parsing import parse_response
from .runner import read_rows, run, write_json


def evaluate(directory: Path, dataset: Path) -> dict[str, object]:
    manifest = json.loads((directory / "manifest.json").read_text())
    identity = manifest["identity"]
    if fingerprint(dataset) != identity["dataset_sha256"]:
        raise ValueError("Dataset fingerprint differs from manifest")
    config = Config(**identity["config"])
    gold = {p.id: p for p in load_problems(dataset)}
    rows = read_rows(directory / "predictions.jsonl")
    ids = [r["id"] for r in rows]
    if ids != identity["selected_ids"][: len(ids)] or len(set(ids)) != len(ids):
        raise ValueError("Predictions do not match manifest")
    for row in rows:
        parsed = parse_response(row["output"], config.mode)
        row["parsed"] = asdict(parsed)
        row["correct"] = correct(parsed.value, gold[row["id"]].answer, config)
    result = summarize(rows, len(identity["selected_ids"]), config)
    write_json(directory / "reevaluated-summary.json", result)
    return result


def compare(left: Path, right: Path) -> dict[str, object]:
    a = json.loads((left / "manifest.json").read_text())["identity"]
    b = json.loads((right / "manifest.json").read_text())["identity"]
    for key in ("dataset_sha256", "selected_ids", "split"):
        if a[key] != b[key]:
            raise ValueError(f"Cannot compare different {key}")
    for key in ("mode", "abs_tolerance", "rel_tolerance"):
        if a["config"][key] != b["config"][key]:
            raise ValueError(f"Cannot compare different {key}")
    x, y = read_rows(left / "predictions.jsonl"), read_rows(right / "predictions.jsonl")
    if [r["id"] for r in x] != a["selected_ids"] or [r["id"] for r in y] != b["selected_ids"]:
        raise ValueError("Comparison requires complete, ordered runs")
    # Paired example bootstrap: conditional on this one run per model, not model reruns.
    deltas = [int(v["correct"]) - int(u["correct"]) for u, v in zip(x, y)]
    rng = random.Random(0)
    samples = sorted(
        100 * sum(rng.choices(deltas, k=len(deltas))) / len(deltas) for _ in range(2000)
    )
    return {
        "paired_bootstrap_ci95_difference_pp": [samples[49], samples[1949]],
        "bootstrap_seed": 0,
        "bootstrap_resamples": 2000,
        "n": len(x),
        "left_only_correct": [u["id"] for u, v in zip(x, y) if u["correct"] and not v["correct"]],
        "right_only_correct": [u["id"] for u, v in zip(x, y) if v["correct"] and not u["correct"]],
        "both_correct": sum(u["correct"] and v["correct"] for u, v in zip(x, y)),
        "both_incorrect": sum(not u["correct"] and not v["correct"] for u, v in zip(x, y)),
        "accuracy_difference_right_minus_left_pp": 100
        * (sum(r["correct"] for r in y) - sum(r["correct"] for r in x))
        / len(x),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="PMWP zero-shot evaluation via OpenRouter")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-data", help="Offline dataset validation")
    validate.add_argument("dataset", type=Path)
    execute = commands.add_parser("run", help="Send paid API requests")
    execute.add_argument("--config", type=Path, required=True)
    execute.add_argument("--dataset", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--split", choices=["validation", "test", "smoke"], required=True)
    execute.add_argument("--limit", type=int)
    execute.add_argument("--resume", action="store_true")
    score = commands.add_parser("evaluate", help="Re-score saved outputs without API requests")
    score.add_argument("directory", type=Path)
    score.add_argument("--dataset", type=Path, required=True)
    pair = commands.add_parser("compare", help="Paired comparison of complete saved runs")
    pair.add_argument("left", type=Path)
    pair.add_argument("right", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "validate-data":
            problems = load_problems(args.dataset)
            result = {"records": len(problems), "sha256": fingerprint(args.dataset)}
        elif args.command == "run":
            config = load_config(args.config)
            result = run(
                args.dataset,
                args.output,
                config,
                OpenRouterClient(config),
                limit=args.limit,
                split=args.split,
                resume=args.resume,
            )
        elif args.command == "evaluate":
            result = evaluate(args.directory, args.dataset)
        else:
            result = compare(args.left, args.right)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        # Never print HTTP bodies, headers, keys, or arbitrary exception payloads.
        print(
            f"Command failed ({type(exc).__name__}); check local inputs and run artifacts.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
