import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pmwp_baseline.cli import compare, evaluate, main
from pmwp_baseline.client import APIError, Completion
from pmwp_baseline.config import Config
from pmwp_baseline.runner import read_rows, run

FIXTURE = Path(__file__).parents[1] / "fixtures" / "problems.json"


class FakeClient:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.requests = []

    def complete(self, messages):
        self.requests.append(messages)
        output = next(self.outputs)
        if isinstance(output, BaseException):
            raise output
        return Completion(
            output,
            {
                "latency_seconds": 0.1,
                "attempts": 1,
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost": 0.001,
                },
            },
        )


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("test/model", "equation", "fa", "test-provider")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "run"

    def test_end_to_end_resume_and_rescore(self):
        client = FakeClient(["Reason\nx = 10 * 8", "x = 20 / 4"])
        result = run(FIXTURE, self.output, self.config, client, split="validation")
        self.assertEqual(result["accuracy_percent"], 100)
        self.assertEqual(result["recorded"], 2)
        self.assertEqual(result["telemetry"]["solving"]["total_tokens"]["reported_sum"], 30)
        resumed = FakeClient([])
        self.assertEqual(
            run(FIXTURE, self.output, self.config, resumed, split="validation", resume=True)[
                "correct"
            ],
            2,
        )
        self.assertEqual(resumed.requests, [])
        self.assertEqual(evaluate(self.output, FIXTURE)["correct"], 2)
        self.assertEqual(compare(self.output, self.output)["both_correct"], 2)
        for updates in ({"mode": "numeric"}, {"max_tokens": 3000}):
            with self.assertRaises(ValueError):
                run(
                    FIXTURE,
                    self.output,
                    Config(**{**self.config.to_dict(), **updates}),
                    resumed,
                    split="validation",
                    resume=True,
                )
        with self.assertRaises(ValueError):
            run(FIXTURE, self.output, self.config, resumed, split="validation")

    def test_english_translation_and_failure_denominator(self):
        config = Config(**{**self.config.to_dict(), "language": "en"})
        client = FakeClient(
            ["Ten people eat eight slices each. Find x.", "x = 8*10", APIError("http_503", 3, 0.2)]
        )
        result = run(FIXTURE, self.output, config, client, split="smoke")
        self.assertEqual(result["accuracy_percent"], 50)
        self.assertEqual(result["api_failed_examples"], 1)
        self.assertEqual(result["retry_attempts"], 2)
        self.assertIn("Ten people", client.requests[1][1]["content"])
        self.assertEqual(len(read_rows(self.output / "failures.jsonl")), 1)
        rows = read_rows(self.output / "predictions.jsonl")
        self.assertEqual(rows[0]["text"], json.loads(FIXTURE.read_text())[0]["text"])
        self.assertEqual(rows[1]["status"], "api_error")

    def test_interruption_resume(self):
        client = FakeClient(["x = 80", KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            run(FIXTURE, self.output, self.config, client, split="validation")
        summary = json.loads((self.output / "summary.json").read_text())
        self.assertEqual(summary["unrun"], 1)
        self.assertEqual(summary["status"], "partial")
        resumed = FakeClient(["x = 5"])
        result = run(FIXTURE, self.output, self.config, resumed, split="validation", resume=True)
        self.assertEqual(result["correct"], 2)
        self.assertEqual(len(resumed.requests), 1)

    def test_corrupt_results_and_changed_dataset(self):
        run(FIXTURE, self.output, self.config, FakeClient(["x = 80", "x = 5"]), split="smoke")
        with (self.output / "predictions.jsonl").open("a") as stream:
            stream.write('{"id":')
        with self.assertRaises(ValueError):
            run(FIXTURE, self.output, self.config, FakeClient([]), split="smoke", resume=True)
        changed = Path(self.temporary.name) / "changed.json"
        changed.write_text(FIXTURE.read_text() + "\n")
        with self.assertRaises(ValueError):
            evaluate(self.output, changed)

    def test_empty_translation_and_invalid_population(self):
        config = Config(**{**self.config.to_dict(), "language": "en"})
        result = run(FIXTURE, self.output, config, FakeClient(["", ""]), split="smoke")
        self.assertEqual(result["invalid_predictions"], 2)
        with self.assertRaises(ValueError):
            run(FIXTURE, self.output, config, FakeClient([]), split="smoke", limit=0)

    def test_cli_offline(self):
        with patch("sys.argv", ["pmwp-baseline", "validate-data", str(FIXTURE)]):
            with patch("builtins.print") as printer:
                self.assertEqual(main(), 0)
                self.assertIn('"records": 2', printer.call_args[0][0])

    def test_translation_cost_is_included_and_metric_is_numeric(self):
        config = Config(**{**self.config.to_dict(), "language": "en", "mode": "numeric"})
        client = FakeClient(["English one", "x = 80", "English two", "x = 5"])
        result = run(FIXTURE, self.output, config, client, split="validation")
        self.assertEqual(result["metric"], "AnsAcc")
        self.assertEqual(result["total_usage"]["total_tokens"]["reported_sum"], 60)
        self.assertEqual(result["total_usage"]["cost"]["reported_sum"], 0.004)
        self.assertEqual(result["cost_per_correct_reported_usd"], 0.002)
        self.assertEqual(result["breakdown"]["operation_count"]["1"]["n"], 2)

    def test_lock_and_nonempty_directory_protection(self):
        import fcntl

        self.output.mkdir()
        with (self.output / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ValueError):
                run(FIXTURE, self.output, self.config, FakeClient([]), split="smoke")
        (self.output / "existing-file.txt").write_text("preserve me")
        with self.assertRaises(ValueError):
            run(FIXTURE, self.output, self.config, FakeClient([]), split="smoke")
        self.assertEqual((self.output / "existing-file.txt").read_text(), "preserve me")

    def test_pairwise_difference_and_incompatible_mode(self):
        run(FIXTURE, self.output, self.config, FakeClient(["x = 80", "bad"]), split="smoke")
        right = self.output.parent / "right"
        run(FIXTURE, right, self.config, FakeClient(["x = 80", "x = 5"]), split="smoke")
        comparison = compare(self.output, right)
        self.assertEqual(comparison["right_only_correct"], [2])
        self.assertEqual(comparison["accuracy_difference_right_minus_left_pp"], 50)
        self.assertEqual(comparison, compare(self.output, right))
        numeric = self.output.parent / "numeric"
        config = Config(**{**self.config.to_dict(), "mode": "numeric"})
        run(FIXTURE, numeric, config, FakeClient(["x = 80", "x = 5"]), split="smoke")
        with self.assertRaises(ValueError):
            compare(self.output, numeric)
