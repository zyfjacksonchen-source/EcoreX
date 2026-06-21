# encoding:utf-8
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestModelCapabilities(unittest.TestCase):
    def test_infer_provider_from_model_prefixes(self):
        from common import const
        from models.model_capabilities import infer_provider_id

        self.assertEqual(infer_provider_id("deepseek-v4-flash"), const.DEEPSEEK)
        self.assertEqual(infer_provider_id("qwen3.7-max"), const.QWEN_DASHSCOPE)
        self.assertEqual(infer_provider_id("Qwen/Qwen3-235B-A22B"), const.MODELSCOPE)
        self.assertEqual(infer_provider_id("meituan-longcat/LongCat-Flash-Lite"), const.MODELSCOPE)
        self.assertEqual(infer_provider_id("kimi-k2.6"), const.MOONSHOT)
        self.assertEqual(infer_provider_id("linkai-gpt-4o-mini"), const.LINKAI)
        self.assertEqual(infer_provider_id("gpt-5.5"), const.OPENAI)

    def test_linkai_config_overrides_model_inference_when_key_exists(self):
        from common import const
        from models.model_capabilities import infer_provider_id

        self.assertEqual(
            infer_provider_id("deepseek-v4-flash", use_linkai=True, has_linkai_key=True),
            const.LINKAI,
        )

    def test_openai_fixed_sampling_models_strip_unsupported_params(self):
        from models.model_capabilities import (
            get_model_capabilities,
            normalize_reasoning_effort,
            sanitize_chat_payload,
        )

        payload = {
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 4096,
            "temperature": 0.7,
            "top_p": 0.9,
            "frequency_penalty": 0.2,
            "presence_penalty": 0.1,
            "reasoning_effort": "high",
            "verbosity": "low",
            "stream": True,
        }
        capabilities = get_model_capabilities("gpt-5.5", "openai")
        clean, removed = sanitize_chat_payload(payload, capabilities)

        self.assertTrue(capabilities.supports_reasoning_effort)
        self.assertTrue(capabilities.supports_verbosity)
        self.assertEqual(capabilities.max_tokens_param, "max_completion_tokens")
        self.assertNotIn("temperature", clean)
        self.assertNotIn("top_p", clean)
        self.assertNotIn("frequency_penalty", clean)
        self.assertNotIn("presence_penalty", clean)
        self.assertNotIn("max_tokens", clean)
        self.assertEqual(clean["max_completion_tokens"], 4096)
        self.assertEqual(clean["reasoning_effort"], "high")
        self.assertEqual(clean["verbosity"], "low")
        self.assertEqual(normalize_reasoning_effort("max", capabilities), "high")
        self.assertEqual(
            set(removed),
            {"temperature", "top_p", "frequency_penalty", "presence_penalty", "max_tokens"},
        )
        self.assertEqual(clean["stream_options"], {"include_usage": True})

    def test_custom_openai_compatible_provider_does_not_get_openai_stream_usage(self):
        from models.model_capabilities import get_model_capabilities, sanitize_chat_payload

        clean, removed = sanitize_chat_payload(
            {"model": "gpt-5.5", "stream": True, "temperature": 0.7},
            get_model_capabilities("gpt-5.5", "custom"),
        )

        self.assertEqual(clean["temperature"], 0.7)
        self.assertNotIn("stream_options", clean)
        self.assertEqual(removed, ())

    def test_capabilities_for_config_downgrades_non_official_openai_base(self):
        from common import const
        from models.model_capabilities import capabilities_for_config

        capabilities = capabilities_for_config({
            "model": "gpt-5.5",
            "bot_type": const.CHATGPT,
            "open_ai_api_base": "https://coding-plan.test/v1",
            "use_linkai": False,
        })

        self.assertEqual(capabilities.provider, "openai_compatible")
        self.assertTrue(capabilities.supports_temperature)
        self.assertFalse(capabilities.supports_stream_usage)
        self.assertFalse(capabilities.supports_reasoning_effort)
        self.assertFalse(capabilities.supports_verbosity)
        self.assertEqual(capabilities.max_tokens_param, "max_tokens")

    def test_reasoning_effort_normalization_prefers_conservative_supported_value(self):
        from common import const
        from models.model_capabilities import get_model_capabilities, normalize_reasoning_effort

        capabilities = get_model_capabilities("deepseek-v4-flash", const.DEEPSEEK)

        self.assertEqual(normalize_reasoning_effort("max", capabilities), "max")
        self.assertEqual(normalize_reasoning_effort("medium", capabilities), "high")
        self.assertEqual(normalize_reasoning_effort("minimal", capabilities), "high")

    def test_openai_compatible_bot_uses_sanitized_payload(self):
        from models.openai_compatible_bot import OpenAICompatibleBot

        class CaptureBot(OpenAICompatibleBot):
            def get_api_config(self):
                return {
                    "provider": "openai",
                    "api_key": "test-key",
                    "api_base": "https://api.openai.com/v1",
                    "model": "gpt-5.5",
                    "default_temperature": 0.7,
                    "default_top_p": 0.9,
                    "default_frequency_penalty": 0.2,
                    "default_presence_penalty": 0.1,
                }

            def _handle_stream_response(self, request_params, api_key, api_base):
                return request_params

        payload = CaptureBot().call_with_tools(
            [{"role": "user", "content": "hello"}],
            stream=True,
            max_tokens=4096,
            reasoning_effort="max",
            verbosity="high",
        )

        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertNotIn("temperature", payload)
        self.assertNotIn("top_p", payload)
        self.assertNotIn("frequency_penalty", payload)
        self.assertNotIn("presence_penalty", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(payload["max_completion_tokens"], 4096)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["verbosity"], "high")
        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_openai_compatible_bot_infers_official_openai_when_provider_is_omitted(self):
        from models.openai_compatible_bot import OpenAICompatibleBot

        class CaptureBot(OpenAICompatibleBot):
            def get_api_config(self):
                return {
                    "api_key": "test-key",
                    "api_base": "https://api.openai.com/v1",
                    "model": "gpt-5.5",
                    "default_temperature": 0.7,
                    "default_top_p": 0.9,
                    "default_frequency_penalty": 0.2,
                    "default_presence_penalty": 0.1,
                }

            def _handle_stream_response(self, request_params, api_key, api_base):
                return request_params

        payload = CaptureBot().call_with_tools(
            [{"role": "user", "content": "hello"}],
            stream=True,
            max_tokens=4096,
        )

        self.assertNotIn("temperature", payload)
        self.assertNotIn("top_p", payload)
        self.assertNotIn("frequency_penalty", payload)
        self.assertNotIn("presence_penalty", payload)
        self.assertEqual(payload["max_completion_tokens"], 4096)
        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_openai_compatible_bot_treats_custom_openai_base_as_non_official(self):
        from models.openai_compatible_bot import OpenAICompatibleBot

        class CaptureBot(OpenAICompatibleBot):
            def get_api_config(self):
                return {
                    "provider": "openai",
                    "api_key": "test-key",
                    "api_base": "https://coding-plan.test/v1",
                    "model": "gpt-5.5",
                    "default_temperature": 0.7,
                    "default_top_p": 0.9,
                    "default_frequency_penalty": 0.2,
                    "default_presence_penalty": 0.1,
                }

            def _handle_stream_response(self, request_params, api_key, api_base):
                return request_params

        payload = CaptureBot().call_with_tools(
            [{"role": "user", "content": "hello"}],
            stream=True,
            max_tokens=4096,
            reasoning_effort="max",
            verbosity="high",
        )

        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(payload["top_p"], 0.9)
        self.assertEqual(payload["frequency_penalty"], 0.2)
        self.assertEqual(payload["presence_penalty"], 0.1)
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertNotIn("max_completion_tokens", payload)
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("verbosity", payload)
        self.assertNotIn("stream_options", payload)

    def test_openai_compatible_bot_coerces_system_messages_for_o1_models(self):
        from models.openai_compatible_bot import OpenAICompatibleBot

        class CaptureBot(OpenAICompatibleBot):
            def get_api_config(self):
                return {
                    "provider": "openai",
                    "api_key": "test-key",
                    "api_base": "https://api.openai.com/v1",
                    "model": "o1-mini",
                }

            def _sync_response_with_retry(self, request_params, *args, **kwargs):
                return request_params

        payload = CaptureBot().call_with_tools(
            [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "hello"},
            ],
            stream=False,
        )

        self.assertEqual([message["role"] for message in payload["messages"]], ["user", "user"])
        self.assertEqual(payload["messages"][0]["content"], "rules")

    def test_responses_adapter_receives_reasoning_verbosity_and_token_limit(self):
        from models.openai_compatible_bot import OpenAICompatibleBot

        class CaptureBot(OpenAICompatibleBot):
            def get_api_config(self):
                return {
                    "provider": "openai",
                    "api_key": "test-key",
                    "api_base": "https://api.openai.com/v1",
                    "model": "gpt-5.5",
                }

            def _responses_sync_response_with_retry(self, plan, *args, **kwargs):
                return plan.create_payload

        payload = CaptureBot().call_with_tools(
            [{"role": "user", "content": "hello"}],
            stream=False,
            max_tokens=1234,
            reasoning_effort="max",
            verbosity="high",
            use_responses_api=True,
        )

        self.assertEqual(payload["max_output_tokens"], 1234)
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["text"], {"verbosity": "high"})
        self.assertNotIn("temperature", payload)

    def test_agent_bridge_only_emits_supported_model_controls(self):
        from agent.protocol import LLMRequest
        from bridge.agent_bridge import AgentLLMModel
        from common import const

        local_config = {
            "model": "gpt-5.5",
            "bot_type": const.OPENAI,
            "use_linkai": False,
            "enable_thinking": True,
            "reasoning_effort": "max",
            "verbosity": "high",
        }
        request = LLMRequest(
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=100,
        )

        with patch("bridge.agent_bridge.conf", return_value=local_config):
            bridge_model = AgentLLMModel(None)
            gemini_kwargs = bridge_model._build_call_kwargs(
                request,
                stream=False,
                model_name="gemini-3.5-flash",
                provider=const.GEMINI,
            )
            deepseek_kwargs = bridge_model._build_call_kwargs(
                request,
                stream=False,
                model_name="deepseek-v4-flash",
                provider=const.DEEPSEEK,
            )
            openai_kwargs = bridge_model._build_call_kwargs(
                request,
                stream=False,
                model_name="gpt-5.5",
                provider=const.OPENAI,
            )

        self.assertNotIn("thinking", gemini_kwargs)
        self.assertNotIn("reasoning_effort", gemini_kwargs)
        self.assertNotIn("verbosity", gemini_kwargs)

        self.assertEqual(deepseek_kwargs["thinking"], {"type": "enabled"})
        self.assertEqual(deepseek_kwargs["reasoning_effort"], "max")
        self.assertNotIn("verbosity", deepseek_kwargs)

        self.assertNotIn("thinking", openai_kwargs)
        self.assertEqual(openai_kwargs["reasoning_effort"], "high")
        self.assertEqual(openai_kwargs["verbosity"], "high")

    def test_chatgpt_bot_initial_args_use_capability_catalog(self):
        from common import const
        from models.chatgpt.chat_gpt_bot import ChatGPTBot

        local_config = {
            "bot_type": const.OPENAI,
            "model": const.GPT_54_MINI,
            "temperature": 0.7,
            "top_p": 0.9,
            "frequency_penalty": 0.2,
            "presence_penalty": 0.1,
            "request_timeout": 30,
            "open_ai_api_key": "test-key",
            "open_ai_api_base": "https://api.openai.com/v1",
        }

        with patch("models.chatgpt.chat_gpt_bot.conf", return_value=local_config):
            bot = ChatGPTBot()

        self.assertEqual(bot.args["model"], const.GPT_54_MINI)
        self.assertNotIn("temperature", bot.args)
        self.assertNotIn("top_p", bot.args)
        self.assertNotIn("frequency_penalty", bot.args)
        self.assertNotIn("presence_penalty", bot.args)

    def test_chatgpt_bot_initial_args_keep_sampling_for_custom_openai_compatible_route(self):
        from common import const
        from models.chatgpt.chat_gpt_bot import ChatGPTBot

        local_config = {
            "bot_type": const.CUSTOM,
            "model": const.GPT_54_MINI,
            "temperature": 0.7,
            "top_p": 0.9,
            "frequency_penalty": 0.2,
            "presence_penalty": 0.1,
            "request_timeout": 30,
            "custom_api_key": "test-key",
            "custom_api_base": "https://coding-plan.test/v1",
        }

        with patch("models.chatgpt.chat_gpt_bot.conf", return_value=local_config):
            bot = ChatGPTBot()

        self.assertEqual(bot.args["model"], const.GPT_54_MINI)
        self.assertEqual(bot.args["temperature"], 0.7)
        self.assertEqual(bot.args["top_p"], 0.9)
        self.assertEqual(bot.args["frequency_penalty"], 0.2)
        self.assertEqual(bot.args["presence_penalty"], 0.1)

    def test_model_fallback_config_parses_explicit_routes(self):
        from models.model_fallback import configured_model_fallback_routes

        routes = configured_model_fallback_routes(
            {
                "model_fallbacks": [
                    "gpt-5.4-mini",
                    {"model": "deepseek-v4-flash", "bot_type": "deepseek"},
                    {"model": "ignored", "enabled": False},
                    "primary-model",
                ],
            },
            primary_model="primary-model",
            primary_bot_type="openai",
        )

        self.assertEqual([route.model for route in routes], ["gpt-5.4-mini", "deepseek-v4-flash"])
        self.assertEqual(routes[0].provider, "openai")
        self.assertEqual(routes[1].provider, "deepseek")


if __name__ == "__main__":
    unittest.main()
