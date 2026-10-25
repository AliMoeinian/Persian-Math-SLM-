import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from integration.test_runner import FIXTURE

from pmwp_baseline.cli import evaluate
from pmwp_baseline.client import OpenRouterClient
from pmwp_baseline.config import Config
from pmwp_baseline.runner import read_rows, run


class MeasurementIntegrationTests(unittest.TestCase):
    def test_real_client_ledger_survives_runner_storage_and_offline_scoring(self):
        clock = [0.0]
        count = [0]

        def sleep(delay):
            clock[0] += delay

        def opener(request, **kwargs):
            count[0] += 1
            clock[0] += 2
            usage = {"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6, "cost": 0}
            if count[0] == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    502,
                    "temporary",
                    {},
                    io.BytesIO(json.dumps({"usage": usage}).encode()),
                )
            content = None
            if count[0] == 2:
                content = "x = 80"
                usage = {
                    "prompt_tokens": 20,
                    "completion_tokens": 30,
                    "total_tokens": 50,
                    "cost": 0,
                }
            return io.BytesIO(
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {"content": content},
                                "finish_reason": "length" if content is None else "stop",
                            }
                        ],
                        "usage": usage,
                    }
                ).encode()
            )

        config = Config("test/model", "numeric", "fa", "provider")
        client = OpenRouterClient(
            config, api_key="do-not-store-me", opener=opener, sleep=sleep, clock=lambda: clock[0]
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            result = run(FIXTURE, output, config, client, split="smoke")
            self.assertEqual(result["accuracy_percent"], 50)
            self.assertEqual(result["total_usage"]["attempts"], 3)
            self.assertEqual(result["total_usage"]["total_tokens"]["reported_sum"], 62)
            self.assertTrue(result["total_usage"]["total_tokens"]["complete"])
            self.assertEqual(
                result["total_usage"]["timing"]["request_seconds"]["success"]["mean"], 5
            )
            self.assertEqual(
                result["total_usage"]["timing"]["request_seconds"]["failure"]["mean"], 2
            )
            self.assertEqual(
                result["problem_timing"]["initial_pass_processing_seconds"]["success"]["n"], 1
            )
            self.assertEqual(
                result["problem_timing"]["initial_pass_processing_seconds"]["failure"]["n"], 1
            )
            rows = read_rows(output / "predictions.jsonl")
            self.assertEqual(len(rows[0]["calls"][0]["metadata"]["attempt_records"]), 2)
            self.assertEqual(rows[1]["calls"][0]["metadata"]["finish_reason"], "length")
            self.assertEqual(evaluate(output, FIXTURE)["total_usage"], result["total_usage"])
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["telemetry_schema_version"], 2)
            self.assertEqual(manifest["identity"]["schema_version"], 2)
            for path in output.iterdir():
                if path.is_file():
                    self.assertNotIn("do-not-store-me", path.read_text())
