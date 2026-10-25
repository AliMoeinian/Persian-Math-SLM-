import http.client
import io
import json
import unittest
import urllib.error

from pmwp_baseline.client import APIError, OpenRouterClient
from pmwp_baseline.config import Config
from pmwp_baseline.telemetry import attempts_for, distribution, problem_timing, usage_summary


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class TimedResponse(io.BytesIO):
    def __init__(self, body, clock):
        super().__init__(json.dumps(body).encode())
        self.clock = clock

    def read(self, *args):
        self.clock.advance(3)
        return super().read(*args)


def body(content="x = 999", usage=None):
    return {
        "id": "mock",
        "model": "test/model",
        "provider": "provider",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": usage or {},
    }


class TimingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.config = Config("test/model", "numeric", "fa", "provider")

    def test_retry_accounting_exact_duration_and_http_error_usage(self):
        requests = []

        def opener(request, **kwargs):
            requests.append(request)
            if len(requests) == 1:
                self.clock.advance(1)
                raise urllib.error.HTTPError(
                    request.full_url,
                    502,
                    "secret",
                    {"Retry-After": "2"},
                    io.BytesIO(
                        json.dumps(
                            {
                                "usage": {
                                    "prompt_tokens": 10,
                                    "completion_tokens": 20,
                                    "total_tokens": 30,
                                    "cost": 0.02,
                                }
                            }
                        ).encode()
                    ),
                )
            self.clock.advance(2)
            return TimedResponse(
                body(
                    usage={
                        "prompt_tokens": 20,
                        "completion_tokens": 30,
                        "total_tokens": 50,
                        "cost": 0.03,
                    }
                ),
                self.clock,
            )

        result = OpenRouterClient(
            self.config, api_key="secret", opener=opener, sleep=self.clock.advance, clock=self.clock
        ).complete([])
        metadata = result.metadata
        self.assertEqual(metadata["latency_seconds"], 8)
        self.assertEqual(metadata["timing"]["retry_wait_seconds"], 2)
        records = metadata["attempt_records"]
        self.assertEqual([r["response_seconds"] for r in records], [1, 5])
        self.assertEqual([r["outcome"] for r in records], ["failure", "success"])
        # The received prediction is mathematically wrong; it is still an API success.
        self.assertEqual(result.content, "x = 999")
        summary = usage_summary([{"metadata": metadata, "attempts": 2, "latency_seconds": 8}])
        self.assertEqual(summary["total_tokens"]["reported_sum"], 80)
        self.assertAlmostEqual(summary["cost"]["reported_sum"], 0.05)
        self.assertEqual(summary["attempts"], 2)
        self.assertEqual(summary["timing"]["request_seconds"]["success"]["mean"], 8)
        self.assertEqual(summary["timing"]["attempt_response_seconds"]["failure"]["mean"], 1)
        self.assertEqual(summary["timing"]["attempt_response_seconds"]["success"]["mean"], 5)
        self.assertNotIn("secret", json.dumps(metadata))
        json.dumps(metadata, allow_nan=False)  # No circular evidence structures.

    def test_null_content_no_retry_but_usage_and_finish_are_retained(self):
        calls = []

        def opener(*args, **kwargs):
            calls.append(1)
            value = body(None, {"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6})
            value["choices"][0]["finish_reason"] = "length"
            return TimedResponse(value, self.clock)

        with self.assertRaises(APIError) as caught:
            OpenRouterClient(
                self.config, api_key="secret", opener=opener, clock=self.clock
            ).complete([])
        exc = caught.exception
        self.assertEqual(len(calls), 1)
        self.assertEqual(exc.metadata["attempt_records"][0]["finish_reason"], "length")
        summary = usage_summary(
            [
                {
                    "metadata": exc.metadata,
                    "error": exc.category,
                    "attempts": exc.attempts,
                    "latency_seconds": exc.elapsed,
                }
            ]
        )
        self.assertEqual(summary["total_tokens"]["reported_sum"], 6)
        self.assertEqual(summary["timing"]["request_seconds"]["failure"]["mean"], 3)

    def test_transport_exhaustion_and_invalid_json_capture_unknown_usage(self):
        def broken(*args, **kwargs):
            self.clock.advance(1)
            raise http.client.IncompleteRead(b"partial")

        with self.assertRaises(APIError) as caught:
            OpenRouterClient(
                self.config,
                api_key="secret",
                opener=broken,
                sleep=self.clock.advance,
                clock=self.clock,
            ).complete([])
        exc = caught.exception
        self.assertEqual(exc.category, "transport_error")
        self.assertEqual(exc.elapsed, 6)  # 3 attempts + waits 1 and 2.
        summary = usage_summary([{"error": exc.category, "attempts": 3, "metadata": exc.metadata}])
        self.assertEqual(summary["total_tokens"]["missing_attempts"], 3)
        self.assertIsNone(summary["cost"]["reported_sum"])
        count = []

        def invalid(*args, **kwargs):
            count.append(1)
            return io.BytesIO(b"not json")

        with self.assertRaises(APIError) as caught:
            OpenRouterClient(self.config, api_key="secret", opener=invalid).complete([])
        self.assertEqual(caught.exception.category, "invalid_response")
        self.assertEqual(len(count), 1)
        self.assertEqual(len(caught.exception.metadata["attempt_records"]), 1)


class UsageTests(unittest.TestCase):
    def test_reasoning_and_cache_are_subsets_not_added(self):
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cost": 0,
            "prompt_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 10},
            "completion_tokens_details": {"reasoning_tokens": 30},
        }
        record = {"usage": usage, "outcome": "success"}
        result = usage_summary([{"metadata": {"attempt_records": [record]}}])
        self.assertEqual(result["total_tokens"]["reported_sum"], 150)
        self.assertEqual(result["reasoning_tokens"]["reported_sum"], 30)
        self.assertEqual(result["cached_prompt_tokens"]["reported_sum"], 40)
        self.assertTrue(result["cost"]["complete"])
        self.assertEqual(result["cost"]["reported_sum"], 0)
        self.assertEqual(result["token_consistency"]["mismatched_attempts"], 0)

    def test_absent_reasoning_is_unknown_explicit_zero_is_known(self):
        result = usage_summary(
            [
                {
                    "metadata": {
                        "attempt_records": [
                            {"usage": {}, "outcome": "failure"},
                            {
                                "usage": {"completion_tokens_details": {"reasoning_tokens": 0}},
                                "outcome": "success",
                            },
                        ]
                    }
                }
            ]
        )
        self.assertEqual(result["reasoning_tokens"]["reported_sum"], 0)
        self.assertEqual(result["reasoning_tokens"]["missing_attempts"], 1)
        self.assertFalse(result["reasoning_tokens"]["complete"])

    def test_legacy_retry_coverage_does_not_invent_timing_or_double_usage(self):
        call = {"attempts": 3, "latency_seconds": 9, "metadata": {"usage": {"total_tokens": 10}}}
        result = usage_summary([call])
        self.assertEqual(len(attempts_for(call)), 3)
        self.assertEqual(result["total_tokens"]["reported_sum"], 10)
        self.assertEqual(result["total_tokens"]["missing_attempts"], 2)
        self.assertEqual(result["timing"]["missing_attempt_response_timings"], 3)
        self.assertEqual(result["timing"]["missing_request_wait_timings"], 1)
        self.assertEqual(result["timing"]["request_seconds"]["success"]["mean"], 9)
        # Cache replay is never billed/counted twice.
        self.assertEqual(attempts_for({"metadata": {"reused_from_parent": True}}), [])

    def test_invalid_usage_and_mismatched_token_totals(self):
        records = [
            {"usage": {"total_tokens": v}, "outcome": "failure"}
            for v in (None, -1, True, float("nan"), float("inf"), "123")
        ]
        records.append(
            {
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 6},
                "outcome": "success",
            }
        )
        result = usage_summary([{"metadata": {"attempt_records": records}}])
        self.assertEqual(result["total_tokens"]["reported_sum"], 6)
        self.assertEqual(result["total_tokens"]["missing_attempts"], 6)
        self.assertEqual(result["token_consistency"]["mismatched_attempts"], 1)

    def test_percentiles_and_empty_populations(self):
        self.assertEqual(distribution([]), {"n": 0, "mean": None, "median": None, "p95": None})
        self.assertEqual(distribution(list(range(1, 21)))["p95"], 19)
        self.assertEqual(distribution([3])["p95"], 3)
        self.assertEqual(
            problem_timing([{"status": "ok", "latency_seconds": 3}])[
                "missing_initial_pass_timings"
            ],
            1,
        )


