# encoding:utf-8
import os
import sys
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FakeOpenAIClient:
    def __init__(self, *, chunks=None, response=None, chunk_sequences=None, response_sequence=None):
        self.chunks = list(chunks or [])
        self.chunk_sequences = [list(seq) for seq in (chunk_sequences or [])]
        self.response = response or {}
        self.response_sequence = list(response_sequence or [])
        self.calls = []

    def chat_completions(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            if self.chunk_sequences:
                chunks = self.chunk_sequences.pop(0)
            else:
                chunks = self.chunks
            def generate():
                for chunk in chunks:
                    yield chunk
            return generate()
        if self.response_sequence:
            return self.response_sequence.pop(0)
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

        gemini_usage = normalize_usage_tokens({
            "promptTokenCount": 11,
            "candidatesTokenCount": 13,
            "totalTokenCount": 24,
            "thoughtsTokenCount": 7,
            "cachedContentTokenCount": 5,
        })
        self.assertEqual(gemini_usage["input_tokens"], 11)
        self.assertEqual(gemini_usage["output_tokens"], 13)
        self.assertEqual(gemini_usage["total_tokens"], 24)
        self.assertEqual(gemini_usage["reasoning_tokens"], 7)
        self.assertEqual(gemini_usage["cached_tokens"], 5)

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
            model_max_retries=0,
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
            model_max_retries=0,
        ))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["message"], "rate limit exceeded")
        self.assertEqual(result[0]["status_code"], 429)
        self.assertEqual(result[0]["error_taxonomy"], "rate_limit")
        self.assertTrue(result[0]["retryable"])
        self.assertTrue(result[0]["retry_exhausted"])
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

    def test_sync_retries_rate_limit_with_retry_after_then_records_success_attempt(self):
        from models.model_telemetry import get_recent_model_calls

        sleeps = []
        client = FakeOpenAIClient(response_sequence=[
            {
                "error": {
                    "message": "rate limit",
                    "code": "rate_limit_exceeded",
                    "type": "rate_limit",
                },
                "message": "rate limit",
                "status_code": 429,
                "retry_after": "3",
            },
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6},
            },
        ])
        bot = FakeTelemetryBot.build(client)

        result = bot.call_with_tools(
            [{"role": "user", "content": "hi"}],
            stream=False,
            model_max_retries=2,
            model_retry_sleep=sleeps.append,
        )

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(sleeps, [3.0])
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "completed"])
        self.assertEqual([event["retry_count"] for event in events], [0, 1])
        self.assertEqual(events[0]["error_taxonomy"], "rate_limit")
        self.assertEqual(events[1]["total_tokens"], 6)

    def test_stream_retries_before_first_token_and_hides_retry_error_chunk(self):
        from models.model_telemetry import get_recent_model_calls

        sleeps = []
        error_chunk = {
            "error": {"message": "server unavailable", "code": "", "type": ""},
            "message": "server unavailable",
            "status_code": 503,
            "retry_after": "0.25",
        }
        success_chunks = [
            {"choices": [{"delta": {"content": "ok"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        ]
        client = FakeOpenAIClient(chunk_sequences=[[error_chunk], success_chunks])
        bot = FakeTelemetryBot.build(client)

        result = list(bot.call_with_tools(
            [{"role": "user", "content": "hi"}],
            stream=True,
            model_max_retries=1,
            model_retry_sleep=sleeps.append,
        ))

        self.assertEqual(result, success_chunks)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(sleeps, [0.25])
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "completed"])
        self.assertEqual([event["retry_count"] for event in events], [0, 1])
        self.assertEqual(events[0]["error_taxonomy"], "server_error")

    def test_stream_does_not_retry_after_first_token(self):
        from models.model_telemetry import get_recent_model_calls

        sleeps = []
        content_chunk = {"choices": [{"delta": {"content": "partial"}}]}
        error_chunk = {
            "error": {"message": "server unavailable", "code": "", "type": ""},
            "message": "server unavailable",
            "status_code": 503,
        }
        client = FakeOpenAIClient(chunks=[content_chunk, error_chunk])
        bot = FakeTelemetryBot.build(client)

        result = list(bot.call_with_tools(
            [{"role": "user", "content": "hi"}],
            stream=True,
            model_max_retries=2,
            model_retry_sleep=sleeps.append,
        ))

        self.assertEqual(result[0], content_chunk)
        self.assertEqual(result[1]["status_code"], 503)
        self.assertTrue(result[1]["retryable"])
        self.assertTrue(result[1]["retry_suppressed"])
        self.assertEqual(result[1]["retry_suppressed_reason"], "stream_output_started")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(sleeps, [])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_taxonomy"], "server_error")
        self.assertIsNotNone(event["first_token_latency_ms"])

    def test_stream_non_retryable_400_fails_closed_with_typed_evidence(self):
        from models.model_telemetry import get_recent_model_calls

        sleeps = []
        error_chunk = {
            "error": {"message": "invalid request", "code": "invalid_request", "type": "invalid_request"},
            "message": "invalid request",
            "status_code": 400,
        }
        client = FakeOpenAIClient(chunks=[error_chunk])
        bot = FakeTelemetryBot.build(client)

        result = list(bot.call_with_tools(
            [{"role": "user", "content": "hi"}],
            stream=True,
            model_max_retries=2,
            model_retry_sleep=sleeps.append,
        ))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status_code"], 400)
        self.assertEqual(result[0]["error_taxonomy"], "client_error")
        self.assertFalse(result[0]["retryable"])
        self.assertEqual(result[0]["retry_attempt"], 0)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(sleeps, [])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_taxonomy"], "client_error")

    def test_sync_non_retryable_400_fails_closed_with_typed_evidence(self):
        from models.model_telemetry import get_recent_model_calls

        sleeps = []
        client = FakeOpenAIClient(response={
            "error": {"message": "invalid request", "code": "invalid_request", "type": "invalid_request"},
            "message": "invalid request",
            "status_code": 400,
        })
        bot = FakeTelemetryBot.build(client)

        result = bot.call_with_tools(
            [{"role": "user", "content": "hi"}],
            stream=False,
            model_max_retries=2,
            model_retry_sleep=sleeps.append,
        )

        self.assertEqual(result["status_code"], 400)
        self.assertEqual(result["error_taxonomy"], "client_error")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["retry_attempt"], 0)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(sleeps, [])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_taxonomy"], "client_error")

    def test_agent_bridge_forwards_retry_count_and_cancelable_sleep(self):
        from agent.protocol.models import LLMRequest
        from bridge.agent_bridge import AgentLLMModel

        captured = {}

        class CaptureBot:
            def call_with_tools(self, **kwargs):
                captured.update(kwargs)
                return iter([])

        class CaptureModel(AgentLLMModel):
            def __init__(self):
                self._capture_bot = CaptureBot()

            @property
            def bot(self):
                return self._capture_bot

            @property
            def model(self):
                return "gpt-5.5"

        def retry_sleep(delay):
            return None

        model = CaptureModel()
        request = LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            retry_count=3,
            model_retry_sleep=retry_sleep,
        )

        self.assertEqual(list(model.call_stream(request)), [])
        self.assertEqual(captured["retry_count"], 3)
        self.assertIs(captured["model_retry_sleep"], retry_sleep)

    def _native_agent_model(self, bot, *, model_name="glm-4", provider="zhipu_ai"):
        from bridge.agent_bridge import AgentLLMModel

        class NativeModel(AgentLLMModel):
            def __init__(self):
                self._capture_bot = bot

            @property
            def bot(self):
                return self._capture_bot

            @property
            def model(self):
                return model_name

            def _resolve_bot_type(self, _model_name):
                return provider

        return NativeModel()

    def test_agent_bridge_native_sync_gateway_retries_and_records_telemetry(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        class NativeBot:
            def __init__(self):
                self.calls = []
                self.responses = [
                    {
                        "error": {"message": "rate limit", "code": "rate_limit_exceeded"},
                        "message": "rate limit",
                        "status_code": 429,
                        "retry_after": "0.25",
                    },
                    {
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {
                            "prompt_tokens": 3,
                            "completion_tokens": 4,
                            "total_tokens": 7,
                        },
                    },
                ]

            def call_with_tools(self, **kwargs):
                self.calls.append(kwargs)
                return self.responses.pop(0)

        sleeps = []
        bot = NativeBot()
        model = self._native_agent_model(bot)
        result = model.call(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            retry_count=2,
            model_max_retries=1,
            model_retry_sleep=sleeps.append,
        ))

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual([call["retry_count"] for call in bot.calls], [2, 3])
        self.assertEqual(sleeps, [0.25])
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "completed"])
        self.assertEqual([event["retry_count"] for event in events], [2, 3])
        self.assertEqual(events[0]["provider"], "zhipu_ai")
        self.assertEqual(events[0]["api_path"], "/native/call_with_tools")
        self.assertEqual(events[0]["error_taxonomy"], "rate_limit")
        self.assertEqual(events[1]["total_tokens"], 7)

    def test_agent_bridge_native_sync_gateway_fails_closed_on_non_retryable_4xx(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        class NativeBot:
            def __init__(self):
                self.calls = []

            def call_with_tools(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "error": {"message": "invalid request", "code": "bad_request"},
                    "message": "invalid request",
                    "status_code": 400,
                }

        sleeps = []
        bot = NativeBot()
        model = self._native_agent_model(bot)
        result = model.call(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            model_max_retries=2,
            model_retry_sleep=sleeps.append,
        ))

        self.assertEqual(result["status_code"], 400)
        self.assertEqual(result["error_taxonomy"], "client_error")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["retry_attempt"], 0)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(sleeps, [])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_taxonomy"], "client_error")

    def test_agent_bridge_native_sync_gateway_accepts_single_response_generator(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        class NativeGeneratorBot:
            def call_with_tools(self, **kwargs):
                def generate():
                    yield {
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {
                            "prompt_tokens": 5,
                            "completion_tokens": 6,
                            "total_tokens": 11,
                        },
                    }

                return generate()

        model = self._native_agent_model(NativeGeneratorBot(), model_name="deepseek-v4", provider="deepseek")
        result = model.call(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            model_max_retries=0,
        ))

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "deepseek")
        self.assertFalse(event["stream"])
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["total_tokens"], 11)

    def test_agent_bridge_native_sync_generator_exception_uses_retry_policy(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        class NativeGeneratorBot:
            def __init__(self):
                self.calls = []

            def call_with_tools(self, **kwargs):
                self.calls.append(kwargs)

                def generate():
                    raise TimeoutError("request timeout")
                    yield {}

                return generate()

        sleeps = []
        bot = NativeGeneratorBot()
        model = self._native_agent_model(bot, model_name="deepseek-v4", provider="deepseek")
        result = model.call(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            model_max_retries=1,
            model_retry_sleep=sleeps.append,
        ))

        self.assertEqual(len(bot.calls), 2)
        self.assertEqual([call["retry_count"] for call in bot.calls], [0, 1])
        self.assertEqual(sleeps, [2.0])
        self.assertTrue(result["error"])
        self.assertEqual(result["error_taxonomy"], "timeout")
        self.assertTrue(result["retryable"])
        self.assertTrue(result["retry_exhausted"])
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "failed"])
        self.assertEqual([event["error_taxonomy"] for event in events], ["timeout", "timeout"])

    def test_agent_bridge_native_stream_gateway_retries_before_first_token(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        class NativeStreamBot:
            def __init__(self):
                self.calls = []
                self.sequences = [
                    [{
                        "error": {"message": "server unavailable"},
                        "message": "server unavailable",
                        "status_code": 503,
                        "retry_after": "0.5",
                    }],
                    [
                        {"choices": [{"delta": {"content": "ok"}}]},
                        {
                            "choices": [{"delta": {}, "finish_reason": "stop"}],
                            "usage": {
                                "promptTokenCount": 2,
                                "candidatesTokenCount": 3,
                                "totalTokenCount": 5,
                            },
                        },
                    ],
                ]

            def call_with_tools(self, **kwargs):
                self.calls.append(kwargs)
                return iter(self.sequences.pop(0))

        sleeps = []
        bot = NativeStreamBot()
        model = self._native_agent_model(bot, model_name="gemini-3.5-flash", provider="gemini")
        result = list(model.call_stream(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            model_max_retries=1,
            model_retry_sleep=sleeps.append,
        )))

        self.assertEqual(result[0]["choices"][0]["delta"]["content"], "ok")
        self.assertEqual(len(result), 2)
        self.assertEqual(len(bot.calls), 2)
        self.assertEqual(sleeps, [0.5])
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "completed"])
        self.assertEqual(events[0]["provider"], "gemini")
        self.assertEqual(events[0]["error_taxonomy"], "server_error")
        self.assertEqual(events[1]["first_token_latency_ms"] is not None, True)
        self.assertEqual(events[1]["total_tokens"], 5)

    def test_agent_bridge_native_stream_exception_after_first_token_is_retry_suppressed(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        class RaisesAfterContent:
            def __init__(self):
                self.index = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.index += 1
                if self.index == 1:
                    return {"choices": [{"delta": {"content": "partial"}}]}
                raise ConnectionError("connection reset by peer")

        class NativeStreamBot:
            def __init__(self):
                self.calls = []

            def call_with_tools(self, **kwargs):
                self.calls.append(kwargs)
                return RaisesAfterContent()

        sleeps = []
        bot = NativeStreamBot()
        model = self._native_agent_model(bot, model_name="moonshot-v1-8k", provider="moonshot")
        result = list(model.call_stream(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            model_max_retries=2,
            model_retry_sleep=sleeps.append,
        )))

        self.assertEqual(result[0]["choices"][0]["delta"]["content"], "partial")
        self.assertTrue(result[1]["error"])
        self.assertTrue(result[1]["retryable"])
        self.assertTrue(result[1]["retry_suppressed"])
        self.assertEqual(result[1]["retry_suppressed_reason"], "stream_output_started")
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(sleeps, [])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_taxonomy"], "network_error")
        self.assertIsNotNone(event["first_token_latency_ms"])

    def test_agent_bridge_native_stream_exception_before_output_exhausts_with_typed_marker(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        class RaisesImmediately:
            def __iter__(self):
                return self

            def __next__(self):
                raise TimeoutError("request timeout")

        class NativeStreamBot:
            def __init__(self):
                self.calls = []

            def call_with_tools(self, **kwargs):
                self.calls.append(kwargs)
                return RaisesImmediately()

        sleeps = []
        bot = NativeStreamBot()
        model = self._native_agent_model(bot, model_name="qwen3.7-plus", provider="dashscope")
        result = list(model.call_stream(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            model_max_retries=1,
            model_retry_sleep=sleeps.append,
        )))

        self.assertEqual(len(bot.calls), 2)
        self.assertEqual([call["retry_count"] for call in bot.calls], [0, 1])
        self.assertEqual(sleeps, [2.0])
        self.assertTrue(result[0]["error"])
        self.assertEqual(result[0]["error_taxonomy"], "timeout")
        self.assertTrue(result[0]["retryable"])
        self.assertTrue(result[0]["retry_exhausted"])
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "failed"])
        self.assertEqual([event["error_taxonomy"] for event in events], ["timeout", "timeout"])

    def test_agent_bridge_native_stream_close_records_cancelled_once(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        class ClosableIterator:
            def __init__(self, chunks):
                self._iter = iter(chunks)
                self.closed = 0

            def __iter__(self):
                return self

            def __next__(self):
                return next(self._iter)

            def close(self):
                self.closed += 1

        class NativeStreamBot:
            def __init__(self):
                self.stream = ClosableIterator([
                    {"choices": [{"delta": {"content": "partial"}}]},
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                ])

            def call_with_tools(self, **kwargs):
                return self.stream

        bot = NativeStreamBot()
        model = self._native_agent_model(bot, model_name="glm-4", provider="zhipu_ai")

        stream = model.call_stream(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        ))
        self.assertEqual(next(stream)["choices"][0]["delta"]["content"], "partial")
        stream.close()
        stream.close()

        events = get_recent_model_calls()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "cancelled")
        self.assertEqual(events[0]["error_taxonomy"], "cancelled")
        self.assertEqual(events[0]["first_token_latency_ms"] is not None, True)
        self.assertGreaterEqual(bot.stream.closed, 1)

    def test_agent_bridge_does_not_double_wrap_shared_openai_gateway(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        chunks = [
            {"choices": [{"delta": {"content": "ok"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        ]
        client = FakeOpenAIClient(chunks=chunks)
        bot = FakeTelemetryBot.build(client)
        model = self._native_agent_model(bot, model_name="gpt-5.5", provider="openai")

        result = list(model.call_stream(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )))

        self.assertEqual(result, chunks)
        self.assertEqual(len(client.calls), 1)
        events = get_recent_model_calls()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["api_path"], "/chat/completions")

    def test_agent_bridge_native_call_suppresses_inner_legacy_reply_span(self):
        from agent.protocol.models import LLMRequest
        from models.legacy_reply_gateway import wrap_legacy_reply_text
        from models.model_telemetry import get_recent_model_calls

        class NativeBot:
            def __init__(self):
                self.reply_calls = []

            def reply_text(self, session, retry_count=0):
                self.reply_calls.append(retry_count)
                return {
                    "total_tokens": 7,
                    "completion_tokens": 3,
                    "content": "ok",
                }

            def call_with_tools(self, **kwargs):
                return self.reply_text(
                    SimpleNamespace(),
                    retry_count=kwargs.get("retry_count", 0),
                )

        bot = wrap_legacy_reply_text(NativeBot(), provider_hint="modelscope")
        model = self._native_agent_model(bot, model_name="modelscope-agent", provider="modelscope")
        result = model.call(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            model_max_retries=0,
        ))

        self.assertEqual(result["content"], "ok")
        self.assertEqual(bot.reply_calls, [0])
        events = get_recent_model_calls()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["api_path"], "/native/call_with_tools")
        self.assertEqual(events[0]["provider"], "modelscope")

    def test_legacy_reply_text_gateway_records_one_span_for_internal_retry(self):
        from models.legacy_reply_gateway import LEGACY_REPLY_TEXT_API_PATH, wrap_legacy_reply_text
        from models.model_telemetry import get_recent_model_calls

        class RecursiveLegacyBot:
            def __init__(self):
                self.calls = []

            def get_api_config(self):
                return {"provider": "legacy-provider", "model": "legacy-model"}

            def reply_text(self, session, retry_count=0):
                self.calls.append(retry_count)
                if retry_count == 0:
                    return self.reply_text(session, retry_count=1)
                return {
                    "total_tokens": 9,
                    "completion_tokens": 4,
                    "content": "ok",
                }

        bot = wrap_legacy_reply_text(RecursiveLegacyBot())
        result = bot.reply_text(SimpleNamespace())

        self.assertEqual(result["content"], "ok")
        self.assertEqual(bot.calls, [0, 1])
        events = get_recent_model_calls()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["provider"], "legacy-provider")
        self.assertEqual(event["model"], "legacy-model")
        self.assertEqual(event["api_path"], LEGACY_REPLY_TEXT_API_PATH)
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["retry_count"], 0)
        self.assertEqual(event["input_tokens"], 5)
        self.assertEqual(event["output_tokens"], 4)
        self.assertEqual(event["total_tokens"], 9)

    def test_legacy_reply_text_gateway_records_failure_sentinel(self):
        from models.legacy_reply_gateway import wrap_legacy_reply_text
        from models.model_telemetry import get_recent_model_calls

        class ErrorLegacyBot:
            def reply_text(self, session):
                return {
                    "completion_tokens": 0,
                    "content": "rate limit exceeded",
                    "status_code": 429,
                }

        bot = wrap_legacy_reply_text(
            ErrorLegacyBot(),
            provider_hint="custom",
            model_hint="legacy-chat",
        )
        result = bot.reply_text(SimpleNamespace())

        self.assertEqual(result["content"], "rate limit exceeded")
        events = get_recent_model_calls()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["provider"], "custom")
        self.assertEqual(event["model"], "legacy-chat")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_taxonomy"], "rate_limit")
        self.assertEqual(event["error_status_code"], 429)
        self.assertEqual(event["error_message"], "rate limit exceeded")

    def test_legacy_reply_text_gateway_treats_empty_completion_as_failed(self):
        from models.legacy_reply_gateway import wrap_legacy_reply_text
        from models.model_telemetry import get_recent_model_calls

        class EmptyLegacyBot:
            def reply_text(self, session):
                return {"completion_tokens": 0, "content": ""}

        bot = wrap_legacy_reply_text(EmptyLegacyBot(), provider_hint="legacy")
        self.assertEqual(bot.reply_text(SimpleNamespace())["content"], "")

        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(
            event["error_message"],
            "Legacy reply_text returned no completion tokens",
        )

    def test_legacy_reply_text_gateway_allows_zero_token_text_success(self):
        from models.legacy_reply_gateway import wrap_legacy_reply_text
        from models.model_telemetry import get_recent_model_calls

        class ZeroTokenTextBot:
            def reply_text(self, session):
                return {
                    "total_tokens": 0,
                    "completion_tokens": 0,
                    "content": "thinking text",
                }

        bot = wrap_legacy_reply_text(ZeroTokenTextBot(), provider_hint="modelscope")
        self.assertEqual(bot.reply_text(SimpleNamespace())["content"], "thinking text")

        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "modelscope")
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["error_taxonomy"], "")

    def test_legacy_reply_text_gateway_modelscope_fallback_without_total_tokens_fails(self):
        from models.legacy_reply_gateway import wrap_legacy_reply_text
        from models.model_telemetry import get_recent_model_calls

        class ModelScopeFallbackBot:
            def reply_text(self, session):
                return {
                    "completion_tokens": 0,
                    "content": "Please try again later",
                }

        bot = wrap_legacy_reply_text(ModelScopeFallbackBot(), provider_hint="modelscope")
        self.assertEqual(bot.reply_text(SimpleNamespace())["content"], "Please try again later")

        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "modelscope")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_message"], "Please try again later")

    def test_legacy_reply_text_gateway_modelscope_status_error_overrides_text_success(self):
        from models.legacy_reply_gateway import wrap_legacy_reply_text
        from models.model_telemetry import get_recent_model_calls

        class ModelScopeRateLimitBot:
            def reply_text(self, session):
                return {
                    "total_tokens": 0,
                    "completion_tokens": 0,
                    "content": "rate limit exceeded",
                    "status_code": 429,
                }

        bot = wrap_legacy_reply_text(ModelScopeRateLimitBot(), provider_hint="modelscope")
        self.assertEqual(bot.reply_text(SimpleNamespace())["content"], "rate limit exceeded")

        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_taxonomy"], "rate_limit")
        self.assertEqual(event["error_status_code"], 429)

    def test_legacy_reply_text_gateway_non_modelscope_zero_token_text_fails(self):
        from models.legacy_reply_gateway import wrap_legacy_reply_text
        from models.model_telemetry import get_recent_model_calls

        class DeepSeekStyleErrorBot:
            def reply_text(self, session):
                return {
                    "completion_tokens": 0,
                    "content": "Please try again later",
                }

        bot = wrap_legacy_reply_text(DeepSeekStyleErrorBot(), provider_hint="deepseek")
        self.assertEqual(bot.reply_text(SimpleNamespace())["content"], "Please try again later")

        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "deepseek")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_taxonomy"], "unknown")
        self.assertEqual(event["error_message"], "Please try again later")

    def test_agent_stream_retry_stopped_marker_skips_outer_retry(self):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class MarkerModel:
            def __init__(self):
                self.calls = 0
                self.request = None

            def call_stream(self, request):
                self.calls += 1
                self.request = request
                yield {
                    "error": {"message": "server unavailable", "code": "", "type": ""},
                    "message": "server unavailable",
                    "status_code": 503,
                    "retry_suppressed": True,
                    "retry_suppressed_reason": "stream_output_started",
                }

        model = MarkerModel()
        agent = SimpleNamespace(last_usage=None, memory_manager=None)
        executor = AgentStreamExecutor(
            agent=agent,
            model=model,
            system_prompt="",
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )
        executor._validate_and_fix_messages = lambda: None
        executor._prepare_messages = lambda: [{"role": "user", "content": "hi"}]
        executor._identify_complete_turns = lambda: []

        with self.assertRaises(Exception) as ctx:
            executor._call_llm_stream(max_retries=3)

        self.assertEqual(model.calls, 1)
        self.assertEqual(model.request.retry_count, 0)
        self.assertTrue(callable(model.request.model_retry_sleep))
        self.assertIs(model.request.model_retry_sleep.__self__, executor)
        self.assertIs(
            model.request.model_retry_sleep.__func__,
            executor._sleep_cancelable.__func__,
        )
        self.assertNotIn("MODEL_RETRY_EXHAUSTED", str(ctx.exception))
        self.assertIn("server unavailable", str(ctx.exception))

    def test_error_classifier_prefers_context_overflow(self):
        from models.model_telemetry import classify_model_error

        self.assertEqual(
            classify_model_error(
                status_code=400,
                message="This model's maximum context length was exceeded",
            ),
            "context_overflow",
        )

    def test_retry_after_parser_accepts_seconds(self):
        from models.model_retry import parse_retry_after

        self.assertEqual(parse_retry_after("2.5"), 2.5)

    def test_retry_after_parser_accepts_http_dates(self):
        from models.model_retry import parse_retry_after

        now = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
        future = datetime(2026, 6, 21, 12, 0, 5, tzinfo=timezone.utc)
        past = datetime(2026, 6, 21, 11, 59, 59, tzinfo=timezone.utc)

        self.assertEqual(parse_retry_after(format_datetime(future, usegmt=True), now=now), 5.0)
        self.assertEqual(parse_retry_after(format_datetime(past, usegmt=True), now=now), 0.0)


if __name__ == "__main__":
    unittest.main()
