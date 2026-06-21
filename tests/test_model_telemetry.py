# encoding:utf-8
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FakeOpenAIClient:
    def __init__(self, *, chunks=None, response=None):
        self.chunks = list(chunks or [])
        self.response = response or {}
        self.calls = []

    def chat_completions(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            def generate():
                for chunk in self.chunks:
                    yield chunk
            return generate()
        return self.response


class FakeTelemetryBot:
    @staticmethod
    def build(client, *, provider="openai", model="gpt-5.5"):
        from models.openai_compatible_bot import OpenAICompatibleBot

        class Bot(OpenAICompatibleBot):
            def get_api_config(self):
                return {
                    "provider": provider,
                    "api_key": "test-key",
                    "api_base": "https://api.openai.com/v1",
                    "model": model,
                    "default_temperature": 0.7,
                    "default_top_p": 0.9,
                    "default_frequency_penalty": 0.2,
                    "default_presence_penalty": 0.1,
                }

            def _get_http_client(self):
                return client

        return Bot()


class TestModelTelemetry(unittest.TestCase):
    def setUp(self):
        from models.model_telemetry import reset_model_call_telemetry_for_tests

        reset_model_call_telemetry_for_tests()

    def test_usage_normalization_extracts_reasoning_and_cached_tokens(self):
        from models.model_telemetry import normalize_usage_tokens

        usage = normalize_usage_tokens({
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "prompt_tokens_details": {"cached_tokens": 5},
            "completion_tokens_details": {"reasoning_tokens": 3},
        })

        self.assertEqual(usage["input_tokens"], 11)
        self.assertEqual(usage["output_tokens"], 7)
        self.assertEqual(usage["total_tokens"], 18)
        self.assertEqual(usage["reasoning_tokens"], 3)
        self.assertEqual(usage["cached_tokens"], 5)

    def test_openai_compatible_stream_records_latency_usage_and_provider(self):
        from models.model_telemetry import get_recent_model_calls

        chunks = [
            {"choices": [{"delta": {"content": "hello"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 1},
                    "completion_tokens_details": {"reasoning_tokens": 4},
                },
            },
        ]
        client = FakeOpenAIClient(chunks=chunks)
        bot = FakeTelemetryBot.build(client)

        result = list(bot.call_with_tools(
            [{"role": "user", "content": "hi"}],
            stream=True,
        ))

        self.assertEqual(result, chunks)
        events = get_recent_model_calls()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["provider"], "openai")
        self.assertEqual(event["model"], "gpt-5.5")
        self.assertTrue(event["stream"])
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["retry_count"], 0)
        self.assertIsNotNone(event["first_token_latency_ms"])
        self.assertGreaterEqual(event["total_latency_ms"], event["first_token_latency_ms"])
        self.assertEqual(event["input_tokens"], 3)
        self.assertEqual(event["output_tokens"], 2)
        self.assertEqual(event["total_tokens"], 5)
        self.assertEqual(event["reasoning_tokens"], 4)
        self.assertEqual(event["cached_tokens"], 1)

    def test_openai_compatible_stream_error_records_taxonomy(self):
        from models.model_telemetry import get_recent_model_calls

        error_chunk = {
            "error": {
                "message": "rate limit exceeded",
                "code": "rate_limit_exceeded",
                "type": "requests",
            },
            "message": "rate limit exceeded",
            "status_code": 429,
        }
        client = FakeOpenAIClient(chunks=[error_chunk])
        bot = FakeTelemetryBot.build(client)

        result = list(bot.call_with_tools(
            [{"role": "user", "content": "hi"}],
            stream=True,
        ))

        self.assertEqual(result, [error_chunk])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_taxonomy"], "rate_limit")
        self.assertEqual(event["error_status_code"], 429)
        self.assertEqual(event["error_code"], "rate_limit_exceeded")
        self.assertIsNone(event["first_token_latency_ms"])

    def test_stream_close_after_first_chunk_records_cancelled_once(self):
        from models.model_telemetry import get_recent_model_calls

        chunks = [
            {"choices": [{"delta": {"content": "partial"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        ]
        client = FakeOpenAIClient(chunks=chunks)
        bot = FakeTelemetryBot.build(client)

        stream = bot.call_with_tools(
            [{"role": "user", "content": "hi"}],
            stream=True,
        )
        self.assertEqual(next(stream), chunks[0])
        stream.close()
        stream.close()

        events = get_recent_model_calls()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["status"], "cancelled")
        self.assertEqual(event["error_taxonomy"], "cancelled")
        self.assertIsNotNone(event["first_token_latency_ms"])

    def test_openai_compatible_sync_records_usage_and_retry_count(self):
        from models.model_telemetry import get_recent_model_calls

        response = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
            },
        }
        client = FakeOpenAIClient(response=response)
        bot = FakeTelemetryBot.build(client, provider="custom", model="custom-model")

        result = bot.call_with_tools(
            [{"role": "user", "content": "hi"}],
            stream=False,
            retry_count=2,
        )

        self.assertEqual(result, response)
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "custom")
        self.assertEqual(event["model"], "custom-model")
        self.assertFalse(event["stream"])
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["retry_count"], 2)
        self.assertEqual(event["input_tokens"], 10)
        self.assertEqual(event["output_tokens"], 5)
        self.assertEqual(event["total_tokens"], 15)

    def test_error_classifier_prefers_context_overflow(self):
        from models.model_telemetry import classify_model_error

        self.assertEqual(
            classify_model_error(
                status_code=400,
                message="This model's maximum context length was exceeded",
            ),
            "context_overflow",
        )


if __name__ == "__main__":
    unittest.main()