class BoundaryTests(unittest.TestCase):
    def test_json_processing_is_not_api_response_time(self):
        from unittest.mock import patch

        clock = Clock()
        config = Config("test/model", "numeric", "fa", "provider")
        original_loads = json.loads

        def slow_json(*args, **kwargs):
            clock.advance(2)
            return original_loads(*args, **kwargs)

        def opener(*args, **kwargs):
            return TimedResponse(body("x = 1"), clock)

        with patch("pmwp_baseline.client.json.loads", side_effect=slow_json):
            value = OpenRouterClient(config, api_key="secret", opener=opener, clock=clock).complete(
                []
            )
        record = value.metadata["attempt_records"][0]
        self.assertEqual(record["response_seconds"], 3)
        self.assertEqual(record["response_processing_seconds"], 2)
        self.assertEqual(value.metadata["timing"]["request_seconds"], 5)

    def test_actual_wait_not_requested_wait_and_failed_request_population(self):
        clock = Clock()
        config = Config("test/model", "numeric", "fa", "provider", max_attempts=2)

        def opener(*args, **kwargs):
            clock.advance(1)
            raise TimeoutError("secret")

        def oversleep(seconds):
            clock.advance(seconds + 1)

        with self.assertRaises(APIError) as caught:
            OpenRouterClient(
                config, api_key="secret", opener=opener, clock=clock, sleep=oversleep
            ).complete([])
        metadata = caught.exception.metadata
        self.assertEqual(metadata["attempt_records"][0]["retry_wait_planned_seconds"], 1)
        self.assertEqual(metadata["attempt_records"][0]["retry_wait_seconds"], 2)
        self.assertEqual(metadata["timing"]["request_seconds"], 4)
        summary = usage_summary([{"metadata": metadata, "error": "transport_error", "attempts": 2}])
        self.assertEqual(summary["timing"]["request_seconds"]["failure"]["n"], 1)
        self.assertEqual(summary["timing"]["request_seconds"]["success"]["n"], 0)
        self.assertEqual(summary["outcomes"]["failure"], 2)

    def test_nonfinite_provider_usage_is_persistable_and_unknown(self):
        clock = Clock()
        config = Config("test/model", "numeric", "fa", "provider")

        def opener(*args, **kwargs):
            return TimedResponse(body(usage={"cost": float("nan")}), clock)

        value = OpenRouterClient(config, api_key="secret", opener=opener, clock=clock).complete([])
        json.dumps(value.metadata, allow_nan=False)
        summary = usage_summary([{"metadata": value.metadata}])
        self.assertIsNone(summary["cost"]["reported_sum"])
        self.assertEqual(summary["cost"]["missing_attempts"], 1)
