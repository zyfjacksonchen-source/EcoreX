# encoding:utf-8
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _restore_conf_value(key, previous_value):
    from config import conf

    if previous_value is None:
        conf().pop(key, None)
    else:
        conf()[key] = previous_value


class TestOpenAIResponsesAdapter(unittest.TestCase):
    def test_responses_state_store_persists_state_without_raw_session_id(self):
        from models.openai.responses_adapter import ResponsesState
        from models.openai.responses_state_store import (
            clear_responses_state,
            load_responses_state,
            responses_state_path,
            save_responses_state,
        )

        with tempfile.TemporaryDirectory() as workspace:
            session_id = "customer@example.com/private-session"
            loaded = load_responses_state(
                session_id=session_id,
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
            )
            self.assertTrue(loaded.prompt_cache_key.startswith("ecorex:"))
            self.assertNotIn("customer", loaded.prompt_cache_key)

            saved = save_responses_state(
                session_id=session_id,
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
                state=ResponsesState(
                    previous_response_id="resp_saved",
                    prompt_cache_key=loaded.prompt_cache_key,
                    prompt_cache_retention="24h",
                    service_tier="priority",
                ),
            )
            self.assertEqual(saved.previous_response_id, "resp_saved")

            path = responses_state_path(workspace)
            payload = json.loads(path.read_text(encoding="utf-8"))
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertIn("resp_saved", serialized)
            self.assertNotIn(session_id, serialized)

            reloaded = load_responses_state(
                session_id=session_id,
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
            )
            self.assertEqual(reloaded.previous_response_id, "resp_saved")
            self.assertEqual(reloaded.prompt_cache_key, loaded.prompt_cache_key)
            self.assertEqual(reloaded.prompt_cache_retention, "24h")
            self.assertTrue(clear_responses_state(
                session_id=session_id,
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
            ))
            cleared = load_responses_state(
                session_id=session_id,
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
            )
            self.assertIsNone(cleared.previous_response_id)

    def test_responses_state_store_recovers_malformed_sessions_bucket(self):
        from models.openai.responses_adapter import ResponsesState
        from models.openai.responses_state_store import (
            clear_responses_state,
            load_responses_state,
            responses_state_path,
            save_responses_state,
        )

        with tempfile.TemporaryDirectory() as workspace:
            path = responses_state_path(workspace)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"schemaVersion": 1, "sessions": ["bad"]}),
                encoding="utf-8",
            )

            saved = save_responses_state(
                session_id="session-malformed",
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
                state=ResponsesState(previous_response_id="resp_recovered"),
            )
            self.assertEqual(saved.previous_response_id, "resp_recovered")
            reloaded = load_responses_state(
                session_id="session-malformed",
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
            )
            self.assertEqual(reloaded.previous_response_id, "resp_recovered")
            self.assertTrue(clear_responses_state(
                session_id="session-malformed",
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
            ))

    def test_build_plan_carries_responses_state_and_tool_shape(self):
        from models.openai.responses_adapter import ResponsesState, build_responses_plan

        state = ResponsesState(
            previous_response_id="resp_previous",
            prompt_cache_key="tenant:stable-prefix",
            prompt_cache_retention="24h",
            service_tier="priority",
            truncation="auto",
        )
        plan = build_responses_plan(
            model="gpt-5.5",
            messages=[
                {"role": "system", "content": "Stay concise."},
                {"role": "user", "content": "Check order ORDER-1"},
            ],
            tools=[{
                "type": "function",
                "function": {
                    "name": "lookup_order",
                    "description": "Lookup an order",
                    "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}},
                    "strict": True,
                },
            }],
            stream=True,
            state=state,
            max_tokens=700,
        )

        payload = plan.create_payload
        self.assertEqual(plan.create_path, "/responses")
        self.assertEqual(plan.compact_path, "/responses/compact")
        self.assertEqual(plan.input_tokens_path, "/responses/input_tokens")
        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["instructions"], "Stay concise.")
        self.assertEqual(payload["previous_response_id"], "resp_previous")
        self.assertEqual(payload["prompt_cache_key"], "tenant:stable-prefix")
        self.assertEqual(payload["prompt_cache_retention"], "24h")
        self.assertEqual(payload["service_tier"], "priority")
        self.assertEqual(payload["truncation"], "auto")
        self.assertEqual(payload["max_output_tokens"], 700)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(payload["input"], [{
            "role": "user",
            "content": [{"type": "input_text", "text": "Check order ORDER-1"}],
        }])
        self.assertEqual(payload["tools"], [{
            "type": "function",
            "name": "lookup_order",
            "description": "Lookup an order",
            "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}},
            "strict": True,
        }])
        self.assertEqual(payload["tool_choice"], "auto")

    def test_compaction_and_token_count_payloads_share_input_and_instructions(self):
        from models.openai.responses_adapter import build_responses_plan

        plan = build_responses_plan(
            model="gpt-5.5",
            messages=[
                {"role": "developer", "content": "Use policy v3."},
                {"role": "user", "content": [{"type": "text", "text": "Summarize the case."}]},
            ],
        )

        expected_input = [{
            "role": "user",
            "content": [{"type": "input_text", "text": "Summarize the case."}],
        }]
        self.assertEqual(plan.compact_payload, {
            "model": "gpt-5.5",
            "input": expected_input,
            "instructions": "Use policy v3.",
        })
        self.assertEqual(plan.input_tokens_payload, plan.compact_payload)

    def test_tool_call_history_maps_to_responses_items(self):
        from models.openai.responses_adapter import chat_messages_to_responses_input

        items, instructions = chat_messages_to_responses_input([
            {
                "role": "assistant",
                "content": "I will check.",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup_order", "arguments": "{\"order_id\":\"ORDER-1\"}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "{\"status\":\"delayed\"}"},
        ])

        self.assertIsNone(instructions)
        self.assertEqual(items[0], {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "I will check."}],
        })
        self.assertEqual(items[1], {
            "type": "function_call",
            "call_id": "call_1",
            "name": "lookup_order",
            "arguments": "{\"order_id\":\"ORDER-1\"}",
            "status": "completed",
        })
        self.assertEqual(items[2], {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "{\"status\":\"delayed\"}",
        })

    def test_responses_stream_events_normalize_to_chat_chunks(self):
        from models.openai.responses_adapter import normalize_responses_stream_events_to_chat

        completed = []
        chunks = list(normalize_responses_stream_events_to_chat(
            [
                {
                    "type": "response.created",
                    "response": {
                        "id": "resp_stream",
                        "model": "gpt-5.5",
                        "created_at": 123,
                    },
                },
                {"type": "response.output_text.delta", "delta": "Hel"},
                {"type": "response.output_text.delta", "delta": "lo"},
                {
                    "type": "response.output_item.added",
                    "output_index": 1,
                    "item": {
                        "id": "fc_item",
                        "type": "function_call",
                        "call_id": "call_weather",
                        "name": "lookup_weather",
                    },
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 1,
                    "item_id": "fc_item",
                    "delta": "{\"city\"",
                },
                {
                    "type": "response.function_call_arguments.done",
                    "output_index": 1,
                    "item_id": "fc_item",
                    "arguments": "{\"city\":\"SF\"}",
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_stream",
                        "model": "gpt-5.5",
                        "status": "completed",
                        "service_tier": "priority",
                        "usage": {
                            "input_tokens": 7,
                            "output_tokens": 3,
                            "total_tokens": 10,
                        },
                    },
                },
            ],
            on_completed=completed.append,
        ))

        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "Hel")
        self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], "lo")
        tool_start = chunks[2]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(tool_start["id"], "call_weather")
        self.assertEqual(tool_start["function"]["name"], "lookup_weather")
        self.assertEqual(chunks[3]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"], "{\"city\"")
        self.assertEqual(chunks[4]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"], ":\"SF\"}")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(chunks[-1]["usage"]["total_tokens"], 10)
        self.assertEqual(completed[0]["id"], "resp_stream")

    def test_responses_stream_done_event_can_start_tool_call(self):
        from models.openai.responses_adapter import normalize_responses_stream_events_to_chat

        chunks = list(normalize_responses_stream_events_to_chat([
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "fc_done",
                "name": "lookup_order",
                "arguments": "{\"order_id\":\"ORDER-1\"}",
            },
        ]))

        tool_delta = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(tool_delta["function"]["name"], "lookup_order")
        self.assertEqual(tool_delta["function"]["arguments"], "{\"order_id\":\"ORDER-1\"}")

    def test_responses_stream_error_keeps_code_out_of_status_code(self):
        from models.openai.responses_adapter import normalize_responses_stream_events_to_chat

        chunks = list(normalize_responses_stream_events_to_chat([
            {
                "type": "error",
                "code": "rate_limit_exceeded",
                "message": "slow down",
            },
        ]))

        self.assertEqual(chunks[0]["error"]["code"], "rate_limit_exceeded")
        self.assertEqual(chunks[0]["status_code"], 500)

    def test_responses_stream_completed_callback_failure_does_not_drop_output(self):
        from models.openai.responses_adapter import normalize_responses_stream_events_to_chat

        def raise_on_completed(_response):
            raise RuntimeError("state store unavailable")

        chunks = list(normalize_responses_stream_events_to_chat(
            [
                {"type": "response.output_text.delta", "delta": "done"},
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_done",
                        "model": "gpt-5.5",
                        "status": "completed",
                    },
                },
            ],
            on_completed=raise_on_completed,
        ))

        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "done")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")

    def test_responses_stream_refusal_events_emit_content_once(self):
        from models.openai.responses_adapter import normalize_responses_stream_events_to_chat

        chunks = list(normalize_responses_stream_events_to_chat([
            {
                "type": "response.refusal.delta",
                "item_id": "msg_refusal",
                "output_index": 0,
                "content_index": 0,
                "delta": "I can't",
            },
            {
                "type": "response.refusal.done",
                "item_id": "msg_refusal",
                "output_index": 0,
                "content_index": 0,
                "refusal": "I can't help with that.",
            },
        ]))

        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "I can't")
        self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], " help with that.")

    def test_decision_gate_requires_explicit_enable_and_official_openai_host(self):
        from models.openai.responses_adapter import decide_responses_adapter

        disabled = decide_responses_adapter(
            {"provider": "openai", "api_base": "https://api.openai.com/v1"},
            {},
        )
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.reason, "disabled")

        enabled = decide_responses_adapter(
            {"provider": "openai", "api_base": "https://api.openai.com/v1"},
            {"use_responses_api": True},
        )
        self.assertTrue(enabled.enabled)

        custom = decide_responses_adapter(
            {"provider": "custom", "api_base": "https://api.openai.com/v1"},
            {"use_responses_api": True},
        )
        self.assertFalse(custom.enabled)
        self.assertEqual(custom.reason, "non_official_openai_provider")

        insecure = decide_responses_adapter(
            {"provider": "openai", "api_base": "http://api.openai.com/v1"},
            {"use_responses_api": True},
        )
        self.assertFalse(insecure.enabled)
        self.assertEqual(insecure.reason, "non_official_openai_provider")

    def test_planning_hook_does_not_switch_unless_enabled(self):
        from models.openai.responses_adapter import ResponsesState
        from models.openai.responses_state_store import save_responses_state
        from models.openai_compatible_bot import OpenAICompatibleBot

        class CaptureBot(OpenAICompatibleBot):
            def get_api_config(self):
                return {
                    "provider": "openai",
                    "api_key": "test-key",
                    "api_base": "https://api.openai.com/v1",
                    "model": "gpt-5.5",
                    "responses_service_tier": "flex",
                    "responses_prompt_cache_retention": "24h",
                }

        bot = CaptureBot()
        self.assertIsNone(bot.plan_responses_api_call([{"role": "user", "content": "hi"}]))

        plan = bot.plan_responses_api_call(
            [{"role": "system", "content": "Be direct."}, {"role": "user", "content": "hi"}],
            stream=True,
            use_responses_api=True,
            previous_response_id="resp_123",
        )
        self.assertEqual(plan.create_payload["previous_response_id"], "resp_123")
        self.assertEqual(plan.create_payload["service_tier"], "flex")
        self.assertEqual(plan.create_payload["prompt_cache_retention"], "24h")
        self.assertEqual(plan.create_payload["instructions"], "Be direct.")

        with tempfile.TemporaryDirectory() as workspace:
            save_responses_state(
                session_id="session-planned",
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
                state=ResponsesState(
                    previous_response_id="resp_persisted",
                    prompt_cache_key="ecorex:persisted",
                    prompt_cache_retention="24h",
                ),
            )
            persisted_plan = bot.plan_responses_api_call(
                [{"role": "user", "content": "continue"}],
                stream=False,
                use_responses_api=True,
                session_id="session-planned",
                workspace=workspace,
            )
            fresh_plan = bot.plan_responses_api_call(
                [{"role": "user", "content": "fresh turn only"}],
                stream=False,
                use_responses_api=True,
                session_id="session-planned",
                workspace=workspace,
                responses_input_scope="fresh",
            )

        self.assertNotIn("previous_response_id", persisted_plan.create_payload)
        self.assertEqual(persisted_plan.create_payload["prompt_cache_key"], "ecorex:persisted")
        self.assertEqual(persisted_plan.create_payload["service_tier"], "flex")
        self.assertEqual(fresh_plan.create_payload["previous_response_id"], "resp_persisted")
        self.assertEqual(fresh_plan.create_payload["prompt_cache_key"], "ecorex:persisted")
        self.assertEqual(fresh_plan.create_payload["service_tier"], "flex")

    def test_session_service_clear_context_clears_responses_state(self):
        from agent.chat.session_service import SessionService
        from models.openai.responses_adapter import ResponsesState
        from models.openai.responses_state_store import (
            load_responses_state,
            save_responses_state,
        )

        class DummyStore:
            def clear_context(self, session_id):
                self.session_id = session_id
                return 42

            def clear_session(self, session_id):
                self.session_id = session_id

        class TestService(SessionService):
            def __init__(self, store):
                self.store = store

            def _get_store(self):
                return self.store

            def _remove_agent(self, session_id):
                self.removed_agent = session_id

        with tempfile.TemporaryDirectory() as workspace:
            from config import conf

            previous_workspace = conf().get("agent_workspace")
            conf()["agent_workspace"] = workspace
            try:
                session_id = "session_lifecycle"
                save_responses_state(
                    session_id=session_id,
                    provider="openai",
                    model="gpt-5.5",
                    workspace=workspace,
                    state=ResponsesState(previous_response_id="resp_lifecycle"),
                )
                service = TestService(DummyStore())
                self.assertEqual(service.clear_context("lifecycle"), 42)
                cleared = load_responses_state(
                    session_id=session_id,
                    provider="openai",
                    model="gpt-5.5",
                    workspace=workspace,
                )
            finally:
                _restore_conf_value("agent_workspace", previous_workspace)

        self.assertIsNone(cleared.previous_response_id)

    def test_web_clear_context_handler_clears_responses_state(self):
        import types
        from unittest.mock import patch

        if "web" not in sys.modules:
            sys.modules["web"] = types.SimpleNamespace(
                header=lambda *_args, **_kwargs: None,
                data=lambda: b"{}",
                input=lambda **kwargs: types.SimpleNamespace(**kwargs),
                cookies=lambda: {},
                HTTPError=Exception,
                notfound=lambda: Exception("not found"),
                badrequest=lambda: Exception("bad request"),
                ctx=types.SimpleNamespace(env={}, method="POST", status="200 OK"),
                httpserver=types.SimpleNamespace(
                    LogMiddleware=types.SimpleNamespace(log=lambda *_args, **_kwargs: None),
                    StaticMiddleware=lambda app: app,
                    WSGIServer=lambda *_args, **_kwargs: None,
                ),
                application=lambda *_args, **_kwargs: types.SimpleNamespace(wsgifunc=lambda: None),
            )
        from channel.web.web_channel import SessionClearContextHandler
        from models.openai.responses_adapter import ResponsesState
        from models.openai.responses_state_store import (
            load_responses_state,
            save_responses_state,
        )

        class DummyStore:
            def clear_context(self, session_id):
                self.session_id = session_id
                return 7

        class DummyAgentBridge:
            def __init__(self):
                self.agents = {"session_web_clear": object()}

        class DummyBridge:
            def __init__(self):
                self.agent_bridge = DummyAgentBridge()

            def get_agent_bridge(self):
                return self.agent_bridge

        with tempfile.TemporaryDirectory() as workspace:
            from config import conf

            previous_workspace = conf().get("agent_workspace")
            conf()["agent_workspace"] = workspace
            try:
                save_responses_state(
                    session_id="session_web_clear",
                    provider="openai",
                    model="gpt-5.5",
                    workspace=workspace,
                    state=ResponsesState(previous_response_id="resp_web_clear"),
                )
                with patch("channel.web.web_channel._require_auth", lambda: None), \
                        patch("channel.web.web_channel.web.header", lambda *_args, **_kwargs: None), \
                        patch("agent.memory.get_conversation_store", return_value=DummyStore()), \
                        patch("bridge.bridge.Bridge", return_value=DummyBridge()):
                    payload = SessionClearContextHandler().POST("session_web_clear")
                cleared = load_responses_state(
                    session_id="session_web_clear",
                    provider="openai",
                    model="gpt-5.5",
                    workspace=workspace,
                )
            finally:
                _restore_conf_value("agent_workspace", previous_workspace)

        self.assertEqual(json.loads(payload)["context_start_seq"], 7)
        self.assertIsNone(cleared.previous_response_id)

    def test_clear_history_clears_responses_state_when_persistence_disabled(self):
        from bridge.agent_bridge import AgentBridge
        from models.openai.responses_adapter import ResponsesState
        from models.openai.responses_state_store import (
            load_responses_state,
            save_responses_state,
        )

        with tempfile.TemporaryDirectory() as workspace:
            from config import conf

            previous_workspace = conf().get("agent_workspace")
            previous_persistence = conf().get("conversation_persistence", True)
            conf()["agent_workspace"] = workspace
            conf()["conversation_persistence"] = False
            try:
                save_responses_state(
                    session_id="session_clear_history",
                    provider="openai",
                    model="gpt-5.5",
                    workspace=workspace,
                    state=ResponsesState(previous_response_id="resp_clear_history"),
                )
                persisted = AgentBridge.__new__(AgentBridge)._pre_persist_user_message(
                    "session_clear_history",
                    "hello",
                    {},
                    clear_history=True,
                )
                cleared = load_responses_state(
                    session_id="session_clear_history",
                    provider="openai",
                    model="gpt-5.5",
                    workspace=workspace,
                )
            finally:
                _restore_conf_value("agent_workspace", previous_workspace)
                _restore_conf_value("conversation_persistence", previous_persistence)

        self.assertFalse(persisted)
        self.assertIsNone(cleared.previous_response_id)

    def test_non_stream_responses_runtime_normalizes_and_persists_next_state(self):
        from models.model_telemetry import get_recent_model_calls, reset_model_call_telemetry_for_tests
        from models.openai.responses_state_store import load_responses_state
        from models.openai_compatible_bot import OpenAICompatibleBot

        class CaptureClient:
            def __init__(self):
                self.calls = []

            def responses_create(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "id": "resp_runtime_next",
                    "model": "gpt-5.5",
                    "status": "completed",
                    "service_tier": "priority",
                    "output": [{
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Runtime OK"}],
                    }],
                    "usage": {
                        "input_tokens": 9,
                        "output_tokens": 4,
                        "total_tokens": 13,
                        "input_tokens_details": {"cached_tokens": 5},
                    },
                }

            def chat_completions(self, **_kwargs):
                raise AssertionError("Responses-enabled non-stream calls must not hit chat_completions")

        class RuntimeBot(OpenAICompatibleBot):
            def __init__(self, client):
                self.client = client

            def get_api_config(self):
                return {
                    "provider": "openai",
                    "api_key": "test-key",
                    "api_base": "https://api.openai.com/v1",
                    "model": "gpt-5.5",
                    "use_responses_api": True,
                    "responses_service_tier": "flex",
                    "responses_prompt_cache_retention": "24h",
                }

            def _get_http_client(self):
                return self.client

        reset_model_call_telemetry_for_tests()
        client = CaptureClient()
        bot = RuntimeBot(client)
        with tempfile.TemporaryDirectory() as workspace:
            from models.openai.responses_adapter import ResponsesState
            from models.openai.responses_state_store import save_responses_state

            save_responses_state(
                session_id="session-runtime",
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
                state=ResponsesState(previous_response_id="resp_should_not_auto_load"),
            )
            result = bot.call_with_tools(
                [{"role": "user", "content": "hello"}],
                stream=False,
                session_id="session-runtime",
                workspace=workspace,
                model="gpt-5.5",
            )
            state = load_responses_state(
                session_id="session-runtime",
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
            )

        self.assertEqual(result["choices"][0]["message"]["content"], "Runtime OK")
        self.assertEqual(client.calls[0]["api_key"], "test-key")
        self.assertEqual(client.calls[0]["api_base"], "https://api.openai.com/v1")
        self.assertEqual(client.calls[0]["model"], "gpt-5.5")
        self.assertEqual(client.calls[0]["service_tier"], "flex")
        self.assertEqual(client.calls[0]["prompt_cache_retention"], "24h")
        self.assertTrue(client.calls[0]["prompt_cache_key"].startswith("ecorex:"))
        self.assertNotIn("previous_response_id", client.calls[0])
        self.assertNotIn("messages", client.calls[0])
        self.assertEqual(state.previous_response_id, "resp_runtime_next")
        self.assertEqual(state.service_tier, "priority")
        self.assertEqual(state.prompt_cache_retention, "24h")
        self.assertTrue(state.prompt_cache_key.startswith("ecorex:"))
        events = get_recent_model_calls()
        self.assertEqual(events[0]["api_path"], "/responses")
        self.assertEqual(events[0]["status"], "completed")
        self.assertEqual(events[0]["cached_tokens"], 5)

    def test_responses_failed_status_does_not_persist_next_state(self):
        from models.model_telemetry import get_recent_model_calls, reset_model_call_telemetry_for_tests
        from models.openai.responses_state_store import load_responses_state
        from models.openai_compatible_bot import OpenAICompatibleBot

        class FailedResponsesClient:
            def responses_create(self, **_kwargs):
                return {
                    "id": "resp_failed",
                    "model": "gpt-5.5",
                    "status": "failed",
                    "error": {
                        "message": "tool limit exceeded",
                        "code": "tool_limit",
                        "type": "invalid_request_error",
                    },
                }

        class RuntimeBot(OpenAICompatibleBot):
            def get_api_config(self):
                return {
                    "provider": "openai",
                    "api_key": "test-key",
                    "api_base": "https://api.openai.com/v1",
                    "model": "gpt-5.5",
                    "use_responses_api": True,
                }

            def _get_http_client(self):
                return FailedResponsesClient()

        reset_model_call_telemetry_for_tests()
        with tempfile.TemporaryDirectory() as workspace:
            result = RuntimeBot().call_with_tools(
                [{"role": "user", "content": "hello"}],
                stream=False,
                session_id="session-failed-status",
                workspace=workspace,
                model="gpt-5.5",
                model_max_retries=0,
            )
            state = load_responses_state(
                session_id="session-failed-status",
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
            )

        self.assertTrue(result["error"])
        self.assertEqual(result["error"]["code"], "tool_limit")
        self.assertIsNone(state.previous_response_id)
        events = get_recent_model_calls()
        self.assertEqual(events[0]["api_path"], "/responses")
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[0]["error_code"], "tool_limit")

    def test_non_stream_responses_incomplete_is_non_retryable(self):
        from models.model_telemetry import get_recent_model_calls, reset_model_call_telemetry_for_tests
        from models.openai.responses_state_store import load_responses_state
        from models.openai_compatible_bot import OpenAICompatibleBot

        class IncompleteResponsesClient:
            def __init__(self):
                self.calls = []

            def responses_create(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "id": "resp_incomplete_sync",
                    "model": "gpt-5.5",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_tokens"},
                }

        class RuntimeBot(OpenAICompatibleBot):
            def __init__(self, client):
                self.client = client

            def get_api_config(self):
                return {
                    "provider": "openai",
                    "api_key": "test-key",
                    "api_base": "https://api.openai.com/v1",
                    "model": "gpt-5.5",
                    "use_responses_api": True,
                }

            def _get_http_client(self):
                return self.client

        reset_model_call_telemetry_for_tests()
        client = IncompleteResponsesClient()
        with tempfile.TemporaryDirectory() as workspace:
            result = RuntimeBot(client).call_with_tools(
                [{"role": "user", "content": "hello"}],
                stream=False,
                session_id="session-incomplete-sync",
                workspace=workspace,
                model="gpt-5.5",
                model_max_retries=1,
            )
            state = load_responses_state(
                session_id="session-incomplete-sync",
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["status_code"], 400)
        self.assertFalse(result["retryable"])
        self.assertEqual(result["error"]["code"], "max_tokens")
        self.assertIsNone(state.previous_response_id)
        events = get_recent_model_calls()
        self.assertEqual(events[0]["api_path"], "/responses")
        self.assertEqual(events[0]["error_taxonomy"], "client_error")

    def test_responses_stream_request_uses_responses_api_and_persists_state(self):
        from models.model_telemetry import get_recent_model_calls, reset_model_call_telemetry_for_tests
        from models.openai.responses_state_store import load_responses_state
        from models.openai_compatible_bot import OpenAICompatibleBot

        class ResponsesStreamClient:
            def __init__(self):
                self.chat_calls = []
                self.responses_calls = []

            def responses_create(self, **kwargs):
                self.responses_calls.append(kwargs)
                return iter([
                    {
                        "type": "response.created",
                        "response": {
                            "id": "resp_stream_runtime",
                            "model": "gpt-5.5",
                        },
                    },
                    {"type": "response.output_text.delta", "delta": "stream"},
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_stream_next",
                            "model": "gpt-5.5",
                            "status": "completed",
                            "service_tier": "priority",
                            "usage": {
                                "input_tokens": 11,
                                "output_tokens": 5,
                                "total_tokens": 16,
                                "input_tokens_details": {"cached_tokens": 4},
                            },
                        },
                    },
                ])

            def chat_completions(self, **kwargs):
                self.chat_calls.append(kwargs)
                raise AssertionError("Responses-enabled stream calls must not hit chat_completions")

        class RuntimeBot(OpenAICompatibleBot):
            def __init__(self, client):
                self.client = client

            def get_api_config(self):
                return {
                    "provider": "openai",
                    "api_key": "test-key",
                    "api_base": "https://api.openai.com/v1",
                    "model": "gpt-5.5",
                    "use_responses_api": True,
                }

            def _get_http_client(self):
                return self.client

        reset_model_call_telemetry_for_tests()
        client = ResponsesStreamClient()
        with tempfile.TemporaryDirectory() as workspace:
            chunks = list(RuntimeBot(client).call_with_tools(
                [{"role": "user", "content": "hello"}],
                stream=True,
                session_id="session-stream-runtime",
                workspace=workspace,
                model="gpt-5.5",
            ))
            state = load_responses_state(
                session_id="session-stream-runtime",
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
            )

        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "stream")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(len(client.responses_calls), 1)
        self.assertEqual(len(client.chat_calls), 0)
        self.assertTrue(client.responses_calls[0]["stream"])
        self.assertEqual(client.responses_calls[0]["model"], "gpt-5.5")
        self.assertNotIn("messages", client.responses_calls[0])
        self.assertEqual(state.previous_response_id, "resp_stream_next")
        self.assertEqual(state.service_tier, "priority")
        events = get_recent_model_calls()
        self.assertEqual(events[0]["api_path"], "/responses")
        self.assertTrue(events[0]["stream"])
        self.assertEqual(events[0]["status"], "completed")
        self.assertEqual(events[0]["total_tokens"], 16)
        self.assertEqual(events[0]["cached_tokens"], 4)

    def test_responses_stream_retries_before_first_output(self):
        from models.model_telemetry import get_recent_model_calls, reset_model_call_telemetry_for_tests
        from models.openai_compatible_bot import OpenAICompatibleBot

        class RetryResponsesClient:
            def __init__(self):
                self.responses_calls = []

            def responses_create(self, **kwargs):
                self.responses_calls.append(kwargs)
                if len(self.responses_calls) == 1:
                    return iter([{
                        "type": "error",
                        "message": "server unavailable",
                        "status_code": 503,
                        "retry_after": "0.25",
                    }])
                return iter([
                    {"type": "response.output_text.delta", "delta": "retry-ok"},
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_retry_next",
                            "model": "gpt-5.5",
                            "status": "completed",
                            "usage": {
                                "input_tokens": 2,
                                "output_tokens": 1,
                                "total_tokens": 3,
                            },
                        },
                    },
                ])

        class RuntimeBot(OpenAICompatibleBot):
            def __init__(self, client):
                self.client = client

            def get_api_config(self):
                return {
                    "provider": "openai",
                    "api_key": "test-key",
                    "api_base": "https://api.openai.com/v1",
                    "model": "gpt-5.5",
                    "use_responses_api": True,
                }

            def _get_http_client(self):
                return self.client

        reset_model_call_telemetry_for_tests()
        sleeps = []
        client = RetryResponsesClient()
        chunks = list(RuntimeBot(client).call_with_tools(
            [{"role": "user", "content": "hello"}],
            stream=True,
            model="gpt-5.5",
            model_max_retries=1,
            model_retry_sleep=sleeps.append,
        ))

        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "retry-ok")
        self.assertEqual(len(client.responses_calls), 2)
        self.assertEqual(sleeps, [0.25])
        events = get_recent_model_calls()
        self.assertEqual([event["api_path"] for event in events], ["/responses", "/responses"])
        self.assertEqual([event["status"] for event in events], ["failed", "completed"])
        self.assertEqual(events[0]["error_taxonomy"], "server_error")

    def test_responses_stream_error_after_output_suppresses_retry_and_state(self):
        from models.model_telemetry import get_recent_model_calls, reset_model_call_telemetry_for_tests
        from models.openai.responses_state_store import load_responses_state
        from models.openai_compatible_bot import OpenAICompatibleBot

        class ErrorAfterOutputClient:
            def __init__(self):
                self.responses_calls = []

            def responses_create(self, **kwargs):
                self.responses_calls.append(kwargs)
                return iter([
                    {"type": "response.output_text.delta", "delta": "partial"},
                    {
                        "type": "error",
                        "message": "server unavailable",
                        "status_code": 503,
                        "retry_after": "0.25",
                    },
                ])

        class RuntimeBot(OpenAICompatibleBot):
            def __init__(self, client):
                self.client = client

            def get_api_config(self):
                return {
                    "provider": "openai",
                    "api_key": "test-key",
                    "api_base": "https://api.openai.com/v1",
                    "model": "gpt-5.5",
                    "use_responses_api": True,
                }

            def _get_http_client(self):
                return self.client

        reset_model_call_telemetry_for_tests()
        client = ErrorAfterOutputClient()
        with tempfile.TemporaryDirectory() as workspace:
            chunks = list(RuntimeBot(client).call_with_tools(
                [{"role": "user", "content": "hello"}],
                stream=True,
                session_id="session-stream-error-after-output",
                workspace=workspace,
                model="gpt-5.5",
                model_max_retries=1,
                model_retry_sleep=lambda _delay: None,
            ))
            state = load_responses_state(
                session_id="session-stream-error-after-output",
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
            )

        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "partial")
        self.assertEqual(chunks[1]["status_code"], 503)
        self.assertTrue(chunks[1]["retry_suppressed"])
        self.assertEqual(chunks[1]["retry_suppressed_reason"], "stream_output_started")
        self.assertEqual(len(client.responses_calls), 1)
        self.assertIsNone(state.previous_response_id)
        events = get_recent_model_calls()
        self.assertEqual(events[0]["api_path"], "/responses")
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[0]["error_taxonomy"], "server_error")

    def test_responses_stream_incomplete_is_non_retryable_and_does_not_persist_state(self):
        from models.model_telemetry import get_recent_model_calls, reset_model_call_telemetry_for_tests
        from models.openai.responses_state_store import load_responses_state
        from models.openai_compatible_bot import OpenAICompatibleBot

        class IncompleteResponsesClient:
            def __init__(self):
                self.responses_calls = []

            def responses_create(self, **kwargs):
                self.responses_calls.append(kwargs)
                return iter([{
                    "type": "response.incomplete",
                    "response": {
                        "id": "resp_incomplete",
                        "model": "gpt-5.5",
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_tokens"},
                    },
                }])

        class RuntimeBot(OpenAICompatibleBot):
            def __init__(self, client):
                self.client = client

            def get_api_config(self):
                return {
                    "provider": "openai",
                    "api_key": "test-key",
                    "api_base": "https://api.openai.com/v1",
                    "model": "gpt-5.5",
                    "use_responses_api": True,
                }

            def _get_http_client(self):
                return self.client

        reset_model_call_telemetry_for_tests()
        client = IncompleteResponsesClient()
        with tempfile.TemporaryDirectory() as workspace:
            chunks = list(RuntimeBot(client).call_with_tools(
                [{"role": "user", "content": "hello"}],
                stream=True,
                session_id="session-stream-incomplete",
                workspace=workspace,
                model="gpt-5.5",
                model_max_retries=1,
                model_retry_sleep=lambda _delay: None,
            ))
            state = load_responses_state(
                session_id="session-stream-incomplete",
                provider="openai",
                model="gpt-5.5",
                workspace=workspace,
            )

        self.assertEqual(len(client.responses_calls), 1)
        self.assertEqual(chunks[0]["status_code"], 400)
        self.assertFalse(chunks[0]["retryable"])
        self.assertFalse(chunks[0]["retry_exhausted"])
        self.assertEqual(chunks[0]["error"]["code"], "max_tokens")
        self.assertIsNone(state.previous_response_id)
        events = get_recent_model_calls()
        self.assertEqual(events[0]["api_path"], "/responses")
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[0]["error_taxonomy"], "client_error")

    def test_extract_state_and_normalize_response_output(self):
        from models.openai.responses_adapter import (
            ResponsesState,
            extract_responses_state,
            normalize_responses_output_to_chat,
        )

        previous = ResponsesState(prompt_cache_key="tenant:stable-prefix", service_tier="flex")
        response = {
            "id": "resp_new",
            "model": "gpt-5.5",
            "status": "completed",
            "service_tier": "priority",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done"}],
            }],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 3,
                "total_tokens": 13,
                "input_tokens_details": {"cached_tokens": 4},
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        }

        state = extract_responses_state(response, previous)
        self.assertEqual(state.previous_response_id, "resp_new")
        self.assertEqual(state.prompt_cache_key, "tenant:stable-prefix")
        self.assertEqual(state.service_tier, "priority")

        normalized = normalize_responses_output_to_chat(response)
        self.assertEqual(normalized["choices"][0]["message"]["content"], "Done")
        self.assertEqual(normalized["usage"]["prompt_tokens_details"]["cached_tokens"], 4)
        self.assertEqual(normalized["usage"]["completion_tokens_details"]["reasoning_tokens"], 2)

        refusal = normalize_responses_output_to_chat({
            "id": "resp_refusal",
            "model": "gpt-5.5",
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "refusal", "refusal": "I can't help."}],
            }],
        })
        self.assertEqual(refusal["choices"][0]["message"]["content"], "I can't help.")

    def test_compaction_state_uses_output_without_previous_response_id(self):
        from models.openai.responses_adapter import (
            ResponsesState,
            build_responses_plan,
            extract_responses_state,
        )

        compacted_output = [
            {
                "id": "msg_000",
                "type": "message",
                "status": "completed",
                "role": "user",
                "content": [{"type": "input_text", "text": "older context"}],
            },
            {"id": "cmp_001", "type": "compaction", "encrypted_content": "opaque"},
        ]
        previous = ResponsesState(
            previous_response_id="resp_prior",
            prompt_cache_key="tenant:stable-prefix",
            service_tier="flex",
        )
        state = extract_responses_state({
            "id": "resp_compaction",
            "object": "response.compaction",
            "output": compacted_output,
        }, previous)

        self.assertIsNone(state.previous_response_id)
        self.assertEqual(state.compacted_input, compacted_output)
        self.assertEqual(state.prompt_cache_key, "tenant:stable-prefix")
        self.assertEqual(state.service_tier, "flex")

        plan = build_responses_plan(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "fresh turn"}],
            state=state,
        )
        fresh_input = {
            "role": "user",
            "content": [{"type": "input_text", "text": "fresh turn"}],
        }
        self.assertEqual(plan.create_payload["input"], compacted_output + [fresh_input])
        self.assertEqual(plan.compact_payload["input"], compacted_output + [fresh_input])
        self.assertEqual(plan.input_tokens_payload["input"], compacted_output + [fresh_input])
        self.assertNotIn("previous_response_id", plan.create_payload)


