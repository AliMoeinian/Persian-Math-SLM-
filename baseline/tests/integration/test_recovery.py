import tempfile
import unittest
from pathlib import Path

from integration.test_runner import FIXTURE, FakeClient

from pmwp_baseline.client import APIError
from pmwp_baseline.config import Config
from pmwp_baseline.recovery import eligible, recover
from pmwp_baseline.runner import read_rows, run


class RecoveryTests(unittest.TestCase):
    def test_only_infrastructure_errors_are_eligible(self):
        for error in ("missing_content", "invalid_response", "http_401", "http_400"):
            self.assertFalse(eligible({"status": "api_error", "calls": [{"error": error}]}))
        self.assertFalse(eligible({"status": "ok", "calls": [{"content": "x = 999"}]}))
        for error in ("transport_error", "http_429", "http_502"):
            self.assertTrue(eligible({"status": "api_error", "calls": [{"error": error}]}))

    def test_network_recovery_reuses_translation_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "original", Path(directory) / "recovery"
            config = Config("test/model", "numeric", "en", "test-provider")
            run(
                FIXTURE,
                source,
                config,
                FakeClient(["English one", APIError("http_502", 2, 0.2), "English two", "x = 999"]),
                split="smoke",
            )
            before = (source / "predictions.jsonl").read_bytes()
            client = FakeClient(["x = 80"])
            summary = recover(source, target, client)
            self.assertEqual(len(client.requests), 1)
            self.assertIn("English one", client.requests[0][1]["content"])
            self.assertEqual(summary["correct"], 1)
            self.assertEqual(summary["recovery_submitted_ids"], [1])
            self.assertEqual((source / "predictions.jsonl").read_bytes(), before)
            rows = read_rows(target / "predictions.jsonl")
            self.assertEqual(rows[1]["output"], "x = 999")
            self.assertEqual(len(rows[0]["calls"]), 3)
            # One cached translation, two failed HTTP attempts, one recovered solve.
            self.assertEqual(summary["total_usage"]["attempts"], 6)
            self.assertEqual(summary["total_usage"]["total_tokens"]["reported_sum"], 60)
            self.assertEqual(summary["total_usage"]["total_tokens"]["missing_attempts"], 2)
            with self.assertRaises(ValueError):
                recover(target, Path(directory) / "third", client)

    def test_missing_content_not_resubmitted(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "original", Path(directory) / "recovery"
            config = Config("test/model", "numeric", "fa", "test-provider")
            run(
                FIXTURE,
                source,
                config,
                FakeClient(
                    [APIError("missing_content", 1, 0.1), APIError("invalid_response", 1, 0.1)]
                ),
                split="smoke",
            )
            client = FakeClient([])
            result = recover(source, target, client)
            self.assertEqual(client.requests, [])
            self.assertEqual(result["correct"], 0)

    def test_initial_recovery_and_cumulative_times_stay_separate(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "original", Path(directory) / "recovery"
            config = Config("test/model", "numeric", "fa", "test-provider")
            with patch("pmwp_baseline.runner.time.perf_counter", side_effect=[0, 8, 10, 14]):
                run(
                    FIXTURE,
                    source,
                    config,
                    FakeClient([APIError("http_502", 2, 0.2), "x = 999"]),
                    split="smoke",
                )
            with patch("pmwp_baseline.runner.time.perf_counter", side_effect=[1000, 1002]):
                result = recover(source, target, FakeClient(["x = 80"]))
            rows = read_rows(target / "predictions.jsonl")
            self.assertEqual(rows[0]["timing"]["initial_pass_processing_seconds"], 8)
            self.assertEqual(rows[0]["timing"]["recovery_processing_seconds"], 2)
            self.assertEqual(rows[0]["timing"]["cumulative_processing_seconds"], 10)
            self.assertFalse(rows[0]["timing"]["includes_interpass_idle_time"])
            self.assertEqual(
                result["problem_timing"]["initial_pass_processing_seconds"]["failure"]["mean"], 8
            )
            self.assertEqual(
                result["problem_timing"]["recovery_processing_seconds"]["success"]["mean"], 2
            )
            self.assertEqual(result["problem_timing"]["cumulative_processing_seconds"]["mean"], 7)
            self.assertNotIn("end_to_end_latency_seconds", result)
