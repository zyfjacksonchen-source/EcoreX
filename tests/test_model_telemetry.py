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

    @staticmethod
    def _fake_response(status_code, data=None, headers=None, text=None):
        from unittest.mock import MagicMock

        response = MagicMock()
        response.status_code = status_code
        response.headers = headers or {}
        response.text = text if text is not None else str(data or {})
        response.json.return_value = data or {}
        return response

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

    def _fallback_agent_model(self, bots):
        from bridge.agent_bridge import AgentLLMModel
        from models.model_fallback import ModelFallbackRoute

        class FallbackModel(AgentLLMModel):
            def __init__(self):
                self._bots = bots

            @property
            def model(self):
                return "primary-model"

            @model.setter
            def model(self, _value):
                pass

            @property
            def bot(self):
                return self._bots["primary-model"]

            def _resolve_bot_type(self, model_name):
                return "primary-provider" if model_name == "primary-model" else "fallback-provider"

            def _model_call_routes(self):
                return [
                    ModelFallbackRoute(
                        model="primary-model",
                        bot_type="primary-provider",
                        provider="primary-provider",
                        reason="primary",
                        index=0,
                    ),
                    ModelFallbackRoute(
                        model="fallback-model",
                        bot_type="fallback-provider",
                        provider="fallback-provider",
                        reason="fallback",
                        index=1,
                    ),
                ]

            def _get_bot_for_route(self, route):
                return self._bots[route.model]

        return FallbackModel()

    def _modelscope_sync_sentinel_bot(self, responses):
        from models.modelscope.modelscope_bot import ModelScopeBot

        class ModelScopeSyncSentinelBot(ModelScopeBot):
            def __init__(self, queued_responses):
                self.args = {"model": "Qwen/Qwen3.5-27B"}
                self.responses = list(queued_responses)
                self.calls = []
                self.reply_calls = []

            def reply_text(self, session, args=None, retry_count=0, allow_local_retry=True, local_retry_sleep=None):
                self.reply_calls.append({
                    "retry_count": retry_count,
                    "allow_local_retry": allow_local_retry,
                    "args": dict(args or {}),
                    "messages": list(getattr(session, "messages", []) or []),
                })
                return self.responses.pop(0)

            def call_with_tools(self, **kwargs):
                self.calls.append(kwargs)
                session = SimpleNamespace(messages=kwargs.get("messages") or [])
                return self._handle_sync_response(session, kwargs)

        return ModelScopeSyncSentinelBot(responses)

    def _real_modelscope_bot_for_tests(self):
        from models.modelscope.modelscope_bot import ModelScopeBot

        class FakeSessions:
            def session_query(self, _query, _session_id):
                return SimpleNamespace(messages=[])

        class RealModelScopeBot(ModelScopeBot):
            def __init__(self):
                self.args = {
                    "model": "Qwen/Qwen3.5-27B",
                    "temperature": 0.3,
                    "top_p": 1.0,
                }
                self.api_key = "test-key"
                self.base_url = "https://modelscope.test/v1"
                self.sessions = FakeSessions()
                self._last_context = None

        return RealModelScopeBot()

    class _FakeHTTPResponse:
        def __init__(self, status_code, payload=None, *, headers=None, text=None, lines=None, json_error=None):
            self.status_code = status_code
            self._payload = payload if payload is not None else {}
            self.headers = headers or {}
            self.text = text if text is not None else str(self._payload)
            self._lines = list(lines or [])
            self._json_error = json_error

        def json(self):
            if self._json_error is not None:
                raise self._json_error
            return self._payload

        def iter_lines(self):
            return iter(self._lines)

    def _native_http_provider_bot_for_tests(self, provider):
        if provider == "doubao":
            from models.doubao.doubao_bot import DoubaoBot

            class TestDoubaoBot(DoubaoBot):
                def __init__(self):
                    self.args = {
                        "model": "doubao-seed-2-0-pro-260215",
                        "temperature": 0.8,
                        "top_p": 1.0,
                    }

                @property
                def api_key(self):
                    return "test-key"

                @property
                def base_url(self):
                    return "https://doubao.test/api/v3"

            return {
                "bot": TestDoubaoBot(),
                "model": "doubao-seed-2-0-pro-260215",
                "provider": "doubao",
                "patch": "models.doubao.doubao_bot.requests.post",
            }

        if provider == "deepseek":
            from models.deepseek.deepseek_bot import DeepSeekBot

            class TestDeepSeekBot(DeepSeekBot):
                def __init__(self):
                    self.args = {
                        "model": "deepseek-v4-flash",
                        "temperature": 0.7,
                        "top_p": 1.0,
                        "frequency_penalty": 0.0,
                        "presence_penalty": 0.0,
                    }

                @property
                def api_key(self):
                    return "test-key"

                @property
                def api_base(self):
                    return "https://deepseek.test/v1"

            return {
                "bot": TestDeepSeekBot(),
                "model": "deepseek-v4-flash",
                "provider": "deepseek",
                "patch": "models.deepseek.deepseek_bot.requests.post",
            }

        if provider == "minimax":
            from models.minimax.minimax_bot import MinimaxBot

            class TestMinimaxBot(MinimaxBot):
                def __init__(self):
                    self.args = {
                        "model": "MiniMax-M3",
                        "temperature": 0.3,
                        "top_p": 0.95,
                    }

                @property
                def api_key(self):
                    return "test-key"

                @property
                def api_base(self):
                    return "https://minimax.test/v1"

            return {
                "bot": TestMinimaxBot(),
                "model": "MiniMax-M3",
                "provider": "minimax",
                "patch": "models.minimax.minimax_bot.requests.post",
            }

        if provider == "mimo":
            from models.mimo.mimo_bot import MimoBot

            class TestMimoBot(MimoBot):
                def __init__(self):
                    self.args = {
                        "model": "mimo-v2.5-pro",
                        "temperature": 1.0,
                        "top_p": 0.95,
                    }

                @property
                def api_key(self):
                    return "test-key"

                @property
                def api_base(self):
                    return "https://mimo.test/v1"

            return {
                "bot": TestMimoBot(),
                "model": "mimo-v2.5-pro",
                "provider": "mimo",
                "patch": "models.mimo.mimo_bot.requests.post",
            }

        if provider == "moonshot":
            from models.moonshot.moonshot_bot import MoonshotBot

            class TestMoonshotBot(MoonshotBot):
                def __init__(self):
                    self.args = {
                        "model": "moonshot-v1-32k",
                        "temperature": 0.3,
                        "top_p": 1.0,
                    }

                @property
                def api_key(self):
                    return "test-key"

                @property
                def base_url(self):
                    return "https://moonshot.test/v1"

                @property
                def _is_kimi_coding_plan(self):
                    return False

            return {
                "bot": TestMoonshotBot(),
                "model": "moonshot-v1-32k",
                "provider": "moonshot",
                "patch": "models.moonshot.moonshot_bot.requests.post",
            }

        raise AssertionError("unsupported provider")

    def _special_native_http_provider_bot_for_tests(self, provider):
        if provider == "claude":
            from models.claudeapi.claude_api_bot import ClaudeAPIBot

            class TestClaudeBot(ClaudeAPIBot):
                def __init__(self):
                    self.args = {"model": "claude-3-5-sonnet"}

                @property
                def api_key(self):
                    return "test-key"

                @property
                def api_base(self):
                    return "https://claude.test/v1"

                @property
                def proxy(self):
                    return None

            return {
                "bot": TestClaudeBot(),
                "model": "claude-3-5-sonnet",
                "provider": "claude",
                "patch": "models.claudeapi.claude_api_bot.requests.post",
                "success_payload": {
                    "id": "msg-test",
                    "model": "claude-3-5-sonnet",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                },
                "stream_success_lines": [
                    b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}',
                    b'data: {"type":"message_stop"}',
                ],
                "stream_error_lines": [
                    b'data: {"type":"error","error":{"message":"rate limit","code":"rate_limit_exceeded","type":"rate_limit"},"status_code":429,"retry_after_ms":500}',
                ],
                "stream_post_output_error_lines": [
                    b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"partial"}}',
                    b'data: {"type":"error","error":{"message":"server unavailable","code":"server_error","type":"server_error"},"status_code":503,"retry_after":"0.5"}',
                ],
            }

        if provider == "gemini":
            from models.gemini.google_gemini_bot import GoogleGeminiBot

            class TestGeminiBot(GoogleGeminiBot):
                def __init__(self):
                    pass

                @property
                def api_key(self):
                    return "test-key"

                @property
                def api_base(self):
                    return "https://gemini.test"

                @property
                def model(self):
                    return "gemini-3.5-flash"

            return {
                "bot": TestGeminiBot(),
                "model": "gemini-3.5-flash",
                "provider": "gemini",
                "patch": "models.gemini.google_gemini_bot.requests.post",
                "success_payload": {
                    "candidates": [{
                        "content": {"parts": [{"text": "ok"}]},
                        "finishReason": "STOP",
                    }],
                    "usageMetadata": {
                        "promptTokenCount": 1,
                        "candidatesTokenCount": 2,
                        "totalTokenCount": 3,
                    },
                },
                "stream_success_lines": [
                    b'data: {"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}',
                ],
                "stream_error_lines": [
                    b'data:{"error":{"message":"rate limit","code":"rate_limit_exceeded","type":"rate_limit"},"status_code":429,"retry_after_ms":500}',
                ],
                "stream_post_output_error_lines": [
                    b'data: {"candidates":[{"content":{"parts":[{"text":"partial"}]}}]}',
                    b'data: {"error":{"message":"server unavailable","code":"server_error","type":"server_error"},"status_code":503,"retry_after":"0.5"}',
                ],
            }

        if provider == "linkai":
            from models.linkai.link_ai_bot import LinkAIBot

            class TestLinkAIBot(LinkAIBot):
                def __init__(self):
                    self.args = {}

            return {
                "bot": TestLinkAIBot(),
                "model": "gpt-4o-mini",
                "provider": "linkai",
                "patch": "models.linkai.link_ai_bot.requests.post",
                "conf_patch": "models.linkai.link_ai_bot.conf",
                "conf": {
                    "channel_type": "web",
                    "linkai_api_key": "test-key",
                    "linkai_api_base": "https://linkai.test",
                    "model": "gpt-4o-mini",
                    "temperature": 0.7,
                    "top_p": 1,
                    "frequency_penalty": 0,
                    "presence_penalty": 0,
                    "request_timeout": 180,
                },
                "success_payload": {
                    "choices": [{
                        "message": {"content": "ok"},
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 2,
                        "total_tokens": 3,
                    },
                },
                "stream_success_lines": [
                    b'data: {"choices":[{"delta":{"content":"ok"}}]}',
                    b'data: [DONE]',
                ],
                "stream_error_lines": [
                    b'data: {"type":"error","error":{"message":"rate limit","code":"rate_limit_exceeded","type":"rate_limit","http_code":"429"},"retry_after_ms":500}',
                ],
                "stream_post_output_error_lines": [
                    b'data: {"choices":[{"delta":{"content":"partial"}}]}',
                    b'data: {"type":"error","error":{"message":"server unavailable","code":"server_error","type":"server_error","http_code":"503"},"retry_after":"0.5"}',
                ],
            }

        raise AssertionError("unsupported special provider")

    def _with_provider_patches(self, spec, post_side_effect):
        from contextlib import ExitStack
        from unittest.mock import MagicMock, patch

        stack = ExitStack()
        stack.enter_context(patch(spec["patch"], side_effect=post_side_effect))
        conf_patch = spec.get("conf_patch")
        if conf_patch:
            fake_conf = MagicMock()
            values = dict(spec.get("conf") or {})
            fake_conf.get.side_effect = lambda key, default=None: values.get(key, default)
            stack.enter_context(patch(conf_patch, return_value=fake_conf))
        return stack

    def test_special_native_http_providers_sync_retry_after_and_fail_closed(self):
        import requests
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import (
            get_recent_model_calls,
            reset_model_call_telemetry_for_tests,
        )

        for provider in ("claude", "gemini", "linkai"):
            with self.subTest(provider=provider, mode="retry_after"):
                reset_model_call_telemetry_for_tests()
                spec = self._special_native_http_provider_bot_for_tests(provider)
                responses = [
                    self._FakeHTTPResponse(
                        429,
                        {
                            "error": {
                                "message": "rate limit",
                                "code": "rate_limit_exceeded",
                                "type": "rate_limit",
                            }
                        },
                        headers={"Retry-After": "0.25"},
                    ),
                    self._FakeHTTPResponse(200, spec["success_payload"]),
                ]
                posts = []

                def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
                    posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout, **kwargs})
                    return responses.pop(0)

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with self._with_provider_patches(spec, fake_post):
                    result = model.call(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        model_max_retries=1,
                        model_retry_sleep=sleeps.append,
                    ))

                self.assertEqual(result["choices"][0]["message"]["content"], "ok")
                self.assertEqual(sleeps, [0.25])
                self.assertEqual(len(posts), 2)
                events = get_recent_model_calls()
                self.assertEqual([event["status"] for event in events], ["failed", "completed"])
                self.assertEqual(events[0]["provider"], spec["provider"])
                self.assertEqual(events[0]["error_taxonomy"], "rate_limit")
                self.assertEqual(events[0]["error_code"], "rate_limit_exceeded")
                self.assertEqual(events[0]["error_type"], "rate_limit")

            with self.subTest(provider=provider, mode="fail_closed_4xx"):
                reset_model_call_telemetry_for_tests()
                spec = self._special_native_http_provider_bot_for_tests(provider)
                posts = []

                def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
                    posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout, **kwargs})
                    return self._FakeHTTPResponse(
                        400,
                        text="bad request text",
                        json_error=ValueError("not json"),
                    )

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with self._with_provider_patches(spec, fake_post):
                    result = model.call(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        model_max_retries=2,
                        model_retry_sleep=sleeps.append,
                    ))

                self.assertEqual(result["status_code"], 400)
                self.assertEqual(result["message"], "bad request text")
                self.assertEqual(result["error_taxonomy"], "client_error")
                self.assertFalse(result["retryable"])
                self.assertEqual(sleeps, [])
                self.assertEqual(len(posts), 1)
                event = get_recent_model_calls()[0]
                self.assertEqual(event["status"], "failed")
                self.assertEqual(event["error_status_code"], 400)
                self.assertEqual(event["error_taxonomy"], "client_error")

            with self.subTest(provider=provider, mode="timeout_retry"):
                reset_model_call_telemetry_for_tests()
                spec = self._special_native_http_provider_bot_for_tests(provider)
                responses = [
                    requests.Timeout("read timeout"),
                    self._FakeHTTPResponse(200, spec["success_payload"]),
                ]
                posts = []

                def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
                    posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout, **kwargs})
                    response = responses.pop(0)
                    if isinstance(response, Exception):
                        raise response
                    return response

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with self._with_provider_patches(spec, fake_post):
                    result = model.call(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        model_max_retries=1,
                        model_retry_sleep=sleeps.append,
                    ))

                self.assertEqual(result["choices"][0]["message"]["content"], "ok")
                self.assertEqual(sleeps, [2.0])
                self.assertEqual(len(posts), 2)
                events = get_recent_model_calls()
                self.assertEqual([event["status"] for event in events], ["failed", "completed"])
                self.assertEqual(events[0]["error_taxonomy"], "timeout")

            with self.subTest(provider=provider, mode="connection_retry"):
                reset_model_call_telemetry_for_tests()
                spec = self._special_native_http_provider_bot_for_tests(provider)
                responses = [
                    requests.ConnectionError("dns failed"),
                    self._FakeHTTPResponse(200, spec["success_payload"]),
                ]
                posts = []

                def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
                    posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout, **kwargs})
                    response = responses.pop(0)
                    if isinstance(response, Exception):
                        raise response
                    return response

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with self._with_provider_patches(spec, fake_post):
                    result = model.call(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        model_max_retries=1,
                        model_retry_sleep=sleeps.append,
                    ))

                self.assertEqual(result["choices"][0]["message"]["content"], "ok")
                self.assertEqual(sleeps, [2.0])
                self.assertEqual(len(posts), 2)
                events = get_recent_model_calls()
                self.assertEqual([event["status"] for event in events], ["failed", "completed"])
                self.assertEqual(events[0]["error_taxonomy"], "network_error")

    def test_special_native_http_providers_stream_retry_and_suppression(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import (
            get_recent_model_calls,
            reset_model_call_telemetry_for_tests,
        )

        for provider in ("claude", "gemini", "linkai"):
            with self.subTest(provider=provider, mode="http_retry_after"):
                reset_model_call_telemetry_for_tests()
                spec = self._special_native_http_provider_bot_for_tests(provider)
                responses = [
                    self._FakeHTTPResponse(
                        429,
                        {
                            "error": {
                                "message": "rate limit",
                                "code": "rate_limit_exceeded",
                                "type": "rate_limit",
                            }
                        },
                        headers={"Retry-After": "0.5"},
                    ),
                    self._FakeHTTPResponse(200, lines=spec["stream_success_lines"]),
                ]
                posts = []

                def fake_post(url, headers=None, json=None, stream=False, timeout=None, **kwargs):
                    posts.append({
                        "url": url,
                        "headers": headers,
                        "json": json,
                        "stream": stream,
                        "timeout": timeout,
                        **kwargs,
                    })
                    return responses.pop(0)

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with self._with_provider_patches(spec, fake_post):
                    chunks = list(model.call_stream(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        stream=True,
                        model_max_retries=1,
                        model_retry_sleep=sleeps.append,
                    )))

                self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "ok")
                self.assertEqual(sleeps, [0.5])
                self.assertEqual(len(posts), 2)
                events = get_recent_model_calls()
                self.assertEqual([event["status"] for event in events], ["failed", "completed"])
                self.assertEqual(events[0]["error_taxonomy"], "rate_limit")

            with self.subTest(provider=provider, mode="sse_retry_after_ms"):
                reset_model_call_telemetry_for_tests()
                spec = self._special_native_http_provider_bot_for_tests(provider)
                responses = [
                    self._FakeHTTPResponse(200, lines=spec["stream_error_lines"]),
                    self._FakeHTTPResponse(200, lines=spec["stream_success_lines"]),
                ]
                posts = []

                def fake_post(url, headers=None, json=None, stream=False, timeout=None, **kwargs):
                    posts.append({
                        "url": url,
                        "headers": headers,
                        "json": json,
                        "stream": stream,
                        "timeout": timeout,
                        **kwargs,
                    })
                    return responses.pop(0)

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with self._with_provider_patches(spec, fake_post):
                    chunks = list(model.call_stream(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        stream=True,
                        model_max_retries=1,
                        model_retry_sleep=sleeps.append,
                    )))

                self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "ok")
                self.assertEqual(sleeps, [0.5])
                self.assertEqual(len(posts), 2)
                events = get_recent_model_calls()
                self.assertEqual([event["status"] for event in events], ["failed", "completed"])
                self.assertEqual(events[0]["error_taxonomy"], "rate_limit")

            with self.subTest(provider=provider, mode="post_output_suppression"):
                reset_model_call_telemetry_for_tests()
                spec = self._special_native_http_provider_bot_for_tests(provider)
                posts = []

                def fake_post(url, headers=None, json=None, stream=False, timeout=None, **kwargs):
                    posts.append({
                        "url": url,
                        "headers": headers,
                        "json": json,
                        "stream": stream,
                        "timeout": timeout,
                        **kwargs,
                    })
                    return self._FakeHTTPResponse(
                        200,
                        lines=spec["stream_post_output_error_lines"],
                    )

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with self._with_provider_patches(spec, fake_post):
                    chunks = list(model.call_stream(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        stream=True,
                        model_max_retries=1,
                        model_retry_sleep=sleeps.append,
                    )))

                self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "partial")
                self.assertEqual(chunks[1]["status_code"], 503)
                self.assertTrue(chunks[1]["retryable"])
                self.assertTrue(chunks[1]["retry_suppressed"])
                self.assertEqual(chunks[1]["retry_suppressed_reason"], "stream_output_started")
                self.assertEqual(sleeps, [])
                self.assertEqual(len(posts), 1)
                event = get_recent_model_calls()[0]
                self.assertEqual(event["status"], "failed")
                self.assertEqual(event["error_taxonomy"], "server_error")

    def test_special_native_http_providers_stream_setup_errors_do_not_capture_cleared_exception(self):
        import requests
        from unittest.mock import patch

        claude_spec = self._special_native_http_provider_bot_for_tests("claude")
        with patch.object(
            claude_spec["bot"],
            "_handle_stream_response",
            side_effect=requests.Timeout("setup timeout"),
        ):
            chunks = list(claude_spec["bot"].call_with_tools(
                [{"role": "user", "content": "hi"}],
                stream=True,
            ))

        self.assertEqual(chunks[0]["status_code"], 504)
        self.assertIn("timed out", chunks[0]["message"])

        with patch.dict(sys.modules, {
            "zai": SimpleNamespace(ZhipuAiClient=lambda *args, **kwargs: None),
        }):
            from models.zhipuai.zhipuai_bot import ZHIPUAIBot

        zhipu_bot = ZHIPUAIBot.__new__(ZHIPUAIBot)
        zhipu_bot.args = {"model": "glm-4", "temperature": 0.7, "top_p": 0.9}
        with patch.object(
            zhipu_bot,
            "_handle_stream_response",
            side_effect=RuntimeError("setup failed"),
        ):
            chunks = list(zhipu_bot.call_with_tools(
                [{"role": "user", "content": "hi"}],
                stream=True,
            ))

        self.assertEqual(chunks[0]["status_code"], 500)
        self.assertEqual(chunks[0]["message"], "setup failed")

    def test_zhipu_sdk_errors_use_shared_retry_evidence(self):
        from unittest.mock import patch
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls, reset_model_call_telemetry_for_tests
        with patch.dict(sys.modules, {
            "zai": SimpleNamespace(ZhipuAiClient=lambda *args, **kwargs: None),
        }):
            from models.zhipuai.zhipuai_bot import ZHIPUAIBot

        class FakeZhipuError(Exception):
            def __init__(self, message, *, response=None, body=None):
                super().__init__(message)
                self.response = response
                self.body = body

        class FakeCompletions:
            def __init__(self, responses):
                self.responses = list(responses)
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                response = self.responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        class FakeClient:
            def __init__(self, completions):
                self.chat = SimpleNamespace(completions=completions)

        def success_response():
            return SimpleNamespace(
                id="zhipu-ok",
                created=1,
                model="glm-4",
                choices=[SimpleNamespace(
                    message=SimpleNamespace(role="assistant", content="ok", tool_calls=None),
                    finish_reason="stop",
                )],
                usage=SimpleNamespace(
                    prompt_tokens=1,
                    completion_tokens=2,
                    total_tokens=3,
                ),
            )

        reset_model_call_telemetry_for_tests()
        retry_error = FakeZhipuError(
            "rate limit",
            response=self._FakeHTTPResponse(
                429,
                {
                    "error": {
                        "message": "rate limit",
                        "code": "rate_limit_exceeded",
                        "type": "rate_limit",
                    }
                },
                headers={"Retry-After": "0.25"},
            ),
        )
        completions = FakeCompletions([retry_error, success_response()])
        bot = ZHIPUAIBot.__new__(ZHIPUAIBot)
        bot.args = {"model": "glm-4", "temperature": 0.7, "top_p": 0.9}
        bot.client = FakeClient(completions)
        sleeps = []
        model = self._native_agent_model(bot, model_name="glm-4", provider="zhipu_ai")

        result = model.call(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            model_max_retries=1,
            model_retry_sleep=sleeps.append,
        ))

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(sleeps, [0.25])
        self.assertEqual(len(completions.calls), 2)
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "completed"])
        self.assertEqual(events[0]["error_taxonomy"], "rate_limit")
        self.assertEqual(events[0]["error_code"], "rate_limit_exceeded")

        reset_model_call_telemetry_for_tests()
        fail_closed_error = FakeZhipuError(
            "bad request",
            body={
                "status_code": 400,
                "error": {
                    "message": "bad request",
                    "code": "invalid_request",
                    "type": "invalid_request_error",
                },
            },
        )
        completions = FakeCompletions([fail_closed_error])
        bot.client = FakeClient(completions)
        sleeps = []

        result = model.call(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            model_max_retries=2,
            model_retry_sleep=sleeps.append,
        ))

        self.assertEqual(result["status_code"], 400)
        self.assertEqual(result["message"], "bad request")
        self.assertEqual(result["error_taxonomy"], "client_error")
        self.assertFalse(result["retryable"])
        self.assertEqual(sleeps, [])
        self.assertEqual(len(completions.calls), 1)
        event = get_recent_model_calls()[0]
        self.assertEqual(event["error_status_code"], 400)
        self.assertEqual(event["error_code"], "invalid_request")

    def test_dashscope_sdk_errors_use_shared_retry_evidence(self):
        from unittest.mock import patch
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls, reset_model_call_telemetry_for_tests
        fake_dashscope = SimpleNamespace(
            Generation=SimpleNamespace(
                Models=SimpleNamespace(
                    qwen_turbo="qwen-turbo",
                    qwen_plus="qwen-plus",
                    qwen_max="qwen-max",
                    bailian_v1="qwen-bailian-v1",
                ),
                call=None,
            ),
            MultiModalConversation=SimpleNamespace(call=None),
        )
        with patch.dict(sys.modules, {"dashscope": fake_dashscope}):
            from models.dashscope.dashscope_bot import DashscopeBot

        class FakeDashscopeResponse:
            def __init__(self, *, status_code, code="", message="", output=None, usage=None, retry_after_ms=None):
                self.status_code = status_code
                self.code = code
                self.message = message
                self.output = output or {}
                self.usage = usage or {}
                if retry_after_ms is not None:
                    self.retry_after_ms = retry_after_ms

        class FakeDashscopeProxyResponse:
            def __init__(self, data):
                self._data = data

            def __getattr__(self, name):
                raise KeyError(name)

            def __getitem__(self, key):
                return self._data[key]

            def keys(self):
                return self._data.keys()

        def success_response():
            return FakeDashscopeResponse(
                status_code=200,
                output={
                    "choices": [{
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }]
                },
                usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            )

        class TestDashscopeBot(DashscopeBot):
            def __init__(self):
                self.model_name = "qwen-plus"

            @property
            def api_key(self):
                return "test-key"

        reset_model_call_telemetry_for_tests()
        responses = [
            FakeDashscopeResponse(
                status_code=429,
                code="rate_limit_exceeded",
                message="rate limit",
                retry_after_ms=500,
            ),
            success_response(),
        ]
        calls = []

        def fake_generation_call(**kwargs):
            calls.append(kwargs)
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        sleeps = []
        bot = TestDashscopeBot()
        model = self._native_agent_model(bot, model_name="qwen-plus", provider="dashscope")

        with patch.object(fake_dashscope.Generation, "call", side_effect=fake_generation_call):
            result = model.call(LLMRequest(
                messages=[{"role": "user", "content": "hi"}],
                model_max_retries=1,
                model_retry_sleep=sleeps.append,
            ))

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(sleeps, [0.5])
        self.assertEqual(len(calls), 2)
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "completed"])
        self.assertEqual(events[0]["error_taxonomy"], "rate_limit")
        self.assertEqual(events[0]["error_code"], "rate_limit_exceeded")

        reset_model_call_telemetry_for_tests()
        responses = [
            FakeDashscopeResponse(
                status_code=400,
                code="invalid_request",
                message="bad request",
            )
        ]
        calls = []
        sleeps = []

        with patch.object(fake_dashscope.Generation, "call", side_effect=fake_generation_call):
            result = model.call(LLMRequest(
                messages=[{"role": "user", "content": "hi"}],
                model_max_retries=2,
                model_retry_sleep=sleeps.append,
            ))

        self.assertEqual(result["status_code"], 400)
        self.assertEqual(result["message"], "bad request")
        self.assertEqual(result["error_taxonomy"], "client_error")
        self.assertEqual(sleeps, [])
        self.assertEqual(len(calls), 1)
        event = get_recent_model_calls()[0]
        self.assertEqual(event["error_status_code"], 400)
        self.assertEqual(event["error_code"], "invalid_request")

        reset_model_call_telemetry_for_tests()
        responses = [
            FakeDashscopeProxyResponse({
                "status_code": 429,
                "code": "rate_limit_exceeded",
                "message": "rate limit",
                "retry_after_ms": 500,
            }),
            success_response(),
        ]
        calls = []
        sleeps = []

        with patch.object(fake_dashscope.Generation, "call", side_effect=fake_generation_call):
            result = model.call(LLMRequest(
                messages=[{"role": "user", "content": "hi"}],
                model_max_retries=1,
                model_retry_sleep=sleeps.append,
            ))

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(sleeps, [0.5])
        self.assertEqual(len(calls), 2)
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "completed"])
        self.assertEqual(events[0]["error_taxonomy"], "rate_limit")

        for exc, taxonomy in (
            (TimeoutError("request timeout"), "timeout"),
            (ConnectionError("connection reset by peer"), "network_error"),
        ):
            with self.subTest(provider="dashscope", mode=taxonomy):
                reset_model_call_telemetry_for_tests()
                responses = [exc, success_response()]
                calls = []
                sleeps = []

                with patch.object(fake_dashscope.Generation, "call", side_effect=fake_generation_call):
                    result = model.call(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        model_max_retries=1,
                        model_retry_sleep=sleeps.append,
                    ))

                self.assertEqual(result["choices"][0]["message"]["content"], "ok")
                self.assertEqual(sleeps, [2.0])
                self.assertEqual(len(calls), 2)
                events = get_recent_model_calls()
                self.assertEqual([event["status"] for event in events], ["failed", "completed"])
                self.assertEqual(events[0]["error_taxonomy"], taxonomy)

    def test_dashscope_stream_error_after_output_is_retry_suppressed(self):
        from unittest.mock import patch
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls
        fake_dashscope = SimpleNamespace(
            Generation=SimpleNamespace(
                Models=SimpleNamespace(
                    qwen_turbo="qwen-turbo",
                    qwen_plus="qwen-plus",
                    qwen_max="qwen-max",
                    bailian_v1="qwen-bailian-v1",
                ),
                call=None,
            ),
            MultiModalConversation=SimpleNamespace(call=None),
        )
        with patch.dict(sys.modules, {"dashscope": fake_dashscope}):
            from models.dashscope.dashscope_bot import DashscopeBot

        class FakeDashscopeResponse:
            def __init__(self, *, status_code, code="", message="", output=None, retry_after_ms=None):
                self.status_code = status_code
                self.code = code
                self.message = message
                self.output = output or {}
                self.usage = {}
                if retry_after_ms is not None:
                    self.retry_after_ms = retry_after_ms

        class TestDashscopeBot(DashscopeBot):
            def __init__(self):
                self.model_name = "qwen-plus"

            @property
            def api_key(self):
                return "test-key"

        responses = [[
            FakeDashscopeResponse(
                status_code=200,
                output={
                    "choices": [{
                        "message": {"role": "assistant", "content": "partial"},
                        "finish_reason": None,
                    }]
                },
            ),
            FakeDashscopeResponse(
                status_code=503,
                code="server_error",
                message="server unavailable",
                retry_after_ms=500,
            ),
        ]]
        calls = []

        def fake_generation_call(**kwargs):
            calls.append(kwargs)
            return iter(responses.pop(0))

        sleeps = []
        bot = TestDashscopeBot()
        model = self._native_agent_model(bot, model_name="qwen-plus", provider="dashscope")

        with patch.object(fake_dashscope.Generation, "call", side_effect=fake_generation_call):
            chunks = list(model.call_stream(LLMRequest(
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                model_max_retries=1,
                model_retry_sleep=sleeps.append,
            )))

        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "partial")
        self.assertEqual(chunks[1]["status_code"], 503)
        self.assertTrue(chunks[1]["retryable"])
        self.assertTrue(chunks[1]["retry_suppressed"])
        self.assertEqual(chunks[1]["retry_suppressed_reason"], "stream_output_started")
        self.assertEqual(sleeps, [])
        self.assertEqual(len(calls), 1)
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_taxonomy"], "server_error")

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

    def test_native_http_providers_sync_retry_after_uses_shared_gateway(self):
        from unittest.mock import patch
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import (
            get_recent_model_calls,
            reset_model_call_telemetry_for_tests,
        )

        for provider in ("deepseek", "mimo", "doubao", "moonshot", "minimax"):
            with self.subTest(provider=provider):
                reset_model_call_telemetry_for_tests()
                spec = self._native_http_provider_bot_for_tests(provider)
                responses = [
                    self._FakeHTTPResponse(
                        429,
                        {
                            "error": {
                                "message": "rate limit",
                                "code": "rate_limit_exceeded",
                                "type": "rate_limit",
                            }
                        },
                        headers={"Retry-After": "0.25"},
                    ),
                    self._FakeHTTPResponse(
                        200,
                        {
                            "choices": [{
                                "message": {"content": "ok"},
                                "finish_reason": "stop",
                            }],
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 2,
                                "total_tokens": 3,
                            },
                        },
                    ),
                ]
                posts = []

                def fake_post(url, headers=None, json=None, timeout=None, **_kwargs):
                    posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
                    return responses.pop(0)

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with patch(spec["patch"], side_effect=fake_post):
                    result = model.call(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        model_max_retries=1,
                        model_retry_sleep=sleeps.append,
                    ))

                self.assertEqual(result["content"][0]["text"], "ok")
                self.assertEqual(sleeps, [0.25])
                self.assertEqual(len(posts), 2)
                self.assertFalse("retry_count" in posts[0]["json"])
                self.assertFalse("model_max_retries" in posts[0]["json"])
                self.assertFalse("model_retry_sleep" in posts[0]["json"])
                events = get_recent_model_calls()
                self.assertEqual([event["status"] for event in events], ["failed", "completed"])
                self.assertEqual([event["retry_count"] for event in events], [0, 1])
                self.assertEqual([event["provider"] for event in events], [spec["provider"], spec["provider"]])
                self.assertEqual(events[0]["error_taxonomy"], "rate_limit")
                self.assertEqual(events[0]["error_code"], "rate_limit_exceeded")
                self.assertEqual(events[0]["error_type"], "rate_limit")

    def test_native_http_providers_sync_non_json_4xx_fails_closed(self):
        from unittest.mock import patch
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import (
            get_recent_model_calls,
            reset_model_call_telemetry_for_tests,
        )

        for provider in ("deepseek", "mimo", "doubao", "moonshot", "minimax"):
            with self.subTest(provider=provider):
                reset_model_call_telemetry_for_tests()
                spec = self._native_http_provider_bot_for_tests(provider)
                posts = []

                def fake_post(url, headers=None, json=None, timeout=None, **_kwargs):
                    posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
                    return self._FakeHTTPResponse(
                        400,
                        text="bad request text",
                        json_error=ValueError("not json"),
                    )

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with patch(spec["patch"], side_effect=fake_post):
                    result = model.call(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        model_max_retries=2,
                        model_retry_sleep=sleeps.append,
                    ))

                self.assertEqual(result["status_code"], 400)
                self.assertEqual(result["message"], "bad request text")
                self.assertEqual(result["error_taxonomy"], "client_error")
                self.assertFalse(result["retryable"])
                self.assertEqual(sleeps, [])
                self.assertEqual(len(posts), 1)
                event = get_recent_model_calls()[0]
                self.assertEqual(event["status"], "failed")
                self.assertEqual(event["error_status_code"], 400)
                self.assertEqual(event["error_taxonomy"], "client_error")

    def test_native_http_providers_stream_retry_after_before_output(self):
        from unittest.mock import patch
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import (
            get_recent_model_calls,
            reset_model_call_telemetry_for_tests,
        )

        for provider in ("deepseek", "mimo", "doubao", "moonshot", "minimax"):
            with self.subTest(provider=provider):
                reset_model_call_telemetry_for_tests()
                spec = self._native_http_provider_bot_for_tests(provider)
                responses = [
                    self._FakeHTTPResponse(
                        429,
                        {
                            "error": {
                                "message": "rate limit",
                                "code": "rate_limit_exceeded",
                                "type": "rate_limit",
                            }
                        },
                        headers={"Retry-After": "0.5"},
                    ),
                    self._FakeHTTPResponse(
                        200,
                        lines=[
                            b'data: {"choices":[{"delta":{"content":"ok"}}]}',
                            b'data: [DONE]',
                        ],
                    ),
                ]
                posts = []

                def fake_post(url, headers=None, json=None, stream=False, timeout=None, **_kwargs):
                    posts.append({
                        "url": url,
                        "headers": headers,
                        "json": json,
                        "stream": stream,
                        "timeout": timeout,
                    })
                    return responses.pop(0)

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with patch(spec["patch"], side_effect=fake_post):
                    chunks = list(model.call_stream(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        stream=True,
                        model_max_retries=1,
                        model_retry_sleep=sleeps.append,
                    )))

                self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "ok")
                self.assertEqual(sleeps, [0.5])
                self.assertEqual(len(posts), 2)
                self.assertEqual([post["stream"] for post in posts], [True, True])
                self.assertFalse("retry_count" in posts[0]["json"])
                self.assertFalse("model_max_retries" in posts[0]["json"])
                self.assertFalse("model_retry_sleep" in posts[0]["json"])
                events = get_recent_model_calls()
                self.assertEqual([event["status"] for event in events], ["failed", "completed"])
                self.assertEqual(events[0]["error_taxonomy"], "rate_limit")

    def test_native_http_providers_stream_error_after_output_is_retry_suppressed(self):
        from unittest.mock import patch
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import (
            get_recent_model_calls,
            reset_model_call_telemetry_for_tests,
        )

        for provider in ("deepseek", "mimo", "doubao", "moonshot", "minimax"):
            with self.subTest(provider=provider):
                reset_model_call_telemetry_for_tests()
                spec = self._native_http_provider_bot_for_tests(provider)
                posts = []

                def fake_post(url, headers=None, json=None, stream=False, timeout=None, **_kwargs):
                    posts.append({
                        "url": url,
                        "headers": headers,
                        "json": json,
                        "stream": stream,
                        "timeout": timeout,
                    })
                    return self._FakeHTTPResponse(
                        200,
                        lines=[
                            b'data: {"choices":[{"delta":{"content":"partial"}}]}',
                            b'data: {"error":{"message":"server unavailable","code":"server_error","type":"server_error"},"status_code":503,"retry_after":"0.5"}',
                            b'data: [DONE]',
                        ],
                    )

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with patch(spec["patch"], side_effect=fake_post):
                    chunks = list(model.call_stream(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        stream=True,
                        model_max_retries=1,
                        model_retry_sleep=sleeps.append,
                    )))

                self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "partial")
                self.assertEqual(chunks[1]["status_code"], 503)
                self.assertTrue(chunks[1]["retryable"])
                self.assertTrue(chunks[1]["retry_suppressed"])
                self.assertEqual(chunks[1]["retry_suppressed_reason"], "stream_output_started")
                self.assertEqual(sleeps, [])
                self.assertEqual(len(posts), 1)
                events = get_recent_model_calls()
                self.assertEqual(len(events), 1)
                event = events[0]
                self.assertEqual(event["status"], "failed")
                self.assertEqual(event["error_taxonomy"], "server_error")

    def test_native_http_providers_stream_retry_after_ms_keeps_millisecond_units(self):
        from unittest.mock import patch
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import (
            get_recent_model_calls,
            reset_model_call_telemetry_for_tests,
        )

        for provider in ("deepseek", "mimo", "doubao", "moonshot", "minimax"):
            with self.subTest(provider=provider):
                reset_model_call_telemetry_for_tests()
                spec = self._native_http_provider_bot_for_tests(provider)
                responses = [
                    self._FakeHTTPResponse(
                        200,
                        lines=[
                            b'data: {"error":{"message":"rate limit","code":"rate_limit_exceeded","type":"rate_limit"},"status_code":429,"retry_after_ms":500}',
                            b'data: [DONE]',
                        ],
                    ),
                    self._FakeHTTPResponse(
                        200,
                        lines=[
                            b'data: {"choices":[{"delta":{"content":"ok"}}]}',
                            b'data: [DONE]',
                        ],
                    ),
                ]

                def fake_post(url, headers=None, json=None, stream=False, timeout=None, **_kwargs):
                    return responses.pop(0)

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with patch(spec["patch"], side_effect=fake_post):
                    chunks = list(model.call_stream(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        stream=True,
                        model_max_retries=1,
                        model_retry_sleep=sleeps.append,
                    )))

                self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "ok")
                self.assertEqual(sleeps, [0.5])
                events = get_recent_model_calls()
                self.assertEqual([event["status"] for event in events], ["failed", "completed"])
                self.assertEqual(events[0]["error_taxonomy"], "rate_limit")

    def test_native_http_providers_minimax_stream_http_code_is_retryable(self):
        from unittest.mock import patch
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import (
            get_recent_model_calls,
            reset_model_call_telemetry_for_tests,
        )

        cases = [
            (
                "top_level_http_code",
                b'data: {"type":"error","message":"rate limit","http_code":"429","retry_after_ms":500}',
            ),
            (
                "nested_http_code",
                b'data: {"type":"error","error":{"message":"rate limit","type":"rate_limit","http_code":"429","retry_after_ms":500}}',
            ),
        ]

        for case_name, error_line in cases:
            with self.subTest(case=case_name):
                reset_model_call_telemetry_for_tests()
                spec = self._native_http_provider_bot_for_tests("minimax")
                responses = [
                    self._FakeHTTPResponse(
                        200,
                        lines=[
                            error_line,
                            b'data: [DONE]',
                        ],
                    ),
                    self._FakeHTTPResponse(
                        200,
                        lines=[
                            b'data: {"choices":[{"delta":{"content":"ok"}}]}',
                            b'data: [DONE]',
                        ],
                    ),
                ]

                def fake_post(url, headers=None, json=None, stream=False, timeout=None, **_kwargs):
                    return responses.pop(0)

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with patch(spec["patch"], side_effect=fake_post):
                    chunks = list(model.call_stream(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        stream=True,
                        model_max_retries=1,
                        model_retry_sleep=sleeps.append,
                    )))

                self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "ok")
                self.assertEqual(sleeps, [0.5])
                events = get_recent_model_calls()
                self.assertEqual([event["status"] for event in events], ["failed", "completed"])
                self.assertEqual(events[0]["error_status_code"], 429)
                self.assertEqual(events[0]["error_taxonomy"], "rate_limit")

    def test_native_http_providers_stream_top_level_http_code_is_retryable(self):
        from unittest.mock import patch
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import (
            get_recent_model_calls,
            reset_model_call_telemetry_for_tests,
        )

        for provider in ("doubao", "moonshot"):
            with self.subTest(provider=provider):
                reset_model_call_telemetry_for_tests()
                spec = self._native_http_provider_bot_for_tests(provider)
                responses = [
                    self._FakeHTTPResponse(
                        200,
                        lines=[
                            b'data: {"error":{"message":"rate limit","type":"rate_limit"},"http_code":"429","retry_after_ms":500}',
                            b'data: [DONE]',
                        ],
                    ),
                    self._FakeHTTPResponse(
                        200,
                        lines=[
                            b'data: {"choices":[{"delta":{"content":"ok"}}]}',
                            b'data: [DONE]',
                        ],
                    ),
                ]

                def fake_post(url, headers=None, json=None, stream=False, timeout=None, **_kwargs):
                    return responses.pop(0)

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with patch(spec["patch"], side_effect=fake_post):
                    chunks = list(model.call_stream(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        stream=True,
                        model_max_retries=1,
                        model_retry_sleep=sleeps.append,
                    )))

                self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "ok")
                self.assertEqual(sleeps, [0.5])
                events = get_recent_model_calls()
                self.assertEqual([event["status"] for event in events], ["failed", "completed"])
                self.assertEqual(events[0]["error_status_code"], 429)
                self.assertEqual(events[0]["error_taxonomy"], "rate_limit")

    def test_native_http_providers_stream_text_status_code_does_not_hide_http_code(self):
        from unittest.mock import patch
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import (
            get_recent_model_calls,
            reset_model_call_telemetry_for_tests,
        )

        for provider in ("doubao", "moonshot"):
            with self.subTest(provider=provider):
                reset_model_call_telemetry_for_tests()
                spec = self._native_http_provider_bot_for_tests(provider)
                responses = [
                    self._FakeHTTPResponse(
                        200,
                        lines=[
                            b'data: {"error":{"message":"rate limit","type":"rate_limit"},"status_code":"error","http_code":"429","retry_after_ms":500}',
                            b'data: [DONE]',
                        ],
                    ),
                    self._FakeHTTPResponse(
                        200,
                        lines=[
                            b'data: {"choices":[{"delta":{"content":"ok"}}]}',
                            b'data: [DONE]',
                        ],
                    ),
                ]

                def fake_post(url, headers=None, json=None, stream=False, timeout=None, **_kwargs):
                    return responses.pop(0)

                sleeps = []
                model = self._native_agent_model(
                    spec["bot"],
                    model_name=spec["model"],
                    provider=spec["provider"],
                )

                with patch(spec["patch"], side_effect=fake_post):
                    chunks = list(model.call_stream(LLMRequest(
                        messages=[{"role": "user", "content": "hi"}],
                        stream=True,
                        model_max_retries=1,
                        model_retry_sleep=sleeps.append,
                    )))

                self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "ok")
                self.assertEqual(sleeps, [0.5])
                events = get_recent_model_calls()
                self.assertEqual([event["status"] for event in events], ["failed", "completed"])
                self.assertEqual(events[0]["error_status_code"], 429)
                self.assertEqual(events[0]["error_taxonomy"], "rate_limit")

    def test_native_http_provider_error_status_text_does_not_hide_http_code(self):
        from models.model_provider_errors import provider_error_response
        from models.model_retry import build_retry_decision

        error = provider_error_response({
            "message": "rate limit",
            "status": "error",
            "http_code": "429",
            "retry_after_ms": 500,
        })
        decision = build_retry_decision(error, attempt=0, max_retries=1)

        self.assertEqual(error["status_code"], 429)
        self.assertEqual(error["retry_after_ms"], 500)
        self.assertEqual(decision.taxonomy, "rate_limit")
        self.assertTrue(decision.should_retry)
        self.assertEqual(decision.delay_seconds, 0.5)

    def test_agent_bridge_modelscope_real_sync_uses_shared_retry_after_without_local_retry(self):
        from unittest.mock import patch
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        class FakeResponse:
            def __init__(self, status_code, payload, headers=None):
                self.status_code = status_code
                self._payload = payload
                self.headers = headers or {}
                self.text = str(payload)

            def json(self):
                return self._payload

        responses = [
            FakeResponse(
                429,
                {"error": {"message": "rate limit", "code": "rate_limit_exceeded"}},
                headers={"Retry-After": "0.25"},
            ),
            FakeResponse(
                200,
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                },
            ),
        ]
        posts = []

        def fake_post(url, headers=None, json=None, timeout=None, **_kwargs):
            posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return responses.pop(0)

        sleeps = []
        local_sleeps = []
        bot = self._real_modelscope_bot_for_tests()
        model = self._native_agent_model(
            bot,
            model_name="Qwen/Qwen3.5-27B",
            provider="modelscope",
        )

        with patch("models.modelscope.modelscope_bot.requests.post", side_effect=fake_post), \
                patch("models.modelscope.modelscope_bot.time.sleep", side_effect=local_sleeps.append):
            result = model.call(LLMRequest(
                messages=[{"role": "user", "content": "hi"}],
                system="follow the system",
                model_max_retries=1,
                model_retry_sleep=sleeps.append,
                retry_count=2,
            ))

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(sleeps, [0.25])
        self.assertEqual(local_sleeps, [])
        self.assertEqual(len(posts), 2)
        self.assertEqual([post["json"]["stream"] for post in posts], [False, False])
        self.assertEqual(posts[0]["json"]["messages"][0], {
            "role": "system",
            "content": "follow the system",
        })
        self.assertEqual(posts[0]["json"]["messages"][1], {"role": "user", "content": "hi"})
        self.assertFalse("system" in posts[0]["json"])
        self.assertFalse("retry_count" in posts[0]["json"])
        self.assertFalse("model_max_retries" in posts[0]["json"])
        self.assertFalse("model_retry_sleep" in posts[0]["json"])
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "completed"])
        self.assertEqual([event["retry_count"] for event in events], [2, 3])
        self.assertEqual(events[0]["error_taxonomy"], "rate_limit")
        self.assertEqual(events[1]["total_tokens"], 3)

    def test_agent_bridge_modelscope_real_sync_non_json_error_preserves_http_status(self):
        from unittest.mock import patch
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        class NonJsonResponse:
            status_code = 401
            headers = {}
            text = "not json auth failure"

            def json(self):
                raise ValueError("not json")

        posts = []

        def fake_post(url, headers=None, json=None, timeout=None, **_kwargs):
            posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return NonJsonResponse()

        sleeps = []
        local_sleeps = []
        bot = self._real_modelscope_bot_for_tests()
        model = self._native_agent_model(
            bot,
            model_name="Qwen/Qwen3.5-27B",
            provider="modelscope",
        )

        with patch("models.modelscope.modelscope_bot.requests.post", side_effect=fake_post), \
                patch("models.modelscope.modelscope_bot.time.sleep", side_effect=local_sleeps.append):
            result = model.call(LLMRequest(
                messages=[{"role": "user", "content": "hi"}],
                model_max_retries=2,
                model_retry_sleep=sleeps.append,
            ))

        self.assertEqual(result["status_code"], 401)
        self.assertEqual(result["error_taxonomy"], "client_error")
        self.assertFalse(result["retryable"])
        self.assertEqual(sleeps, [])
        self.assertEqual(local_sleeps, [])
        self.assertEqual(len(posts), 1)
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 401)
        self.assertEqual(event["error_taxonomy"], "client_error")

    def test_agent_bridge_modelscope_sync_sentinel_uses_shared_retry_and_backoff(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        bot = self._modelscope_sync_sentinel_bot([
            {
                "completion_tokens": 0,
                "content": "rate limited",
                "message": "rate limit",
                "status_code": 429,
                "error": {"message": "rate limit", "code": "rate_limit_exceeded"},
                "retry_after": "0.25",
            },
            {
                "completion_tokens": 0,
                "content": "server unavailable",
                "message": "server unavailable",
                "status_code": 503,
                "error": {"message": "server unavailable"},
            },
            {
                "total_tokens": 7,
                "completion_tokens": 4,
                "content": "ok",
            },
        ])
        sleeps = []
        model = self._native_agent_model(
            bot,
            model_name="Qwen/Qwen3.5-27B",
            provider="modelscope",
        )

        result = model.call(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            model_max_retries=2,
            model_retry_sleep=sleeps.append,
        ))

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual([call["retry_count"] for call in bot.calls], [0, 1, 2])
        self.assertEqual([call["retry_count"] for call in bot.reply_calls], [0, 1, 2])
        self.assertEqual([call["allow_local_retry"] for call in bot.reply_calls], [False, False, False])
        self.assertEqual(sleeps, [0.25, 4.0])
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "failed", "completed"])
        self.assertEqual([event["provider"] for event in events], ["modelscope", "modelscope", "modelscope"])
        self.assertEqual([event["error_taxonomy"] for event in events[:2]], ["rate_limit", "server_error"])
        self.assertEqual(events[0]["api_path"], "/native/call_with_tools")
        self.assertEqual(events[2]["total_tokens"], 7)

    def test_agent_bridge_modelscope_stream_non_200_uses_shared_retry_before_output(self):
        from unittest.mock import patch
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        class FakeResponse:
            def __init__(self, status_code, payload=None, headers=None, lines=None):
                self.status_code = status_code
                self._payload = payload or {}
                self.headers = headers or {}
                self.text = str(self._payload)
                self._lines = list(lines or [])

            def json(self):
                return self._payload

            def iter_lines(self):
                return iter(self._lines)

        responses = [
            FakeResponse(
                429,
                {"error": {"message": "rate limit", "code": "rate_limit_exceeded"}},
                headers={"Retry-After": "0.5"},
            ),
            FakeResponse(
                200,
                lines=[
                    b'data: {"choices":[{"delta":{"content":"ok"}}]}',
                    b'data: [DONE]',
                ],
            ),
        ]
        posts = []

        def fake_post(url, headers=None, json=None, stream=False, timeout=None, **_kwargs):
            posts.append({
                "url": url,
                "headers": headers,
                "json": json,
                "stream": stream,
                "timeout": timeout,
            })
            return responses.pop(0)

        sleeps = []
        bot = self._real_modelscope_bot_for_tests()
        model = self._native_agent_model(
            bot,
            model_name="Qwen/Qwen3.5-27B",
            provider="modelscope",
        )

        with patch("models.modelscope.modelscope_bot.requests.post", side_effect=fake_post):
            chunks = list(model.call_stream(LLMRequest(
                messages=[{"role": "user", "content": "hi"}],
                system="follow the system",
                stream=True,
                model_max_retries=1,
                model_retry_sleep=sleeps.append,
            )))

        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "ok")
        self.assertEqual(sleeps, [0.5])
        self.assertEqual(len(posts), 2)
        self.assertEqual([post["stream"] for post in posts], [True, True])
        self.assertEqual(posts[0]["json"]["messages"][0], {
            "role": "system",
            "content": "follow the system",
        })
        self.assertEqual(posts[0]["json"]["messages"][1], {"role": "user", "content": "hi"})
        self.assertFalse("system" in posts[0]["json"])
        self.assertFalse("retry_count" in posts[0]["json"])
        self.assertFalse("model_max_retries" in posts[0]["json"])
        self.assertFalse("model_retry_sleep" in posts[0]["json"])
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "completed"])
        self.assertEqual([event["provider"] for event in events], ["modelscope", "modelscope"])
        self.assertEqual(events[0]["error_taxonomy"], "rate_limit")

    def test_agent_bridge_sync_falls_back_after_retryable_primary_exhausted(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        class PrimaryBot:
            def __init__(self):
                self.calls = []

            def call_with_tools(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "error": {"message": "server unavailable"},
                    "message": "server unavailable",
                    "status_code": 503,
                }

        class FallbackBot:
            def __init__(self):
                self.calls = []

            def call_with_tools(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "choices": [{"message": {"content": "fallback ok"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                }

        primary = PrimaryBot()
        fallback = FallbackBot()
        model = self._fallback_agent_model({
            "primary-model": primary,
            "fallback-model": fallback,
        })

        result = model.call(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            model_max_retries=0,
        ))

        self.assertEqual(result["choices"][0]["message"]["content"], "fallback ok")
        self.assertEqual(result["model_fallback"]["from_model"], "primary-model")
        self.assertEqual(result["model_fallback"]["to_model"], "fallback-model")
        self.assertEqual(result["model_fallback"]["used"], True)
        self.assertEqual(primary.calls[0]["model"], "primary-model")
        self.assertEqual(fallback.calls[0]["model"], "fallback-model")
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "completed"])
        self.assertEqual([event["provider"] for event in events], ["primary-provider", "fallback-provider"])
        self.assertEqual([event["model"] for event in events], ["primary-model", "fallback-model"])

    def test_agent_bridge_modelscope_sync_sentinel_routes_to_fallback_after_exhausted(self):
        from agent.protocol.models import LLMRequest
        from bridge.agent_bridge import AgentLLMModel
        from models.model_fallback import ModelFallbackRoute
        from models.model_telemetry import get_recent_model_calls

        class ModelScopeFallbackModel(AgentLLMModel):
            def __init__(self, bots):
                self._bots = bots

            @property
            def model(self):
                return "Qwen/Qwen3.5-27B"

            @model.setter
            def model(self, _value):
                pass

            @property
            def bot(self):
                return self._bots["Qwen/Qwen3.5-27B"]

            def _resolve_bot_type(self, model_name):
                return "modelscope" if model_name == "Qwen/Qwen3.5-27B" else "fallback-provider"

            def _model_call_routes(self):
                return [
                    ModelFallbackRoute(
                        model="Qwen/Qwen3.5-27B",
                        bot_type="modelscope",
                        provider="modelscope",
                        reason="primary",
                        index=0,
                    ),
                    ModelFallbackRoute(
                        model="fallback-model",
                        bot_type="fallback-provider",
                        provider="fallback-provider",
                        reason="fallback",
                        index=1,
                    ),
                ]

            def _get_bot_for_route(self, route):
                return self._bots[route.model]

        class FallbackBot:
            def __init__(self):
                self.calls = []

            def call_with_tools(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "choices": [{"message": {"content": "fallback ok"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                }

        primary = self._modelscope_sync_sentinel_bot([{
            "completion_tokens": 0,
            "content": "server unavailable",
            "message": "server unavailable",
            "status_code": 503,
            "error": {"message": "server unavailable"},
        }])
        fallback = FallbackBot()
        model = ModelScopeFallbackModel({
            "Qwen/Qwen3.5-27B": primary,
            "fallback-model": fallback,
        })

        result = model.call(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            model_max_retries=0,
        ))

        self.assertEqual(result["choices"][0]["message"]["content"], "fallback ok")
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(fallback.calls), 1)
        self.assertEqual(result["model_fallback"]["used"], True)
        self.assertEqual(result["model_fallback"]["from_provider"], "modelscope")
        self.assertEqual(result["model_fallback"]["to_model"], "fallback-model")
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "completed"])
        self.assertEqual([event["provider"] for event in events], ["modelscope", "fallback-provider"])

    def test_agent_bridge_create_bot_binds_openai_compatible_route_config(self):
        from bridge.agent_bridge import AgentLLMModel
        from config import conf

        keys = [
            "bot_type",
            "model",
            "open_ai_api_key",
            "open_ai_api_base",
            "custom_api_key",
            "custom_api_base",
        ]
        previous = {key: conf().get(key) for key in keys}
        try:
            conf()["model"] = "gpt-route-test"
            conf()["open_ai_api_key"] = "openai-key"
            conf()["open_ai_api_base"] = "https://api.openai.test/v1"
            conf()["custom_api_key"] = "custom-key"
            conf()["custom_api_base"] = "https://custom.example/v1"

            conf()["bot_type"] = "deepseek"
            custom_bot = AgentLLMModel._create_bot("custom")
            custom_config = custom_bot.get_api_config()
            self.assertEqual(custom_config["provider"], "custom")
            self.assertEqual(custom_config["api_key"], "custom-key")
            self.assertEqual(custom_config["api_base"], "https://custom.example/v1")
            self.assertEqual(custom_bot._get_http_client().api_key, "custom-key")
            self.assertEqual(custom_bot._get_http_client().api_base, "https://custom.example/v1")

            conf()["bot_type"] = "custom"
            openai_bot = AgentLLMModel._create_bot("openai")
            openai_config = openai_bot.get_api_config()
            self.assertEqual(openai_config["provider"], "openai")
            self.assertEqual(openai_config["api_key"], "openai-key")
            self.assertEqual(openai_config["api_base"], "https://api.openai.test/v1")
            self.assertEqual(openai_bot._get_http_client().api_key, "openai-key")
            self.assertEqual(openai_bot._get_http_client().api_base, "https://api.openai.test/v1")
        finally:
            for key, value in previous.items():
                if value is None:
                    conf().pop(key, None)
                else:
                    conf()[key] = value

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

    def test_agent_bridge_modelscope_sync_sentinel_non_retryable_4xx_fails_closed(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        bot = self._modelscope_sync_sentinel_bot([{
            "completion_tokens": 0,
            "content": "invalid request",
            "message": "invalid request",
            "status_code": 400,
            "error": {"message": "invalid request", "code": "bad_request"},
        }])
        sleeps = []
        model = self._native_agent_model(
            bot,
            model_name="Qwen/Qwen3.5-27B",
            provider="modelscope",
        )

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
        self.assertEqual(event["error_status_code"], 400)
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

    def test_agent_bridge_stream_falls_back_before_first_output(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        class ClosableErrorStream:
            def __init__(self):
                self.closed = 0
                self._chunks = iter([{
                    "error": {"message": "server unavailable"},
                    "message": "server unavailable",
                    "status_code": 503,
                }])

            def __iter__(self):
                return self

            def __next__(self):
                return next(self._chunks)

            def close(self):
                self.closed += 1

        class PrimaryBot:
            def __init__(self):
                self.calls = []
                self.stream = ClosableErrorStream()

            def call_with_tools(self, **kwargs):
                self.calls.append(kwargs)
                return self.stream

        class FallbackBot:
            def __init__(self):
                self.calls = []

            def call_with_tools(self, **kwargs):
                self.calls.append(kwargs)
                return iter([
                    {"choices": [{"delta": {"content": "ok"}}]},
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                ])

        primary = PrimaryBot()
        fallback = FallbackBot()
        model = self._fallback_agent_model({
            "primary-model": primary,
            "fallback-model": fallback,
        })

        result = list(model.call_stream(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            model_max_retries=0,
        )))

        self.assertEqual(result[0]["choices"][0]["delta"]["content"], "ok")
        self.assertEqual(result[0]["model_fallback"]["from_model"], "primary-model")
        self.assertEqual(result[0]["model_fallback"]["to_model"], "fallback-model")
        self.assertEqual(result[1]["model_fallback"]["from_model"], "primary-model")
        self.assertEqual(result[1]["model_fallback"]["to_model"], "fallback-model")
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(fallback.calls), 1)
        self.assertEqual(primary.stream.closed, 1)
        events = get_recent_model_calls()
        self.assertEqual([event["status"] for event in events], ["failed", "completed"])
        self.assertEqual([event["model"] for event in events], ["primary-model", "fallback-model"])

    def test_agent_bridge_stream_does_not_fallback_after_output_started(self):
        from agent.protocol.models import LLMRequest
        from models.model_telemetry import get_recent_model_calls

        class PrimaryBot:
            def __init__(self):
                self.calls = []

            def call_with_tools(self, **kwargs):
                self.calls.append(kwargs)
                return iter([
                    {"choices": [{"delta": {"content": "partial"}}]},
                    {
                        "error": {"message": "server unavailable"},
                        "message": "server unavailable",
                        "status_code": 503,
                    },
                ])

        class FallbackBot:
            def __init__(self):
                self.calls = []

            def call_with_tools(self, **kwargs):
                self.calls.append(kwargs)
                return iter([{"choices": [{"delta": {"content": "should not run"}}]}])

        primary = PrimaryBot()
        fallback = FallbackBot()
        model = self._fallback_agent_model({
            "primary-model": primary,
            "fallback-model": fallback,
        })

        result = list(model.call_stream(LLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            model_max_retries=1,
        )))

        self.assertEqual(result[0]["choices"][0]["delta"]["content"], "partial")
        self.assertEqual(result[1]["status_code"], 503)
        self.assertTrue(result[1]["retry_suppressed"])
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(fallback.calls), 0)
        events = get_recent_model_calls()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[0]["model"], "primary-model")

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

    def test_modelscope_legacy_reply_text_keeps_local_retry_and_strips_control_args(self):
        from unittest.mock import patch
        from models.modelscope.modelscope_bot import ModelScopeBot

        class FakeResponse:
            def __init__(self, status_code, payload, headers=None):
                self.status_code = status_code
                self._payload = payload
                self.headers = headers or {}
                self.text = str(payload)

            def json(self):
                return self._payload

        bot = ModelScopeBot.__new__(ModelScopeBot)
        bot.api_key = "test-key"
        bot.base_url = "https://modelscope.test/v1"
        session = SimpleNamespace(messages=[{"role": "user", "content": "hi"}])
        sleeps = []
        posts = []
        responses = [
            FakeResponse(503, {"error": {"message": "server unavailable"}}),
            FakeResponse(
                200,
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"completion_tokens": 2, "total_tokens": 5},
                },
            ),
        ]

        def fake_post(url, headers=None, json=None, timeout=None, **_kwargs):
            posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return responses.pop(0)

        with patch("models.modelscope.modelscope_bot.requests.post", side_effect=fake_post):
            result = bot.reply_text(
                session,
                args={
                    "model": "Qwen/Qwen3.5-27B",
                    "retry_count": 99,
                    "model_max_retries": 3,
                    "model_retry_sleep": sleeps.append,
                    "session_id": "session-1",
                    "channel_type": "web",
                    "thinking": {"type": "disabled"},
                    "reasoning_effort": "high",
                },
                allow_local_retry=True,
                local_retry_sleep=sleeps.append,
            )

        self.assertEqual(result["content"], "ok")
        self.assertEqual(result["completion_tokens"], 2)
        self.assertEqual(sleeps, [3])
        self.assertEqual(len(posts), 2)
        sent_body = posts[0]["json"]
        self.assertEqual(sent_body["model"], "Qwen/Qwen3.5-27B")
        self.assertFalse("retry_count" in sent_body)
        self.assertFalse("model_max_retries" in sent_body)
        self.assertFalse("model_retry_sleep" in sent_body)
        self.assertFalse("session_id" in sent_body)
        self.assertFalse("channel_type" in sent_body)
        self.assertFalse("thinking" in sent_body)
        self.assertFalse("reasoning_effort" in sent_body)

    def test_modelscope_stream_non_200_yields_typed_error_and_strips_control_args(self):
        from unittest.mock import patch
        from models.modelscope.modelscope_bot import ModelScopeBot

        class FakeResponse:
            status_code = 429
            headers = {"Retry-After": "0.5"}
            text = "rate limit"

            def json(self):
                return {
                    "error": {
                        "message": "rate limit",
                        "code": "rate_limit_exceeded",
                        "type": "rate_limit",
                    }
                }

        bot = ModelScopeBot.__new__(ModelScopeBot)
        bot.api_key = "test-key"
        bot.base_url = "https://modelscope.test/v1"
        session = SimpleNamespace(messages=[{"role": "user", "content": "hi"}])
        posts = []

        def fake_post(url, headers=None, json=None, stream=False, timeout=None, **_kwargs):
            posts.append({
                "url": url,
                "headers": headers,
                "json": json,
                "stream": stream,
                "timeout": timeout,
            })
            return FakeResponse()

        with patch("models.modelscope.modelscope_bot.requests.post", side_effect=fake_post):
            chunks = list(bot._handle_stream_response(
                session,
                {
                    "model": "Qwen/Qwen3.5-27B",
                    "retry_count": 1,
                    "model_max_retries": 2,
                    "model_retry_sleep": lambda _delay: None,
                    "session_id": "session-1",
                    "channel_type": "web",
                    "thinking": {"type": "disabled"},
                    "reasoning_effort": "high",
                },
            ))

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["status_code"], 429)
        self.assertEqual(chunks[0]["message"], "rate limit")
        self.assertEqual(chunks[0]["retry_after"], "0.5")
        self.assertEqual(chunks[0]["error"]["code"], "rate_limit_exceeded")
        sent_body = posts[0]["json"]
        self.assertTrue(posts[0]["stream"])
        self.assertFalse("retry_count" in sent_body)
        self.assertFalse("model_max_retries" in sent_body)
        self.assertFalse("model_retry_sleep" in sent_body)
        self.assertFalse("session_id" in sent_body)
        self.assertFalse("channel_type" in sent_body)
        self.assertFalse("thinking" in sent_body)
        self.assertFalse("reasoning_effort" in sent_body)

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

    def test_legacy_call_vision_gateway_records_usage_and_model(self):
        from models.legacy_reply_gateway import (
            LEGACY_CALL_VISION_API_PATH,
            wrap_legacy_call_vision,
        )
        from models.model_telemetry import get_recent_model_calls

        class VisionBot:
            def __init__(self):
                self.args = {"model": "default-vision"}

            def call_vision(self, image_url, question, model=None, max_tokens=1000):
                return {
                    "model": model or "default-vision",
                    "content": "ok",
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 4,
                        "total_tokens": 14,
                    },
                }

        bot = wrap_legacy_call_vision(VisionBot(), provider_hint="vision-provider")
        result = bot.call_vision(
            "data:image/png;base64,AAAA",
            "describe",
            model="vision-v1",
        )

        self.assertEqual(result["content"], "ok")
        events = get_recent_model_calls()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["provider"], "vision-provider")
        self.assertEqual(event["model"], "vision-v1")
        self.assertEqual(event["api_path"], LEGACY_CALL_VISION_API_PATH)
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["input_tokens"], 10)
        self.assertEqual(event["output_tokens"], 4)
        self.assertEqual(event["total_tokens"], 14)

    def test_legacy_call_vision_gateway_records_error_dict(self):
        from models.legacy_reply_gateway import wrap_legacy_call_vision
        from models.model_telemetry import get_recent_model_calls

        class VisionBot:
            def call_vision(self, image_url, question, model=None, max_tokens=1000):
                return {
                    "error": True,
                    "message": "bad image",
                    "status_code": 400,
                }

        bot = wrap_legacy_call_vision(
            VisionBot(),
            provider_hint="qianfan",
            model_hint="ernie-vision",
        )
        result = bot.call_vision("data:image/png;base64,AAAA", "describe")

        self.assertTrue(result["error"])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "qianfan")
        self.assertEqual(event["model"], "ernie-vision")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_taxonomy"], "client_error")
        self.assertEqual(event["error_status_code"], 400)
        self.assertEqual(event["error_message"], "bad image")

    def test_legacy_call_vision_gateway_treats_empty_content_as_failed(self):
        from models.legacy_reply_gateway import wrap_legacy_call_vision
        from models.model_telemetry import get_recent_model_calls

        class VisionBot:
            def call_vision(self, image_url, question, model=None, max_tokens=1000):
                return {
                    "model": model or "vision-v1",
                    "content": "",
                    "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
                }

        bot = wrap_legacy_call_vision(VisionBot(), provider_hint="vision-provider")
        result = bot.call_vision("data:image/png;base64,AAAA", "describe", model="vision-v1")

        self.assertEqual(result["content"], "")
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "vision-provider")
        self.assertEqual(event["model"], "vision-v1")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_message"], "Legacy call_vision returned empty content")
        self.assertEqual(event["total_tokens"], 1)

    def test_legacy_call_vision_gateway_parses_http_status_from_message(self):
        from models.legacy_reply_gateway import wrap_legacy_call_vision
        from models.model_telemetry import get_recent_model_calls

        class VisionBot:
            def call_vision(self, image_url, question, model=None, max_tokens=1000):
                return {
                    "error": True,
                    "message": "HTTP 429: rate limit",
                }

        bot = wrap_legacy_call_vision(
            VisionBot(),
            provider_hint="deepseek",
            model_hint="deepseek-vision",
        )
        result = bot.call_vision("data:image/png;base64,AAAA", "describe")

        self.assertTrue(result["error"])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "deepseek")
        self.assertEqual(event["model"], "deepseek-vision")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 429)
        self.assertEqual(event["error_taxonomy"], "rate_limit")
        self.assertEqual(event["error_message"], "HTTP 429: rate limit")

    def test_legacy_call_vision_gateway_records_exception_and_reraises(self):
        from models.legacy_reply_gateway import wrap_legacy_call_vision
        from models.model_telemetry import get_recent_model_calls

        class VisionBot:
            def call_vision(self, image_url, question, model=None, max_tokens=1000):
                raise TimeoutError("vision timeout")

        bot = wrap_legacy_call_vision(
            VisionBot(),
            provider_hint="gemini",
            model_hint="gemini-vision",
        )

        with self.assertRaises(TimeoutError):
            bot.call_vision("data:image/png;base64,AAAA", "describe")

        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "gemini")
        self.assertEqual(event["model"], "gemini-vision")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_taxonomy"], "timeout")
        self.assertEqual(event["error_status_code"], 500)

    def test_raw_vision_http_gateway_records_success(self):
        from unittest.mock import MagicMock, patch
        from agent.tools.vision.vision import RAW_VISION_API_PATH, Vision, VisionProvider
        from models.model_telemetry import get_recent_model_calls

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "a red square"}}],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 3,
                "total_tokens": 14,
            },
        }
        provider = VisionProvider(
            name="OpenAI",
            api_key="test-key",
            api_base="https://api.openai.test/v1",
        )

        with patch("agent.tools.vision.vision.requests.post", return_value=response) as post:
            result = Vision({})._call_api(
                provider,
                "gpt-4o-mini",
                "describe",
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result["content"], "a red square")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "gpt-4o-mini")
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "OpenAI")
        self.assertEqual(event["model"], "gpt-4o-mini")
        self.assertEqual(event["api_path"], RAW_VISION_API_PATH)
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["input_tokens"], 11)
        self.assertEqual(event["output_tokens"], 3)
        self.assertEqual(event["total_tokens"], 14)

    def test_raw_vision_http_gateway_records_non_200_failure(self):
        from unittest.mock import MagicMock, patch
        from agent.tools.vision.vision import Vision, VisionAPIError, VisionProvider
        from models.model_telemetry import get_recent_model_calls

        response = MagicMock()
        response.status_code = 429
        response.text = "rate limit"
        provider = VisionProvider(
            name="LinkAI",
            api_key="test-key",
            api_base="https://api.link-ai.test/v1",
        )

        with patch("agent.tools.vision.vision.requests.post", return_value=response):
            with self.assertRaises(VisionAPIError):
                Vision({})._call_api(
                    provider,
                    "gpt-4o-mini",
                    "describe",
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                )

        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "LinkAI")
        self.assertEqual(event["model"], "gpt-4o-mini")
        self.assertEqual(event["api_path"], "/vision/chat/completions")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 429)
        self.assertEqual(event["error_taxonomy"], "rate_limit")
        self.assertEqual(event["error_message"], "HTTP 429: rate limit")

    def test_raw_vision_http_gateway_records_non_200_json_error_body(self):
        from unittest.mock import MagicMock, patch
        from agent.tools.vision.vision import Vision, VisionAPIError, VisionProvider
        from models.model_telemetry import get_recent_model_calls

        response = MagicMock()
        response.status_code = 429
        response.text = '{"error":{"message":"too many","code":"rate_limit_exceeded","type":"rate_limit"}}'
        response.json.return_value = {
            "error": {
                "message": "too many",
                "code": "rate_limit_exceeded",
                "type": "rate_limit",
            }
        }
        provider = VisionProvider(
            name="OpenAI",
            api_key="test-key",
            api_base="https://api.openai.test/v1",
        )

        with patch("agent.tools.vision.vision.requests.post", return_value=response):
            with self.assertRaises(VisionAPIError):
                Vision({})._call_api(
                    provider,
                    "gpt-4o-mini",
                    "describe",
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                )

        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 429)
        self.assertEqual(event["error_taxonomy"], "rate_limit")
        self.assertEqual(event["error_code"], "rate_limit_exceeded")
        self.assertEqual(event["error_type"], "rate_limit")
        self.assertEqual(event["error_message"], "too many")

    def test_raw_vision_http_gateway_prefers_error_body_http_code(self):
        from unittest.mock import MagicMock, patch
        from agent.tools.vision.vision import Vision, VisionAPIError, VisionProvider
        from models.model_telemetry import get_recent_model_calls

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "error": {
                "message": "too many vision requests",
                "code": "rate_limit_exceeded",
                "type": "rate_limit",
                "status": "error",
                "http_code": 429,
            }
        }
        provider = VisionProvider(
            name="OpenAI",
            api_key="test-key",
            api_base="https://api.openai.test/v1",
        )

        with patch("agent.tools.vision.vision.requests.post", return_value=response):
            with self.assertRaises(VisionAPIError):
                Vision({})._call_api(
                    provider,
                    "gpt-4o-mini",
                    "describe",
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                )

        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 429)
        self.assertEqual(event["error_taxonomy"], "rate_limit")
        self.assertEqual(event["error_code"], "rate_limit_exceeded")
        self.assertEqual(event["error_type"], "rate_limit")
        self.assertEqual(event["error_message"], "too many vision requests")

    def test_raw_vision_http_gateway_handles_string_error_body(self):
        from unittest.mock import MagicMock, patch
        from agent.tools.vision.vision import Vision, VisionAPIError, VisionProvider
        from models.model_telemetry import get_recent_model_calls

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "error": "rate limit",
            "http_code": 429,
        }
        provider = VisionProvider(
            name="OpenAI",
            api_key="test-key",
            api_base="https://api.openai.test/v1",
        )

        with patch("agent.tools.vision.vision.requests.post", return_value=response):
            with self.assertRaises(VisionAPIError):
                Vision({})._call_api(
                    provider,
                    "gpt-4o-mini",
                    "describe",
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                )

        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 429)
        self.assertEqual(event["error_taxonomy"], "rate_limit")

    def test_raw_vision_http_gateway_handles_non_dict_usage(self):
        from unittest.mock import MagicMock, patch
        from agent.tools.vision.vision import Vision, VisionProvider
        from models.model_telemetry import get_recent_model_calls

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": None,
        }
        provider = VisionProvider(
            name="OpenAI",
            api_key="test-key",
            api_base="https://api.openai.test/v1",
        )

        with patch("agent.tools.vision.vision.requests.post", return_value=response):
            result = Vision({})._call_api(
                provider,
                "gpt-4o-mini",
                "describe",
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result["usage"]["total_tokens"], 0)
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["total_tokens"], 0)

    def test_raw_vision_http_gateway_records_timeout_and_reraises(self):
        from unittest.mock import patch
        import requests
        from agent.tools.vision.vision import Vision, VisionProvider
        from models.model_telemetry import get_recent_model_calls

        provider = VisionProvider(
            name="OpenAI",
            api_key="test-key",
            api_base="https://api.openai.test/v1",
        )

        with patch(
            "agent.tools.vision.vision.requests.post",
            side_effect=requests.Timeout("vision timeout"),
        ):
            with self.assertRaises(requests.Timeout):
                Vision({})._call_api(
                    provider,
                    "gpt-4o-mini",
                    "describe",
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                )

        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "OpenAI")
        self.assertEqual(event["model"], "gpt-4o-mini")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 504)
        self.assertEqual(event["error_taxonomy"], "timeout")

    def test_raw_vision_http_gateway_records_each_fallback_attempt(self):
        from unittest.mock import MagicMock, patch
        from agent.tools.vision.vision import Vision, VisionProvider
        from models.model_telemetry import get_recent_model_calls

        failed = MagicMock()
        failed.status_code = 500
        failed.text = "upstream unavailable"
        succeeded = MagicMock()
        succeeded.status_code = 200
        succeeded.json.return_value = {
            "choices": [{"message": {"content": "fallback ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }
        providers = [
            VisionProvider("OpenAI", "test-key", "https://api.openai.test/v1"),
            VisionProvider("LinkAI", "test-key", "https://api.link-ai.test/v1"),
        ]

        with patch(
            "agent.tools.vision.vision.requests.post",
            side_effect=[failed, succeeded],
        ):
            result = Vision({})._call_with_fallback(
                providers,
                "gpt-4o-mini",
                "describe",
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result["provider"], "LinkAI")
        events = get_recent_model_calls()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["provider"], "OpenAI")
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[0]["error_taxonomy"], "server_error")
        self.assertEqual(events[1]["provider"], "LinkAI")
        self.assertEqual(events[1]["status"], "completed")
        self.assertEqual(events[1]["total_tokens"], 5)

    def test_legacy_reply_image_gateway_records_success(self):
        import tempfile
        from unittest.mock import MagicMock, patch
        from bridge.context import Context, ContextType
        from bridge.reply import ReplyType
        from common import const
        from models.chatgpt.chat_gpt_bot import ChatGPTBot
        from models.model_telemetry import get_recent_model_calls

        class FakeClient:
            def __init__(self):
                self.calls = []

            def chat_completions(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "choices": [{"message": {"content": "legacy image ok"}}],
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 4,
                        "total_tokens": 11,
                    },
                }

        fake_conf = MagicMock()
        config = {
            "bot_type": const.OPENAI,
            "model": "gpt-4o",
            "open_ai_api_key": "test-key",
            "open_ai_api_base": "https://api.openai.test/v1",
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = ChatGPTBot.__new__(ChatGPTBot)
        bot._ecorex_route_bot_type = const.OPENAI
        bot._http_client = FakeClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "image.png")
            with open(image_path, "wb") as handle:
                handle.write(b"not-a-real-png")
            with patch("models.chatgpt.chat_gpt_bot.conf", return_value=fake_conf):
                reply = bot.reply_image(Context(ContextType.IMAGE, image_path, {"session_id": "s"}))

        self.assertEqual(reply.type, ReplyType.TEXT)
        self.assertEqual(reply.content, "legacy image ok")
        self.assertEqual(bot._http_client.calls[0]["model"], "gpt-4o")
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "openai")
        self.assertEqual(event["model"], "gpt-4o")
        self.assertEqual(event["api_path"], "/legacy/reply_image")
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["total_tokens"], 11)

    def test_legacy_reply_image_gateway_records_http_error(self):
        import tempfile
        from unittest.mock import MagicMock, patch
        from bridge.context import Context, ContextType
        from bridge.reply import ReplyType
        from common import const
        from models.chatgpt.chat_gpt_bot import ChatGPTBot
        from models.openai.openai_http_client import OpenAIHTTPError
        from models.model_telemetry import get_recent_model_calls

        class ErrorClient:
            def chat_completions(self, **kwargs):
                raise OpenAIHTTPError(
                    429,
                    {
                        "error": {
                            "message": "rate limit",
                            "code": "rate_limit_exceeded",
                            "type": "rate_limit",
                        }
                    },
                )

        fake_conf = MagicMock()
        config = {
            "bot_type": const.OPENAI,
            "model": "gpt-4o",
            "open_ai_api_key": "test-key",
            "open_ai_api_base": "https://api.openai.test/v1",
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = ChatGPTBot.__new__(ChatGPTBot)
        bot._ecorex_route_bot_type = const.OPENAI
        bot._http_client = ErrorClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "image.png")
            with open(image_path, "wb") as handle:
                handle.write(b"not-a-real-png")
            with patch("models.chatgpt.chat_gpt_bot.conf", return_value=fake_conf):
                reply = bot.reply_image(Context(ContextType.IMAGE, image_path, {"session_id": "s"}))

        self.assertEqual(reply.type, ReplyType.ERROR)
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "openai")
        self.assertEqual(event["model"], "gpt-4o")
        self.assertEqual(event["api_path"], "/legacy/reply_image")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 429)
        self.assertEqual(event["error_taxonomy"], "rate_limit")
        self.assertEqual(event["error_code"], "rate_limit_exceeded")
        self.assertEqual(event["error_type"], "rate_limit")
        self.assertEqual(event["error_message"], "rate limit")

    def test_legacy_reply_image_gateway_marks_malformed_content_failed(self):
        import tempfile
        from unittest.mock import MagicMock, patch
        from bridge.context import Context, ContextType
        from bridge.reply import ReplyType
        from common import const
        from models.chatgpt.chat_gpt_bot import ChatGPTBot
        from models.model_telemetry import get_recent_model_calls

        class FakeClient:
            def chat_completions(self, **kwargs):
                return {
                    "choices": [{"message": {"content": None}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
                }

        fake_conf = MagicMock()
        config = {
            "bot_type": const.OPENAI,
            "model": "gpt-4o",
            "open_ai_api_key": "test-key",
            "open_ai_api_base": "https://api.openai.test/v1",
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = ChatGPTBot.__new__(ChatGPTBot)
        bot._ecorex_route_bot_type = const.OPENAI
        bot._http_client = FakeClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "image.png")
            with open(image_path, "wb") as handle:
                handle.write(b"not-a-real-png")
            with patch("models.chatgpt.chat_gpt_bot.conf", return_value=fake_conf):
                reply = bot.reply_image(Context(ContextType.IMAGE, image_path, {"session_id": "s"}))

        self.assertEqual(reply.type, ReplyType.ERROR)
        event = get_recent_model_calls()[0]
        self.assertEqual(event["api_path"], "/legacy/reply_image")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["total_tokens"], 1)

    def test_linkai_chat_with_cached_image_records_telemetry(self):
        from unittest.mock import MagicMock, patch
        from bridge.context import Context, ContextType
        from bridge.reply import ReplyType
        from common import memory
        from models.linkai.link_ai_bot import LinkAIBot
        from models.model_telemetry import get_recent_model_calls

        class FakeSessions:
            def __init__(self):
                self.replies = []

            def session_msg_query(self, query, session_id):
                return [{"role": "user", "content": query}]

            def session_reply(self, reply, session_id, total_tokens, query=None):
                self.replies.append((reply, session_id, total_tokens, query))

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "image answer"}}],
            "usage": {"prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10},
        }
        fake_conf = MagicMock()
        config = {
            "linkai_api_key": "test-key",
            "linkai_api_base": "https://api.link-ai.test",
            "model": "gpt-4o-mini",
            "temperature": 0.7,
            "top_p": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0,
            "request_timeout": 180,
            "channel_type": "web",
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = LinkAIBot.__new__(LinkAIBot)
        bot.sessions = FakeSessions()
        bot._find_group_mapping_code = lambda context: None
        bot._fetch_agent_suffix = lambda response: ""
        bot._fetch_knowledge_search_suffix = lambda response: ""
        bot._process_url = lambda text: text
        multimodal_messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }]
        bot._process_image_msg = lambda **_kwargs: multimodal_messages
        session_id = "linkai-image-session"
        memory.USER_IMAGE_CACHE[session_id] = {"path": "image.png", "msg": MagicMock()}

        try:
            with patch("models.linkai.link_ai_bot.conf", return_value=fake_conf):
                with patch("models.linkai.link_ai_bot.requests.post", return_value=response) as post:
                    reply = bot._chat(
                        "describe",
                        Context(ContextType.TEXT, "describe", {"session_id": session_id}),
                    )
        finally:
            memory.USER_IMAGE_CACHE.pop(session_id, None)

        self.assertEqual(reply.type, ReplyType.TEXT)
        self.assertEqual(reply.content, "image answer")
        self.assertEqual(post.call_args.kwargs["json"]["messages"], multimodal_messages)
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "linkai")
        self.assertEqual(event["model"], "gpt-4o-mini")
        self.assertEqual(event["api_path"], "/legacy/linkai_chat")
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["total_tokens"], 10)

    def test_linkai_chat_records_non_200_json_error(self):
        from unittest.mock import MagicMock, patch
        from bridge.context import Context, ContextType
        from bridge.reply import ReplyType
        from models.linkai.link_ai_bot import LinkAIBot
        from models.model_telemetry import get_recent_model_calls

        class FakeSessions:
            def session_msg_query(self, query, session_id):
                return [{"role": "user", "content": query}]

        response = MagicMock()
        response.status_code = 429
        response.text = '{"error":{"message":"too many","code":"rate_limit_exceeded","type":"rate_limit"}}'
        response.json.return_value = {
            "error": {
                "message": "too many",
                "code": "rate_limit_exceeded",
                "type": "rate_limit",
            }
        }
        fake_conf = MagicMock()
        config = {
            "linkai_api_key": "test-key",
            "linkai_api_base": "https://api.link-ai.test",
            "model": "gpt-4o-mini",
            "temperature": 0.7,
            "top_p": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0,
            "request_timeout": 180,
            "channel_type": "web",
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = LinkAIBot.__new__(LinkAIBot)
        bot.sessions = FakeSessions()
        bot._find_group_mapping_code = lambda context: None

        with patch("models.linkai.link_ai_bot.conf", return_value=fake_conf):
            with patch("models.linkai.link_ai_bot.requests.post", return_value=response):
                reply = bot._chat(
                    "hello",
                    Context(ContextType.TEXT, "hello", {"session_id": "linkai-429-session"}),
                )

        self.assertEqual(reply.type, ReplyType.TEXT)
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "linkai")
        self.assertEqual(event["api_path"], "/legacy/linkai_chat")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 429)
        self.assertEqual(event["error_taxonomy"], "rate_limit")
        self.assertEqual(event["error_code"], "rate_limit_exceeded")
        self.assertEqual(event["error_type"], "rate_limit")
        self.assertEqual(event["error_message"], "too many")

    def test_linkai_chat_records_timeout_retry_attempts(self):
        from unittest.mock import MagicMock, patch
        import requests
        from bridge.context import Context, ContextType
        from bridge.reply import ReplyType
        from models.linkai.link_ai_bot import LinkAIBot
        from models.model_telemetry import get_recent_model_calls

        class FakeSessions:
            def session_msg_query(self, query, session_id):
                return [{"role": "user", "content": query}]

        fake_conf = MagicMock()
        config = {
            "linkai_api_key": "test-key",
            "linkai_api_base": "https://api.link-ai.test",
            "model": "gpt-4o-mini",
            "temperature": 0.7,
            "top_p": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0,
            "request_timeout": 180,
            "channel_type": "web",
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = LinkAIBot.__new__(LinkAIBot)
        bot.sessions = FakeSessions()
        bot._find_group_mapping_code = lambda context: None

        with patch("models.linkai.link_ai_bot.conf", return_value=fake_conf):
            with patch("models.linkai.link_ai_bot.time.sleep", lambda _seconds: None):
                with patch(
                    "models.linkai.link_ai_bot.requests.post",
                    side_effect=requests.Timeout("boom"),
                ):
                    reply = bot._chat(
                        "hello",
                        Context(ContextType.TEXT, "hello", {"session_id": "linkai-timeout-session"}),
                    )

        self.assertEqual(reply.type, ReplyType.TEXT)
        events = get_recent_model_calls()
        self.assertEqual(len(events), 3)
        self.assertEqual([event["retry_count"] for event in events], [0, 1, 2])
        for event in events:
            self.assertEqual(event["provider"], "linkai")
            self.assertEqual(event["api_path"], "/legacy/linkai_chat")
            self.assertEqual(event["status"], "failed")
            self.assertEqual(event["error_status_code"], 504)
            self.assertEqual(event["error_taxonomy"], "timeout")

    def test_linkai_chat_records_connection_retry_attempts(self):
        from unittest.mock import MagicMock, patch
        import requests
        from bridge.context import Context, ContextType
        from bridge.reply import ReplyType
        from models.linkai.link_ai_bot import LinkAIBot
        from models.model_telemetry import get_recent_model_calls

        class FakeSessions:
            def session_msg_query(self, query, session_id):
                return [{"role": "user", "content": query}]

        fake_conf = MagicMock()
        config = {
            "linkai_api_key": "test-key",
            "linkai_api_base": "https://api.link-ai.test",
            "model": "gpt-4o-mini",
            "temperature": 0.7,
            "top_p": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0,
            "request_timeout": 180,
            "channel_type": "web",
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = LinkAIBot.__new__(LinkAIBot)
        bot.sessions = FakeSessions()
        bot._find_group_mapping_code = lambda context: None

        with patch("models.linkai.link_ai_bot.conf", return_value=fake_conf):
            with patch("models.linkai.link_ai_bot.time.sleep", lambda _seconds: None):
                with patch(
                    "models.linkai.link_ai_bot.requests.post",
                    side_effect=requests.ConnectionError("dns failed"),
                ):
                    reply = bot._chat(
                        "hello",
                        Context(ContextType.TEXT, "hello", {"session_id": "linkai-connection-session"}),
                    )

        self.assertEqual(reply.type, ReplyType.TEXT)
        events = get_recent_model_calls()
        self.assertEqual(len(events), 3)
        self.assertEqual([event["retry_count"] for event in events], [0, 1, 2])
        for event in events:
            self.assertEqual(event["provider"], "linkai")
            self.assertEqual(event["api_path"], "/legacy/linkai_chat")
            self.assertEqual(event["status"], "failed")
            self.assertEqual(event["error_status_code"], 503)
            self.assertEqual(event["error_taxonomy"], "network_error")

    def test_legacy_create_img_gateway_records_success(self):
        from models.legacy_reply_gateway import (
            LEGACY_CREATE_IMAGE_API_PATH,
            wrap_legacy_create_img,
        )
        from models.model_telemetry import get_recent_model_calls

        class ImageBot:
            def create_img(self, query, retry_count=0, api_key=None):
                return True, "https://image.test/out.png"

        bot = wrap_legacy_create_img(
            ImageBot(),
            provider_hint="openai",
            model_hint="gpt-image-2-pro",
        )
        result = bot.create_img("draw a stable gateway")

        self.assertEqual(result, (True, "https://image.test/out.png"))
        events = get_recent_model_calls()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["provider"], "openai")
        self.assertEqual(event["model"], "gpt-image-2-pro")
        self.assertEqual(event["api_path"], LEGACY_CREATE_IMAGE_API_PATH)
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["retry_count"], 0)
        self.assertEqual(event["total_tokens"], 0)

    def test_legacy_create_img_gateway_records_false_result(self):
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls

        class ImageBot:
            def create_img(self, query, retry_count=0, api_key=None):
                return False, "HTTP 429: rate limit"

        bot = wrap_legacy_create_img(
            ImageBot(),
            provider_hint="linkai",
            model_hint="gpt-image-2-pro",
        )
        result = bot.create_img("draw too quickly")

        self.assertEqual(result, (False, "HTTP 429: rate limit"))
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "linkai")
        self.assertEqual(event["model"], "gpt-image-2-pro")
        self.assertEqual(event["api_path"], "/legacy/create_img")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 429)
        self.assertEqual(event["error_taxonomy"], "rate_limit")
        self.assertEqual(event["error_message"], "HTTP 429: rate limit")

    def test_openai_create_img_uses_shared_retry_after_then_records_success(self):
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls
        from models.openai.open_ai_image import OpenAIImage
        from models.openai.openai_http_client import OpenAIHTTPError

        class FakeImageClient:
            def __init__(self):
                self.calls = []
                self.responses = [
                    OpenAIHTTPError(
                        429,
                        {
                            "error": {
                                "message": "image rate limit",
                                "code": "rate_limit_exceeded",
                                "type": "rate_limit",
                            }
                        },
                        headers={"Retry-After": "0.25"},
                    ),
                    {"data": [{"url": "https://image.test/retry-ok.png"}]},
                ]

            def images_generate(self, **kwargs):
                self.calls.append(kwargs)
                response = self.responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        fake_conf = MagicMock()
        config = {
            "rate_limit_dalle": False,
            "text_to_image": "gpt-image-2-pro",
            "model_max_retries": 1,
            "image_output_format": "png",
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = OpenAIImage.__new__(OpenAIImage)
        bot._image_client = FakeImageClient()
        sleeps = []

        with patch("models.openai.open_ai_image.conf", return_value=fake_conf):
            wrapped = wrap_legacy_create_img(
                bot,
                provider_hint="openai",
                model_hint="gpt-image-2-pro",
            )
            result = wrapped.create_img("draw after shared retry", model_retry_sleep=sleeps.append)

        self.assertEqual(result, (True, "https://image.test/retry-ok.png"))
        self.assertEqual(len(bot._image_client.calls), 2)
        self.assertEqual(sleeps, [0.25])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "openai")
        self.assertEqual(event["model"], "gpt-image-2-pro")
        self.assertEqual(event["api_path"], "/legacy/create_img")
        self.assertEqual(event["status"], "completed")

    def test_openai_create_img_non_retryable_4xx_records_typed_fail_closed(self):
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls
        from models.openai.open_ai_image import OpenAIImage
        from models.openai.openai_http_client import OpenAIHTTPError

        class FakeImageClient:
            def __init__(self):
                self.calls = []

            def images_generate(self, **kwargs):
                self.calls.append(kwargs)
                raise OpenAIHTTPError(
                    400,
                    {
                        "error": {
                            "message": "bad image prompt",
                            "code": "invalid_request_error",
                            "type": "invalid_request_error",
                        }
                    },
                )

        fake_conf = MagicMock()
        config = {
            "rate_limit_dalle": False,
            "text_to_image": "gpt-image-2-pro",
            "model_max_retries": 2,
            "image_output_format": "png",
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = OpenAIImage.__new__(OpenAIImage)
        bot._image_client = FakeImageClient()
        sleeps = []

        with patch("models.openai.open_ai_image.conf", return_value=fake_conf):
            wrapped = wrap_legacy_create_img(
                bot,
                provider_hint="openai",
                model_hint="gpt-image-2-pro",
            )
            result = wrapped.create_img("bad prompt", model_retry_sleep=sleeps.append)

        self.assertFalse(result[0])
        self.assertEqual(len(bot._image_client.calls), 1)
        self.assertEqual(sleeps, [])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 400)
        self.assertEqual(event["error_taxonomy"], "client_error")
        self.assertEqual(event["error_code"], "invalid_request_error")
        self.assertEqual(event["error_type"], "invalid_request_error")
        self.assertEqual(event["error_message"], "bad image prompt")

    def test_openai_create_img_retryable_error_exhausts_shared_policy(self):
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls
        from models.openai.open_ai_image import OpenAIImage
        from models.openai.openai_http_client import OpenAIHTTPError

        class FakeImageClient:
            def __init__(self):
                self.calls = []

            def images_generate(self, **kwargs):
                self.calls.append(kwargs)
                raise OpenAIHTTPError(
                    503,
                    {
                        "error": {
                            "message": "image service unavailable",
                            "code": "server_error",
                            "type": "server_error",
                        }
                    },
                    headers={"Retry-After": "0.1"},
                )

        fake_conf = MagicMock()
        config = {
            "rate_limit_dalle": False,
            "text_to_image": "gpt-image-2-pro",
            "model_max_retries": 1,
            "image_output_format": "png",
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = OpenAIImage.__new__(OpenAIImage)
        bot._image_client = FakeImageClient()
        sleeps = []

        with patch("models.openai.open_ai_image.conf", return_value=fake_conf):
            wrapped = wrap_legacy_create_img(
                bot,
                provider_hint="openai",
                model_hint="gpt-image-2-pro",
            )
            result = wrapped.create_img("draw after outage", model_retry_sleep=sleeps.append)

        self.assertFalse(result[0])
        self.assertEqual(len(bot._image_client.calls), 2)
        self.assertEqual(sleeps, [0.1])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 503)
        self.assertEqual(event["error_taxonomy"], "server_error")
        self.assertEqual(event["error_code"], "server_error")
        self.assertEqual(event["error_type"], "server_error")
        self.assertEqual(event["error_message"], "image service unavailable")

    def test_openai_create_img_default_model_unavailable_falls_back_without_retry(self):
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls
        from models.openai.open_ai_image import OpenAIImage
        from models.openai.openai_http_client import OpenAIHTTPError

        class FakeImageClient:
            def __init__(self):
                self.calls = []
                self.responses = [
                    OpenAIHTTPError(
                        404,
                        {
                            "error": {
                                "message": "model_not_found",
                                "code": "model_not_found",
                                "type": "invalid_request_error",
                            }
                        },
                    ),
                    {"data": [{"url": "https://image.test/fallback.png"}]},
                ]

            def images_generate(self, **kwargs):
                self.calls.append(kwargs)
                response = self.responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        fake_conf = MagicMock()
        config = {
            "rate_limit_dalle": False,
            "text_to_image": "gpt-image-2-pro",
            "model_max_retries": 2,
            "image_output_format": "png",
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = OpenAIImage.__new__(OpenAIImage)
        bot._image_client = FakeImageClient()
        sleeps = []

        with patch("models.openai.open_ai_image.conf", return_value=fake_conf):
            wrapped = wrap_legacy_create_img(
                bot,
                provider_hint="openai",
                model_hint="gpt-image-2-pro",
            )
            result = wrapped.create_img("draw with fallback", model_retry_sleep=sleeps.append)

        self.assertEqual(result, (True, "https://image.test/fallback.png"))
        self.assertEqual(
            [call["model"] for call in bot._image_client.calls],
            ["gpt-image-2-pro", "gpt-image-2"],
        )
        self.assertEqual(sleeps, [])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "completed")

    def test_openai_create_img_lazily_initializes_rate_bucket_for_legacy_bot(self):
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls
        from models.openai.open_ai_image import OpenAIImage

        class FakeImageClient:
            def __init__(self):
                self.calls = []

            def images_generate(self, **kwargs):
                self.calls.append(kwargs)
                return {"data": [{"url": "https://image.test/rate-bucket.png"}]}

        class FakeTokenBucket:
            instances = []

            def __init__(self, tpm):
                self.tpm = tpm
                self.get_token_calls = 0
                FakeTokenBucket.instances.append(self)

            def get_token(self):
                self.get_token_calls += 1
                return True

        fake_conf = MagicMock()
        config = {
            "rate_limit_dalle": 50,
            "text_to_image": "gpt-image-2-pro",
            "model_max_retries": 0,
            "image_output_format": "png",
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = OpenAIImage.__new__(OpenAIImage)
        bot._image_client = FakeImageClient()
        self.assertFalse(hasattr(bot, "tb4dalle"))

        with patch("models.openai.open_ai_image.conf", return_value=fake_conf):
            with patch("models.openai.open_ai_image.TokenBucket", FakeTokenBucket):
                wrapped = wrap_legacy_create_img(
                    bot,
                    provider_hint="openai",
                    model_hint="gpt-image-2-pro",
                )
                result = wrapped.create_img("draw with legacy rate bucket")

        self.assertEqual(result, (True, "https://image.test/rate-bucket.png"))
        self.assertEqual([bucket.tpm for bucket in FakeTokenBucket.instances], [50])
        self.assertEqual([bucket.get_token_calls for bucket in FakeTokenBucket.instances], [1])
        self.assertTrue(hasattr(bot, "tb4dalle"))
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "completed")

    def test_openai_create_img_local_rate_limit_skips_request_with_typed_error(self):
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls
        from models.openai.open_ai_image import OpenAIImage

        class FakeImageClient:
            def __init__(self):
                self.calls = []

            def images_generate(self, **kwargs):
                self.calls.append(kwargs)
                return {"data": [{"url": "https://image.test/should-not-call.png"}]}

        class FakeTokenBucket:
            def __init__(self, tpm):
                self.tpm = tpm
                self.get_token_calls = 0

            def get_token(self):
                self.get_token_calls += 1
                return False

        fake_conf = MagicMock()
        config = {
            "rate_limit_dalle": 50,
            "text_to_image": "gpt-image-2-pro",
            "model_max_retries": 0,
            "image_output_format": "png",
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = OpenAIImage.__new__(OpenAIImage)
        bot._image_client = FakeImageClient()

        with patch("models.openai.open_ai_image.conf", return_value=fake_conf):
            with patch("models.openai.open_ai_image.TokenBucket", FakeTokenBucket):
                wrapped = wrap_legacy_create_img(
                    bot,
                    provider_hint="openai",
                    model_hint="gpt-image-2-pro",
                )
                result = wrapped.create_img("draw too quickly")

        self.assertFalse(result[0])
        self.assertEqual(bot.tb4dalle.get_token_calls, 1)
        self.assertEqual(bot._image_client.calls, [])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 429)
        self.assertEqual(event["error_taxonomy"], "rate_limit")
        self.assertEqual(event["error_code"], "local_rate_limit")
        self.assertEqual(event["error_type"], "rate_limit")

    def test_linkai_create_img_retryable_error_exhausts_shared_policy(self):
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.linkai.link_ai_bot import LinkAIBot
        from models.model_telemetry import get_recent_model_calls

        fake_conf = MagicMock()
        config = {
            "linkai_api_key": "test-key",
            "linkai_api_base": "https://api.link-ai.test",
            "text_to_image": "gpt-image-2-pro",
            "image_proxy": None,
            "model_max_retries": 1,
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)

        response = MagicMock()
        response.status_code = 503
        response.headers = {"Retry-After": "0.1"}
        response.text = '{"error":{"message":"image unavailable","code":"server_error","type":"server_error"}}'
        response.json.return_value = {
            "error": {
                "message": "image unavailable",
                "code": "server_error",
                "type": "server_error",
            }
        }
        sleeps = []

        with patch("models.linkai.link_ai_bot.conf", return_value=fake_conf):
            with patch("models.linkai.link_ai_bot.requests.post", return_value=response) as post:
                bot = wrap_legacy_create_img(
                    LinkAIBot.__new__(LinkAIBot),
                    provider_hint="linkai",
                    model_hint="gpt-image-2-pro",
                )
                result = bot.create_img("draw outage", model_retry_sleep=sleeps.append)

        self.assertFalse(result[0])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(sleeps, [0.1])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "linkai")
        self.assertEqual(event["api_path"], "/legacy/create_img")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 503)
        self.assertEqual(event["error_taxonomy"], "server_error")
        self.assertEqual(event["error_code"], "server_error")
        self.assertEqual(event["error_type"], "server_error")
        self.assertEqual(event["retry_attempt"], 1)
        self.assertEqual(event["max_retries"], 1)
        self.assertTrue(event["retry_exhausted"])
        self.assertEqual(event["retry_after_seconds"], 0.1)

    def test_zhipu_create_img_uses_shared_retry_after_then_records_success(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls
        from models.zhipuai.zhipu_ai_image import ZhipuAIImage

        class FakeSDKError(Exception):
            status_code = 429
            headers = {"Retry-After": "0.25"}
            body = {
                "error": {
                    "message": "zhipu image rate limit",
                    "code": "rate_limit_exceeded",
                    "type": "rate_limit",
                }
            }

        class FakeImages:
            def __init__(self):
                self.calls = []
                self.responses = [
                    FakeSDKError("zhipu image rate limit"),
                    SimpleNamespace(data=[SimpleNamespace(url="https://image.test/zhipu-retry.png")]),
                ]

            def generations(self, **kwargs):
                self.calls.append(kwargs)
                response = self.responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        fake_conf = MagicMock()
        config = {
            "rate_limit_dalle": False,
            "text_to_image": "cogview-3",
            "image_create_size": "1024x1024",
            "model_max_retries": 1,
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = ZhipuAIImage.__new__(ZhipuAIImage)
        bot.client = SimpleNamespace(images=FakeImages())
        sleeps = []

        with patch("models.zhipuai.zhipu_ai_image.conf", return_value=fake_conf):
            wrapped = wrap_legacy_create_img(
                bot,
                provider_hint="zhipu",
                model_hint="cogview-3",
            )
            result = wrapped.create_img("draw after zhipu retry", model_retry_sleep=sleeps.append)

        self.assertEqual(result, (True, "https://image.test/zhipu-retry.png"))
        self.assertEqual(len(bot.client.images.calls), 2)
        self.assertEqual(sleeps, [0.25])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "zhipu")
        self.assertEqual(event["model"], "cogview-3")
        self.assertEqual(event["api_path"], "/legacy/create_img")
        self.assertEqual(event["status"], "completed")

    def test_modelscope_create_img_retries_task_creation_then_polls_success(self):
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls
        from models.modelscope.modelscope_bot import ModelScopeBot

        fake_conf = MagicMock()
        config = {
            "text_to_image": "modelscope-image",
            "model_max_retries": 1,
            "modelscope_image_max_wait_times": 2,
            "modelscope_image_poll_interval": 0,
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = ModelScopeBot.__new__(ModelScopeBot)
        bot.api_key = "test-key"
        bot.base_url = "https://api.modelscope.test/v1"
        create_rate_limited = self._fake_response(
            429,
            {
                "error": {
                    "message": "modelscope image rate limit",
                    "code": "rate_limit_exceeded",
                    "type": "rate_limit",
                }
            },
            headers={"Retry-After": "0.2"},
        )
        create_ok = self._fake_response(200, {"task_id": "task-1"})
        poll_ok = self._fake_response(
            200,
            {
                "task_status": "SUCCEED",
                "output_images": ["https://image.test/modelscope-retry.png"],
            },
        )
        sleeps = []

        with patch("models.modelscope.modelscope_bot.conf", return_value=fake_conf):
            with patch(
                "models.modelscope.modelscope_bot.requests.post",
                side_effect=[create_rate_limited, create_ok],
            ) as post:
                with patch("models.modelscope.modelscope_bot.requests.get", return_value=poll_ok) as get:
                    wrapped = wrap_legacy_create_img(
                        bot,
                        provider_hint="modelscope",
                        model_hint="modelscope-image",
                    )
                    result = wrapped.create_img("draw after modelscope retry", model_retry_sleep=sleeps.append)

        self.assertEqual(result, (True, "https://image.test/modelscope-retry.png"))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(sleeps, [0.2])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "modelscope")
        self.assertEqual(event["model"], "modelscope-image")
        self.assertEqual(event["api_path"], "/legacy/create_img")
        self.assertEqual(event["status"], "completed")

    def test_modelscope_create_img_non_retryable_create_4xx_records_fail_closed(self):
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls
        from models.modelscope.modelscope_bot import ModelScopeBot

        fake_conf = MagicMock()
        config = {
            "text_to_image": "modelscope-image",
            "model_max_retries": 2,
            "modelscope_image_max_wait_times": 2,
            "modelscope_image_poll_interval": 0,
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = ModelScopeBot.__new__(ModelScopeBot)
        bot.api_key = "test-key"
        bot.base_url = "https://api.modelscope.test/v1"
        create_bad_request = self._fake_response(
            400,
            {
                "error": {
                    "message": "bad image prompt",
                    "code": "invalid_prompt",
                    "type": "invalid_request_error",
                }
            },
        )
        sleeps = []

        with patch("models.modelscope.modelscope_bot.conf", return_value=fake_conf):
            with patch("models.modelscope.modelscope_bot.requests.post", return_value=create_bad_request) as post:
                with patch("models.modelscope.modelscope_bot.requests.get") as get:
                    wrapped = wrap_legacy_create_img(
                        bot,
                        provider_hint="modelscope",
                        model_hint="modelscope-image",
                    )
                    result = wrapped.create_img("bad prompt", model_retry_sleep=sleeps.append)

        self.assertFalse(result[0])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 0)
        self.assertEqual(sleeps, [])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 400)
        self.assertEqual(event["error_taxonomy"], "client_error")
        self.assertEqual(event["error_code"], "invalid_prompt")
        self.assertEqual(event["error_type"], "invalid_request_error")
        self.assertEqual(event["error_message"], "bad image prompt")
        self.assertFalse(event["retryable"])
        self.assertEqual(event["retry_attempt"], 0)
        self.assertEqual(event["max_retries"], 2)

    def test_modelscope_create_img_poll_4xx_fails_closed_with_typed_evidence(self):
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls
        from models.modelscope.modelscope_bot import ModelScopeBot

        fake_conf = MagicMock()
        config = {
            "text_to_image": "modelscope-image",
            "model_max_retries": 1,
            "modelscope_image_max_wait_times": 2,
            "modelscope_image_poll_interval": 0,
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = ModelScopeBot.__new__(ModelScopeBot)
        bot.api_key = "test-key"
        bot.base_url = "https://api.modelscope.test/v1"
        create_ok = self._fake_response(200, {"task_id": "task-bad-poll"})
        poll_bad_request = self._fake_response(
            400,
            {
                "error": {
                    "message": "bad poll request",
                    "code": "invalid_task",
                    "type": "invalid_request_error",
                }
            },
        )

        with patch("models.modelscope.modelscope_bot.conf", return_value=fake_conf):
            with patch("models.modelscope.modelscope_bot.requests.post", return_value=create_ok) as post:
                with patch("models.modelscope.modelscope_bot.requests.get", return_value=poll_bad_request) as get:
                    wrapped = wrap_legacy_create_img(
                        bot,
                        provider_hint="modelscope",
                        model_hint="modelscope-image",
                    )
                    result = wrapped.create_img("draw bad poll")

        self.assertFalse(result[0])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 1)
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 400)
        self.assertEqual(event["error_taxonomy"], "client_error")
        self.assertEqual(event["error_code"], "invalid_task")
        self.assertEqual(event["error_type"], "invalid_request_error")
        self.assertEqual(event["error_message"], "bad poll request")
        self.assertFalse(event["retryable"])
        self.assertEqual(event["retry_attempt"], 0)
        self.assertEqual(event["max_retries"], 1)

    def test_modelscope_create_img_retryable_poll_error_preserves_exhausted_evidence(self):
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls
        from models.modelscope.modelscope_bot import ModelScopeBot

        fake_conf = MagicMock()
        config = {
            "text_to_image": "modelscope-image",
            "model_max_retries": 1,
            "modelscope_image_max_wait_times": 2,
            "modelscope_image_poll_interval": 0,
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = ModelScopeBot.__new__(ModelScopeBot)
        bot.api_key = "test-key"
        bot.base_url = "https://api.modelscope.test/v1"
        create_ok = self._fake_response(200, {"task_id": "task-poll-outage"})
        poll_outage = self._fake_response(
            503,
            {
                "error": {
                    "message": "poll service unavailable",
                    "code": "server_error",
                    "type": "server_error",
                }
            },
            headers={"Retry-After": "0.1"},
        )
        sleeps = []

        with patch("models.modelscope.modelscope_bot.conf", return_value=fake_conf):
            with patch("models.modelscope.modelscope_bot.requests.post", return_value=create_ok) as post:
                with patch("models.modelscope.modelscope_bot.requests.get", return_value=poll_outage) as get:
                    wrapped = wrap_legacy_create_img(
                        bot,
                        provider_hint="modelscope",
                        model_hint="modelscope-image",
                    )
                    result = wrapped.create_img("draw during poll outage", model_retry_sleep=sleeps.append)

        self.assertFalse(result[0])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(sleeps, [0.1])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 503)
        self.assertEqual(event["error_taxonomy"], "server_error")
        self.assertEqual(event["error_code"], "server_error")
        self.assertEqual(event["error_type"], "server_error")
        self.assertEqual(event["error_message"], "poll service unavailable")
        self.assertTrue(event["retryable"])
        self.assertEqual(event["retry_attempt"], 1)
        self.assertEqual(event["max_retries"], 1)
        self.assertTrue(event["retry_exhausted"])
        self.assertEqual(event["retry_after_seconds"], 0.1)

    def test_modelscope_create_img_task_timeout_records_typed_timeout(self):
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls
        from models.modelscope.modelscope_bot import ModelScopeBot

        fake_conf = MagicMock()
        config = {
            "text_to_image": "modelscope-image",
            "model_max_retries": 1,
            "modelscope_image_max_wait_times": 2,
            "modelscope_image_poll_interval": 0,
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = ModelScopeBot.__new__(ModelScopeBot)
        bot.api_key = "test-key"
        bot.base_url = "https://api.modelscope.test/v1"
        create_ok = self._fake_response(200, {"task_id": "task-timeout"})
        poll_running = self._fake_response(200, {"task_status": "RUNNING"})

        with patch("models.modelscope.modelscope_bot.conf", return_value=fake_conf):
            with patch("models.modelscope.modelscope_bot.requests.post", return_value=create_ok) as post:
                with patch("models.modelscope.modelscope_bot.requests.get", return_value=poll_running) as get:
                    wrapped = wrap_legacy_create_img(
                        bot,
                        provider_hint="modelscope",
                        model_hint="modelscope-image",
                    )
                    result = wrapped.create_img("draw too slowly")

        self.assertFalse(result[0])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_count, 2)
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 504)
        self.assertEqual(event["error_taxonomy"], "timeout")
        self.assertEqual(event["error_code"], "task_timeout")
        self.assertEqual(event["error_type"], "timeout")
        self.assertEqual(event["error_message"], "ModelScope image task timed out")

    def test_openai_create_img_error_sidecar_is_thread_local(self):
        import threading
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls
        from models.openai.open_ai_image import OpenAIImage
        from models.openai.openai_http_client import OpenAIHTTPError

        class FakeImageClient:
            def images_generate(self, **kwargs):
                prompt = kwargs.get("prompt") or ""
                if "400" in prompt:
                    raise OpenAIHTTPError(
                        400,
                        {
                            "error": {
                                "message": "bad 400",
                                "code": "bad_400",
                                "type": "invalid_request_error",
                            }
                        },
                    )
                raise OpenAIHTTPError(
                    503,
                    {
                        "error": {
                            "message": "bad 503",
                            "code": "bad_503",
                            "type": "server_error",
                        }
                    },
                )

        class ConcurrentOpenAIImage(OpenAIImage):
            def __init__(self):
                self._image_client = FakeImageClient()
                self.barrier = threading.Barrier(2)

            def _set_create_img_error(self, details, decision=None):
                super()._set_create_img_error(details, decision)
                self.barrier.wait(timeout=5)

        fake_conf = MagicMock()
        config = {
            "rate_limit_dalle": False,
            "text_to_image": "gpt-image-2-pro",
            "model_max_retries": 0,
            "image_output_format": "png",
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        bot = ConcurrentOpenAIImage()
        errors = []
        results = []

        with patch("models.openai.open_ai_image.conf", return_value=fake_conf):
            wrapped = wrap_legacy_create_img(
                bot,
                provider_hint="openai",
                model_hint="gpt-image-2-pro",
            )
            self.assertIsNotNone(getattr(bot, "_ecorex_create_img_error_state", None))

            def invoke(prompt):
                try:
                    results.append(wrapped.create_img(prompt))
                except Exception as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=invoke, args=("draw bad 400",)),
                threading.Thread(target=invoke, args=("draw bad 503",)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        events = get_recent_model_calls()
        self.assertEqual(len(events), 2)
        by_status = {
            event["error_status_code"]: event["error_message"]
            for event in events
        }
        self.assertEqual(by_status[400], "bad 400")
        self.assertEqual(by_status[503], "bad 503")

    def test_legacy_create_img_gateway_prefers_numeric_http_code(self):
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls

        class ImageBot:
            def create_img(self, query, retry_count=0, api_key=None):
                return False, {
                    "status": "error",
                    "http_code": 429,
                    "error": {
                        "message": "too many image requests",
                        "status": "error",
                        "http_code": 500,
                    },
                }

        bot = wrap_legacy_create_img(
            ImageBot(),
            provider_hint="linkai",
            model_hint="gpt-image-2-pro",
        )
        result = bot.create_img("draw too quickly")

        self.assertFalse(result[0])
        event = get_recent_model_calls()[0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_status_code"], 429)
        self.assertEqual(event["error_taxonomy"], "rate_limit")
        self.assertEqual(event["error_message"], "too many image requests")

    def test_legacy_create_img_gateway_uses_zhipu_default_image_model(self):
        from unittest.mock import MagicMock, patch
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls

        class ImageBot:
            def get_api_config(self):
                return {"provider": "zhipu", "model": "glm-4"}

            def create_img(self, query, retry_count=0, api_key=None):
                return True, "https://image.test/zhipu.png"

        fake_conf = MagicMock()
        fake_conf.get.side_effect = lambda key, default=None: default

        with patch("config.conf", return_value=fake_conf):
            bot = wrap_legacy_create_img(ImageBot(), provider_hint="zhipu")
            result = bot.create_img("draw with zhipu")

        self.assertEqual(result, (True, "https://image.test/zhipu.png"))
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "zhipu")
        self.assertEqual(event["model"], "cogview-3")
        self.assertEqual(event["status"], "completed")

    def test_legacy_create_img_gateway_records_exception_and_reraises(self):
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls

        class ImageBot:
            def create_img(self, query, retry_count=0, api_key=None):
                raise TimeoutError("image timeout")

        bot = wrap_legacy_create_img(
            ImageBot(),
            provider_hint="openai",
            model_hint="gpt-image-2-pro",
        )

        with self.assertRaises(TimeoutError):
            bot.create_img("draw timeout")

        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "openai")
        self.assertEqual(event["model"], "gpt-image-2-pro")
        self.assertEqual(event["api_path"], "/legacy/create_img")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_taxonomy"], "timeout")
        self.assertEqual(event["error_status_code"], 500)

    def test_legacy_create_img_gateway_records_one_span_for_internal_retry(self):
        from models.legacy_reply_gateway import wrap_legacy_create_img
        from models.model_telemetry import get_recent_model_calls

        class RecursiveImageBot:
            def __init__(self):
                self.calls = []

            def create_img(self, query, retry_count=0, api_key=None):
                self.calls.append(retry_count)
                if retry_count == 0:
                    return self.create_img(query, retry_count=1, api_key=api_key)
                return True, "https://image.test/retry.png"

        bot = wrap_legacy_create_img(
            RecursiveImageBot(),
            provider_hint="openai",
            model_hint="gpt-image-2",
        )
        result = bot.create_img("draw after retry")

        self.assertEqual(result, (True, "https://image.test/retry.png"))
        self.assertEqual(bot.calls, [0, 1])
        events = get_recent_model_calls()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["model"], "gpt-image-2")
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["retry_count"], 0)

    def test_legacy_model_surfaces_wraps_create_img(self):
        from models.legacy_reply_gateway import wrap_legacy_model_surfaces
        from models.model_telemetry import get_recent_model_calls

        class MultiSurfaceBot:
            def reply_text(self, session):
                return {"total_tokens": 1, "completion_tokens": 1, "content": "ok"}

            def call_vision(self, image_url, question, model=None, max_tokens=1000):
                return {"content": "ok"}

            def create_img(self, query, retry_count=0, api_key=None):
                return True, "https://image.test/surface.png"

        bot = wrap_legacy_model_surfaces(
            MultiSurfaceBot(),
            provider_hint="custom",
            model_hint="image-model",
        )

        self.assertTrue(getattr(bot.reply_text, "_ecorex_legacy_reply_gateway", False))
        self.assertTrue(getattr(bot.call_vision, "_ecorex_legacy_call_vision_gateway", False))
        self.assertTrue(getattr(bot.create_img, "_ecorex_legacy_create_img_gateway", False))
        self.assertEqual(bot.create_img("draw"), (True, "https://image.test/surface.png"))

        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], "custom")
        self.assertEqual(event["model"], "image-model")
        self.assertEqual(event["api_path"], "/legacy/create_img")
        self.assertEqual(event["status"], "completed")

    def test_bot_factory_wraps_legacy_call_vision(self):
        from unittest.mock import MagicMock, patch
        from common import const
        from models.bot_factory import create_bot
        from models.model_telemetry import get_recent_model_calls

        fake_conf = MagicMock()
        config = {
            "model": "qianfan",
            "qianfan_api_key": "test-key",
            "qianfan_api_base": "https://qianfan.test/v2",
            "conversation_max_tokens": 1000,
            "expires_in_seconds": 3600,
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "model": "ernie-4.5-turbo-vl",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }

        with patch("models.qianfan.qianfan_bot.conf", return_value=fake_conf):
            with patch("models.qianfan.qianfan_bot.SessionManager"):
                bot = create_bot(const.QIANFAN)
                self.assertTrue(
                    getattr(bot.reply_text, "_ecorex_legacy_reply_gateway", False)
                )
                self.assertTrue(
                    getattr(bot.call_vision, "_ecorex_legacy_call_vision_gateway", False)
                )
                with patch("models.qianfan.qianfan_bot.requests.post", return_value=fake_response):
                    result = bot.call_vision(
                        "data:image/png;base64,AAAA",
                        "describe",
                        model="ernie-4.5-turbo-vl",
                    )

        self.assertEqual(result["content"], "ok")
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], const.QIANFAN)
        self.assertEqual(event["model"], "ernie-4.5-turbo-vl")
        self.assertEqual(event["api_path"], "/legacy/call_vision")
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["total_tokens"], 5)

    def test_bot_factory_wrapped_qianfan_call_vision_error_records_telemetry(self):
        from unittest.mock import MagicMock, patch
        from common import const
        from models.bot_factory import create_bot
        from models.model_telemetry import get_recent_model_calls

        fake_conf = MagicMock()
        config = {
            "model": "qianfan",
            "qianfan_api_key": "test-key",
            "qianfan_api_base": "https://qianfan.test/v2",
            "conversation_max_tokens": 1000,
            "expires_in_seconds": 3600,
        }
        fake_conf.get.side_effect = lambda key, default=None: config.get(key, default)
        fake_response = MagicMock()
        fake_response.status_code = 400
        fake_response.json.return_value = {"error": {"message": "bad image"}}
        fake_response.text = '{"error":{"message":"bad image"}}'

        with patch("models.qianfan.qianfan_bot.conf", return_value=fake_conf):
            with patch("models.qianfan.qianfan_bot.SessionManager"):
                bot = create_bot(const.QIANFAN)
                with patch("models.qianfan.qianfan_bot.requests.post", return_value=fake_response):
                    result = bot.call_vision(
                        "data:image/png;base64,AAAA",
                        "describe",
                        model="ernie-4.5-turbo-vl",
                    )

        self.assertTrue(result["error"])
        self.assertFalse("status_code" in result)
        event = get_recent_model_calls()[0]
        self.assertEqual(event["provider"], const.QIANFAN)
        self.assertEqual(event["model"], "ernie-4.5-turbo-vl")
        self.assertEqual(event["api_path"], "/legacy/call_vision")
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_taxonomy"], "unknown")
        self.assertEqual(event["error_status_code"], None)
        self.assertIn("bad image", event["error_message"])

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