class TestOpenAIHTTPClientResponsesMethods(unittest.TestCase):
    def test_responses_methods_use_expected_paths(self):
        from models.openai.openai_http_client import OpenAIHTTPClient

        class CaptureClient(OpenAIHTTPClient):
            def __init__(self):
                super().__init__(api_key="test-key", api_base="https://api.openai.com/v1")
                self.calls = []

            def _request(self, **kwargs):
                self.calls.append(kwargs)
                return {"ok": True, "path": kwargs["path"], "stream": kwargs["stream"]}

        client = CaptureClient()
        create = client.responses_create(model="gpt-5.5", input="hi", stream=True)
        compact = client.responses_compact(model="gpt-5.5", input=[])
        tokens = client.responses_input_tokens(model="gpt-5.5", input="hi")

        self.assertEqual(create, {"ok": True, "path": "/responses", "stream": True})
        self.assertEqual(compact, {"ok": True, "path": "/responses/compact", "stream": False})
        self.assertEqual(tokens, {"ok": True, "path": "/responses/input_tokens", "stream": False})
        self.assertTrue(client.calls[0]["payload"]["stream"])
        self.assertNotIn("stream", client.calls[1]["payload"])
        self.assertNotIn("stream", client.calls[2]["payload"])


if __name__ == "__main__":
    unittest.main()
