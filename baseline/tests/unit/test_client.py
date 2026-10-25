import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from pmwp_baseline.client import APIError, OpenRouterClient
from pmwp_baseline.config import Config


class Response(io.BytesIO):
    pass


def response(content="x = 80"):
    return Response(
        json.dumps(
            {
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "model": "test/model",
                "provider": "test-provider",
                "usage": {"total_tokens": 10},
            }
        ).encode()
    )


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("test/model", "numeric", "fa", "test-provider")

    def test_payload_and_retry(self):
        requests, sleeps = [], []

        def opener(request, **kwargs):
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 429, "limited", {"Retry-After": "2"}, None
                )
            return response()

        client = OpenRouterClient(
            self.config, api_key="test-secret", opener=opener, sleep=sleeps.append
        )
        result = client.complete([{"role": "user", "content": "problem"}])
        self.assertEqual(result.metadata["attempts"], 2)
        self.assertEqual(sleeps, [2])
        payload = json.loads(requests[0].data)
        self.assertEqual(payload["temperature"], 0)
        self.assertFalse(payload["provider"]["allow_fallbacks"])
        self.assertEqual(payload["provider"]["only"], ["test-provider"])
        self.assertNotIn("test-secret", json.dumps(result.metadata))

    def test_auth_error_does_not_retry_or_leak(self):
        calls = []

        def opener(request, **kwargs):
            calls.append(1)
            raise urllib.error.HTTPError(request.full_url, 401, "test-secret", {}, None)

        with self.assertRaises(APIError) as raised:
            OpenRouterClient(self.config, api_key="test-secret", opener=opener).complete([])
        self.assertEqual(calls, [1])
        self.assertEqual(str(raised.exception), "http_401")

    def test_retry_exhaustion_and_bad_response(self):
        def failing(request, **kwargs):
            raise TimeoutError("test-secret")

        with self.assertRaises(APIError) as raised:
            OpenRouterClient(
                self.config, api_key="test", opener=failing, sleep=lambda _: None
            ).complete([])
        self.assertEqual(raised.exception.attempts, 3)
        for body in (
            b"{}",
            b"not json",
            b'{"choices":null}',
            b'{"choices":[{"message":{"content":null}}]}',
        ):
            with self.subTest(body=body), self.assertRaises(APIError):
                OpenRouterClient(
                    self.config, api_key="test", opener=lambda *a, **k: Response(body)
                ).complete([])

    def test_missing_key(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(ValueError):
            OpenRouterClient(self.config)

    def test_null_content_preserves_evidence_without_retry(self):
        body = {
            "id": "mock",
            "choices": [{"message": {"content": None}, "finish_reason": "length"}],
            "usage": {"completion_tokens": 2048},
        }
        requests = []

        def opener(request, **kwargs):
            requests.append(request)
            return Response(json.dumps(body).encode())

        with self.assertRaises(APIError) as raised:
            OpenRouterClient(self.config, api_key="test", opener=opener).complete([])
        self.assertEqual(len(requests), 1)
        self.assertEqual(raised.exception.metadata["usage"]["completion_tokens"], 2048)
        self.assertEqual(raised.exception.metadata["choices"][0]["finish_reason"], "length")
