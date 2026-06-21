# encoding:utf-8
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestModelCapabilities(unittest.TestCase):
    def test_infer_provider_from_model_prefixes(self):
        from common import const
        from models.model_capabilities import infer_provider_id

        self.assertEqual(infer_provider_id("deepseek-v4-flash"), const.DEEPSEEK)
        self.assertEqual(infer_provider_id("qwen3.7-max"), const.QWEN_DASHSCOPE)
        self.assertEqual(infer_provider_id("kimi-k2.6"), const.MOONSHOT)
        self.assertEqual(infer_provider_id("gpt-5.5"), const.OPENAI)

    def test_linkai_config_overrides_model_inference_when_key_exists(self):
        from common import const
        from models.model_capabilities import infer_provider_id

        self.assertEqual(
            infer_provider_id("deepseek-v4-flash", use_linkai=True, has_linkai_key=True),
            const.LINKAI,
        )

    def test_openai_fixed_sampling_models_strip_unsupported_params(self):
        from models.model_capabilities import get_model_capabilities, sanitize_chat_payload

        payload = {
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0.7,
            "top_p": 0.9,
            "frequency_penalty": 0.2,
            "presence_penalty": 0.1,
            "stream": True,
        }
        clean, removed = sanitize_chat_payload(payload, get_model_capabilities("gpt-5.5", "openai"))

        self.assertNotIn("temperature", clean)
        self.assertNotIn("top_p", clean)
        self.assertNotIn("frequency_penalty", clean)
        self.assertNotIn("presence_penalty", clean)
        self.assertEqual(set(removed), {"temperature", "top_p", "frequency_penalty", "presence_penalty"})
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
        )

        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertNotIn("temperature", payload)
        self.assertNotIn("top_p", payload)
        self.assertNotIn("frequency_penalty", payload)
        self.assertNotIn("presence_penalty", payload)
        self.assertEqual(payload["stream_options"], {"include_usage": True})

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
