import json
import tempfile
import unittest
from pathlib import Path

from pmwp_baseline.config import Config
from pmwp_baseline.data import load_problems
from pmwp_baseline.evaluation import correct, summarize, wilson
from pmwp_baseline.prompts import messages, prompt_hashes, templates, translation_messages
from pmwp_baseline.telemetry import distribution, usage_summary


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("test/model", "numeric", "fa", "test-provider")

    def test_data_encodings_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            row = {"id": 1, "text": "problem", "answer": 2}
            for data in (json.dumps([row]), json.dumps(row) + "\n"):
                path.write_text(data)
                self.assertEqual(load_problems(path)[0].text, "problem")
            for rows in (
                [],
                [row, row],
                [{**row, "answer": float("nan")}],
                [{**row, "text": ""}],
                [{**row, "id": True}],
                ["bad"],
            ):
                path.write_text(json.dumps(rows))
                with self.assertRaises(ValueError):
                    load_problems(path)

    def test_prompt_contract(self):
        text = "مسئله با {braces} و 17"
        for mode in ("numeric", "equation"):
            for language in ("fa", "en"):
                m = messages(text, mode, language)
                self.assertIn(f"<{text}>", m[1]["content"])
                self.assertEqual(m, messages(text, mode, language))
                self.assertNotIn("NUM_", m[1]["content"])
        self.assertIn("Do not solve", translation_messages(text)[0]["content"])
        self.assertEqual(len(prompt_hashes()), 6)
        self.assertIn("keep x = 10/2, not x = 5", templates()["equation.en.txt"])
        with self.assertRaises(ValueError):
            messages(text, "bad", "fa")

    def test_config_rejects_incompatible_settings(self):
        for updates in (
            {"temperature": 1},
            {"max_tokens": 0},
            {"mode": "bad"},
            {"max_attempts": 0},
            {"provider": ""},
            {"abs_tolerance": -1},
            {"timeout_seconds": float("inf")},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                Config(**{**self.config.to_dict(), **updates})

    def test_scoring_and_unknown_usage(self):
        self.assertTrue(correct(0.30000000001, 0.3, self.config))
        self.assertFalse(correct(None, 1, self.config))
        self.assertFalse(correct(float("inf"), 1, self.config))
        summary = summarize([], 2, self.config)
        self.assertEqual(summary["accuracy_percent"], 0)
        self.assertEqual(summary["status"], "partial")
        self.assertIsNone(summary["accuracy_ci95_wilson_percent"])
        self.assertIsNone(usage_summary([{}])["cost"]["reported_sum"])
        self.assertEqual(
            usage_summary([{"metadata": {"usage": {"cost": 0}}}])["cost"]["reported_sum"], 0
        )
        self.assertEqual(distribution([1, 2, 100])["p95"], 100)
        self.assertLess(wilson(1, 2)[0], 50)
        self.assertGreater(wilson(1, 2)[1], 50)
