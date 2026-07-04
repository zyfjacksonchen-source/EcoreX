# encoding:utf-8
import json
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
        self.assertEqual(infer_provider_id("doubao-seed-2.1-pro"), const.DOUBAO)

    def test_model_context_policy_tracks_top_tier_windows(self):
        from common import const
        from models.model_capabilities import context_policy_for_model, get_model_capabilities

        openai_policy = context_policy_for_model(const.GPT_55, const.OPENAI)
        self.assertEqual(openai_policy.context_window_tokens, 1000000)
        self.assertEqual(openai_policy.max_output_tokens, 128000)
        self.assertEqual(openai_policy.auto_compact_token_limit, 800000)
        self.assertEqual(openai_policy.tokenizer_status, "local_tokenizer")

        gemini_policy = context_policy_for_model(const.GEMINI_31_PRO_PRE, const.GEMINI)
        self.assertEqual(gemini_policy.context_window_tokens, 1048576)
        self.assertEqual(gemini_policy.max_output_tokens, 65536)
        self.assertEqual(gemini_policy.tokenizer_status, "estimated")

        deepseek_capabilities = get_model_capabilities(const.DEEPSEEK_V4_PRO, const.DEEPSEEK)
        self.assertEqual(deepseek_capabilities.context_window_tokens, 1000000)
        self.assertEqual(deepseek_capabilities.auto_compact_token_limit, 800000)

        doubao_policy = context_policy_for_model(const.DOUBAO_SEED_21_PRO, const.DOUBAO)
        self.assertEqual(doubao_policy.context_window_tokens, 256000)
        self.assertEqual(doubao_policy.auto_compact_token_limit, 204800)

    def test_linkai_config_overrides_model_inference_when_key_exists(self):
        from common import const
        from models.model_capabilities import infer_provider_id

        self.assertEqual(
            infer_provider_id("deepseek-v4-flash", use_linkai=True, has_linkai_key=True),
            const.LINKAI,
        )

    def test_custom_bot_type_overrides_gemini_prefix_for_openai_compatible_route(self):
        from common import const
        from models.model_capabilities import capabilities_for_config, infer_provider_id

        self.assertEqual(infer_provider_id("gemini-custom-proxy"), const.GEMINI)
        self.assertEqual(
            infer_provider_id("gemini-custom-proxy", configured_bot_type=const.CUSTOM),
            const.CUSTOM,
        )

        capabilities = capabilities_for_config({
            "model": "gemini-custom-proxy",
            "bot_type": const.CUSTOM,
            "custom_api_key": "test-custom",
            "custom_api_base": "https://custom-gemini.test/v1",
            "use_linkai": False,
        })

        self.assertEqual(capabilities.provider, const.CUSTOM)
        self.assertTrue(capabilities.supports_temperature)
        self.assertFalse(capabilities.supports_stream_usage)
        self.assertEqual(capabilities.max_tokens_param, "max_tokens")

    def test_nonofficial_gemini_base_routes_as_gemini_rest_transport(self):
        from common import const
        from models.model_capabilities import (
            capabilities_for_config,
            infer_provider_id,
            normalize_openai_compatible_api_base,
        )

        self.assertEqual(
            infer_provider_id(
                "gemini-3.1-pro-preview",
                configured_bot_type=const.GEMINI,
                gemini_api_base="https://custom-gemini.test/v1",
                has_gemini_key=True,
            ),
            const.GEMINI,
        )
        self.assertEqual(
            infer_provider_id(
                "gemini-3.1-pro-preview",
                configured_bot_type=const.GEMINI,
                gemini_api_base="https://generativelanguage.googleapis.com",
                has_gemini_key=True,
            ),
            const.GEMINI,
        )

        capabilities = capabilities_for_config({
            "model": "gemini-3.1-pro-preview",
            "bot_type": const.GEMINI,
            "gemini_api_key": "legacy-custom-gemini-key",
            "gemini_api_base": "https://custom-gemini.test/v1",
        })

        self.assertEqual(capabilities.provider, const.GEMINI)
        self.assertEqual(capabilities.max_tokens_param, "max_tokens")
        self.assertEqual(
            infer_provider_id(
                "gemini-3.1-pro-preview",
                configured_bot_type=const.CUSTOM,
                gemini_api_base="https://custom-gemini.test/v1",
                has_gemini_key=True,
                gemini_api_key="legacy-custom-gemini-key",
                custom_api_base="https://custom-gemini.test/v1",
                custom_api_key="legacy-custom-gemini-key",
            ),
            const.GEMINI,
        )
        self.assertEqual(
            normalize_openai_compatible_api_base("http://custom-gemini.test:8080"),
            "http://custom-gemini.test:8080/v1",
        )
        self.assertEqual(
            normalize_openai_compatible_api_base("https://custom-gemini.test/v1/chat/completions"),
            "https://custom-gemini.test/v1",
        )

    def test_gemini_rest_tool_schema_sanitizer_adds_missing_array_items(self):
        from models.gemini.google_gemini_bot import GoogleGeminiBot

        schema = {
            "type": "object",
            "title": "private tool schema",
            "$defs": {"secret": {"type": "string"}},
            "additionalProperties": False,
            "properties": {
                "files": {"type": "array", "description": "local files"},
                "reviews": {"type": ["array", "null"]},
                "sources": {"type": "array", "items": {"anyOf": [{"type": "null"}, {"type": "string"}]}},
                "mode": {"enum": ["fast", "safe"], "default": "safe"},
            },
            "required": ["files"],
        }

        sanitized = GoogleGeminiBot._sanitize_gemini_schema(schema)

        self.assertNotIn("$defs", sanitized)
        self.assertNotIn("additionalProperties", sanitized)
        self.assertNotIn("title", sanitized)
        self.assertEqual(sanitized["type"], "object")
        self.assertEqual(sanitized["properties"]["files"]["items"], {"type": "object"})
        self.assertTrue(sanitized["properties"]["reviews"]["nullable"])
        self.assertEqual(sanitized["properties"]["reviews"]["items"], {"type": "object"})
        self.assertEqual(sanitized["properties"]["sources"]["items"]["type"], "string")
        self.assertNotIn("default", sanitized["properties"]["mode"])

    def test_gemini_rest_tool_conversion_uses_sanitized_schema(self):
        from models.gemini.google_gemini_bot import GoogleGeminiBot

        bot = GoogleGeminiBot.__new__(GoogleGeminiBot)
        converted = bot._convert_tools_to_gemini_rest_format([{
            "type": "function",
            "function": {
                "name": "emit_artifacts",
                "description": "return artifacts",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "files": {"type": "array"},
                        "metadata": {"type": "object", "additionalProperties": True},
                    },
                },
            },
        }])

        parameters = converted[0]["functionDeclarations"][0]["parameters"]
        self.assertEqual(parameters["properties"]["files"]["items"], {"type": "object"})
        self.assertNotIn("additionalProperties", parameters["properties"]["metadata"])

    def test_provider_aware_token_estimator_is_available(self):
        from common import const
        from models.token_estimator import estimate_text_tokens

        self.assertGreater(estimate_text_tokens("hello world", const.GPT_55, const.OPENAI), 0)
        self.assertGreater(estimate_text_tokens("中文 token 估算", const.DEEPSEEK_V4_PRO, const.DEEPSEEK), 0)

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

    def test_custom_openai_compatible_o1_keeps_system_messages(self):
        from models.openai_compatible_bot import OpenAICompatibleBot

        class CaptureBot(OpenAICompatibleBot):
            def get_api_config(self):
                return {
                    "provider": "openai",
                    "api_key": "test-key",
                    "api_base": "https://coding-plan.test/v1",
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

        self.assertEqual([message["role"] for message in payload["messages"]], ["system", "user"])
        self.assertEqual(payload["messages"][0]["content"], "rules")

    def test_azure_route_uses_azure_capability_rules(self):
        from common import const
        from bridge.agent_bridge import AgentLLMModel
        from models.chatgpt.chat_gpt_bot import ChatGPTBot
        from models.model_capabilities import build_provider_capability_matrix, capabilities_for_config, get_model_capabilities
        from models.openai_compatible_bot import OpenAICompatibleBot

        self.assertEqual(
            OpenAICompatibleBot._capability_provider_id(
                {"provider": const.CHATGPTONAZURE},
                "https://example.openai.azure.com/openai/deployments/chat",
            ),
            const.CHATGPTONAZURE,
        )
        capabilities = get_model_capabilities("gpt-5.5", const.CHATGPTONAZURE)
        self.assertFalse(capabilities.supports_temperature)
        self.assertTrue(capabilities.supports_reasoning_effort)
        self.assertEqual(capabilities.max_tokens_param, "max_completion_tokens")

        matrix = build_provider_capability_matrix({const.CHATGPTONAZURE: ["gpt-5.5", "o1-mini"]})
        azure_gpt = matrix["providers"][const.CHATGPTONAZURE]["models"][0]
        self.assertEqual(azure_gpt["api_family"], "azure_openai")
        self.assertEqual(azure_gpt["host_policy"], "azure_deployment")
        self.assertIn(f"{const.CHATGPTONAZURE}:azure-openai-base", azure_gpt["rule_ids"])
        self.assertNotIn("responses_adapter", azure_gpt["surfaces"])

        with patch("models.chatgpt.chat_gpt_bot.conf", return_value={
            "bot_type": const.OPENAI,
            "model": "gpt-5.5",
            "open_ai_api_key": "test-key",
            "open_ai_api_base": "https://example.openai.azure.com/",
            "temperature": 0.7,
            "top_p": 0.9,
        }):
            bot = ChatGPTBot()
            bot.configure_model_route(const.CHATGPTONAZURE)
            self.assertEqual(bot.get_api_config()["provider"], const.CHATGPTONAZURE)

        legacy_azure_config = {
            "bot_type": "",
            "use_azure_chatgpt": True,
            "model": "gpt-5.5",
            "open_ai_api_base": "https://example.openai.azure.com/",
            "use_linkai": False,
        }
        legacy_azure_capabilities = capabilities_for_config(legacy_azure_config)
        self.assertEqual(legacy_azure_capabilities.provider, const.CHATGPTONAZURE)
        self.assertEqual(legacy_azure_capabilities.max_tokens_param, "max_completion_tokens")
        self.assertTrue(legacy_azure_capabilities.supports_reasoning_effort)

        with patch("bridge.agent_bridge.conf", return_value=legacy_azure_config):
            bridge_model = AgentLLMModel(None)
            self.assertEqual(bridge_model._resolve_bot_type("gpt-5.5"), const.CHATGPTONAZURE)

        with patch("models.chatgpt.chat_gpt_bot.conf", return_value={
            **legacy_azure_config,
            "open_ai_api_key": "test-key",
            "temperature": 0.7,
            "top_p": 0.9,
        }):
            legacy_flag_bot = ChatGPTBot()
            self.assertEqual(legacy_flag_bot.get_api_config()["provider"], const.CHATGPTONAZURE)
            self.assertNotIn("temperature", legacy_flag_bot.args)
            self.assertEqual(legacy_flag_bot.args["model"], "gpt-5.5")

        with patch("models.chatgpt.chat_gpt_bot.conf", return_value={
            **legacy_azure_config,
            "open_ai_api_key": "test-key",
            "azure_deployment_id": "chat-deployment",
            "azure_api_version": "2024-02-15-preview",
        }):
            azure_bot = AgentLLMModel._create_bot(const.CHATGPTONAZURE)
            self.assertEqual(type(azure_bot._http_client).__name__, "_AzureChatHTTPClient")
            self.assertIn("/openai/deployments/chat-deployment", azure_bot._http_client.api_base)
            self.assertEqual(azure_bot._http_client._build_headers(None, None).get("api-key"), "test-key")

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

    def test_chatgpt_custom_route_does_not_borrow_gemini_rest_credentials(self):
        from common import const
        from models.chatgpt.chat_gpt_bot import ChatGPTBot

        local_config = {
            "bot_type": const.CUSTOM,
            "model": const.GEMINI_31_PRO_PRE,
            "request_timeout": 30,
            "gemini_api_key": "legacy-custom-gemini-key",
            "gemini_api_base": "http://custom-gemini.test:8080",
            "custom_api_key": "",
            "custom_api_base": "",
        }

        with patch("models.chatgpt.chat_gpt_bot.conf", return_value=local_config):
            bot = ChatGPTBot()
            api_config = bot.get_api_config()

        self.assertEqual(api_config["provider"], const.CUSTOM)
        self.assertEqual(api_config["api_key"], "")
        self.assertIsNone(api_config["api_base"])
        self.assertNotEqual(bot._get_http_client().api_base, "http://custom-gemini.test:8080")

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

    def test_provider_capability_matrix_is_machine_readable_and_catalog_derived(self):
        from common import const
        from models.model_capabilities import build_provider_capability_matrix

        matrix = build_provider_capability_matrix({
            "openai": [const.GPT_55, "o1-mini", const.GPT_4o],
            "deepseek": [const.DEEPSEEK_V4_FLASH],
            "custom": [],
        })

        self.assertEqual(matrix["schema_version"], "ecorex.model-capabilities.v1")
        self.assertEqual(matrix["source"], "models.model_capabilities._CAPABILITY_RULES")
        json.dumps(matrix, sort_keys=True)
        self.assertEqual(
            [row["model"] for row in matrix["providers"]["openai"]["models"]],
            [const.GPT_55, "o1-mini", const.GPT_4o],
        )

        gpt55_row = matrix["providers"]["openai"]["models"][0]
        gpt55 = gpt55_row["capabilities"]
        self.assertEqual(gpt55_row["api_family"], "official_openai")
        self.assertEqual(gpt55_row["host_policy"], "official_openai_host_required")
        self.assertEqual(gpt55_row["token_limit"]["chat_param"], "max_completion_tokens")
        self.assertIn("temperature", gpt55_row["unsupported_params"])
        self.assertIn("openai:official-openai-base", gpt55_row["rule_ids"])
        self.assertIn("openai:fixed-sampling-reasoning-models", gpt55_row["rule_ids"])
        self.assertFalse(gpt55["supports_temperature"])
        self.assertFalse(gpt55["supports_penalties"])
        self.assertTrue(gpt55["supports_reasoning_effort"])
        self.assertEqual(gpt55["max_tokens_param"], "max_completion_tokens")

        o1_row = matrix["providers"]["openai"]["models"][1]
        o1 = o1_row["capabilities"]
        self.assertEqual(o1_row["system_message_policy"], "coerce_to_user")
        self.assertIn("openai:o1-system-message-coercion", o1_row["rule_ids"])
        self.assertFalse(o1["supports_system_messages"])

        deepseek_row = matrix["providers"]["deepseek"]["models"][0]
        deepseek = deepseek_row["capabilities"]
        self.assertTrue(deepseek_row["thinking"]["supported"])
        self.assertEqual(deepseek_row["surfaces"], ["agent_bridge"])
        self.assertNotIn("responses_adapter", deepseek_row["surfaces"])
        self.assertIn("deepseek:v4-thinking", deepseek_row["rule_ids"])
        self.assertTrue(deepseek["supports_thinking_param"])
        self.assertEqual(tuple(deepseek["reasoning_effort_values"]), ("high", "max"))

        self.assertEqual(matrix["providers"]["custom"]["models"], [])

    def test_provider_capability_matrix_keeps_custom_o1_generic(self):
        from models.model_capabilities import build_provider_capability_matrix

        matrix = build_provider_capability_matrix({
            "custom": ["o1-mini"],
        })

        custom_o1 = matrix["providers"]["custom"]["models"][0]
        self.assertEqual(custom_o1["api_family"], "openai_compatible")
        self.assertEqual(custom_o1["system_message_policy"], "native")
        self.assertNotIn("custom:o1-system-message-coercion", custom_o1["rule_ids"])
        self.assertTrue(custom_o1["capabilities"]["supports_system_messages"])


if __name__ == "__main__":
    unittest.main()
