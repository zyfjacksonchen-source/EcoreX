# encoding:utf-8
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestOpenAIResponsesAdapter(unittest.TestCase):
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

    def test_planning_hook_does_not_switch_unless_enabled(self):
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
