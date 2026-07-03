# encoding:utf-8
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if "web" not in sys.modules:
    web_stub = types.ModuleType("web")
    web_stub.HTTPError = type("HTTPError", (Exception,), {})
    web_stub.cookies = lambda: {}
    web_stub.header = lambda *args, **kwargs: None
    web_stub.data = lambda: b"{}"
    web_stub.input = lambda **kwargs: types.SimpleNamespace(**kwargs)
    web_stub.setcookie = lambda *args, **kwargs: None
    web_stub.seeother = lambda *args, **kwargs: Exception("seeother")
    web_stub.notfound = lambda *args, **kwargs: Exception("notfound")
    web_stub.badrequest = lambda *args, **kwargs: Exception("badrequest")
    web_stub.application = lambda *args, **kwargs: types.SimpleNamespace(wsgifunc=lambda: None)
    web_stub.httpserver = types.SimpleNamespace(
        LogMiddleware=type("LogMiddleware", (), {"log": lambda *args, **kwargs: None}),
        StaticMiddleware=lambda app: app,
        WSGIServer=lambda *args, **kwargs: types.SimpleNamespace(serve_forever=lambda: None),
    )
    sys.modules["web"] = web_stub


class TestModelsHandler(unittest.TestCase):
    def test_set_asr_capability_persists_provider_and_model(self):
        from channel.web.web_channel import ModelsHandler

        local_config = {}
        file_config = {}
        handler = ModelsHandler()

        with patch("channel.web.web_channel.conf", return_value=local_config):
            with patch.object(ModelsHandler, "_read_file_config", return_value=file_config):
                with patch.object(ModelsHandler, "_write_file_config") as write_file:
                    with patch.object(ModelsHandler, "_refresh_voice_routing") as refresh_voice:
                        result = json.loads(handler._handle_set_capability({
                            "capability": "asr",
                            "provider_id": "dashscope",
                            "model": "qwen3-asr-flash",
                        }))

        self.assertEqual(result["status"], "success")
        self.assertEqual(local_config["voice_to_text"], "dashscope")
        self.assertEqual(local_config["voice_to_text_model"], "qwen3-asr-flash")
        self.assertEqual(file_config["voice_to_text"], "dashscope")
        self.assertEqual(file_config["voice_to_text_model"], "qwen3-asr-flash")
        write_file.assert_called_once_with(file_config)
        refresh_voice.assert_called_once()

    def test_set_asr_empty_model_keeps_existing(self):
        # Switching provider with an empty model must not wipe a user's
        # hand-configured voice_to_text_model.
        from channel.web.web_channel import ModelsHandler

        local_config = {"voice_to_text_model": "qwen3-asr-flash"}
        file_config = {"voice_to_text_model": "qwen3-asr-flash"}
        handler = ModelsHandler()

        with patch("channel.web.web_channel.conf", return_value=local_config):
            with patch.object(ModelsHandler, "_read_file_config", return_value=file_config):
                with patch.object(ModelsHandler, "_write_file_config"):
                    with patch.object(ModelsHandler, "_refresh_voice_routing"):
                        result = json.loads(handler._handle_set_capability({
                            "capability": "asr",
                            "provider_id": "zhipu",
                            "model": "",
                        }))

        self.assertEqual(result["status"], "success")
        self.assertEqual(local_config["voice_to_text"], "zhipu")
        # Existing model preserved, not overwritten with "".
        self.assertEqual(local_config["voice_to_text_model"], "qwen3-asr-flash")
        self.assertEqual(file_config["voice_to_text_model"], "qwen3-asr-flash")
        self.assertEqual(result["model"], "qwen3-asr-flash")

    def test_asr_capability_exposes_provider_models(self):
        from channel.web.web_channel import ModelsHandler

        cap = ModelsHandler._asr_capability({
            "voice_to_text": "dashscope",
            "voice_to_text_model": "qwen3-asr-flash",
        })

        self.assertTrue(cap["editable"])
        self.assertEqual(cap["current_provider"], "dashscope")
        self.assertEqual(cap["current_model"], "qwen3-asr-flash")
        self.assertIn("provider_models", cap)
        self.assertIn("dashscope", cap["provider_models"])

    def test_chat_capability_exposes_model_catalog_capabilities(self):
        from channel.web.web_channel import ModelsHandler

        cap = ModelsHandler._chat_capability({
            "model": "gpt-5.5",
            "bot_type": "chatGPT",
            "use_linkai": False,
        })

        self.assertEqual(cap["current_provider"], "openai")
        self.assertEqual(cap["current_model"], "gpt-5.5")
        self.assertEqual(cap["capabilities"]["provider"], "openai")
        self.assertFalse(cap["capabilities"]["supports_temperature"])
        self.assertFalse(cap["capabilities"]["supports_penalties"])
        self.assertTrue(cap["capabilities"]["supports_reasoning_effort"])
        self.assertTrue(cap["capabilities"]["supports_verbosity"])
        self.assertFalse(cap["capabilities"]["supports_thinking_param"])
        self.assertEqual(cap["capabilities"]["max_tokens_param"], "max_completion_tokens")
        self.assertTrue(cap["capabilities"]["supports_stream_usage"])
        self.assertEqual(cap["capability_matrix"]["schema_version"], "ecorex.model-capabilities.v1")
        self.assertIn("openai", cap["capability_matrix"]["providers"])
        self.assertIn("deepseek", cap["capability_matrix"]["providers"])
        self.assertIn("chatGPTOnAzure", cap["capability_matrix"]["providers"])
        openai_models = cap["capability_matrix"]["providers"]["openai"]["models"]
        self.assertTrue(any(row["model"] == "gpt-5.5" for row in openai_models))
        deepseek_models = cap["capability_matrix"]["providers"]["deepseek"]["models"]
        self.assertTrue(any(row["capabilities"]["supports_thinking_param"] for row in deepseek_models))
        matrix_text = json.dumps(cap["capability_matrix"], ensure_ascii=False)
        self.assertNotIn("api_key", matrix_text)
        self.assertNotIn("api_base", matrix_text)

    def test_chat_model_options_use_configured_top_tier_models_first(self):
        from channel.web.web_channel import ModelsHandler
        from common import const

        cap = ModelsHandler._chat_capability({
            "model": const.DEEPSEEK_V4_PRO,
            "bot_type": const.DEEPSEEK,
            "deepseek_api_key": "test-deepseek",
            "gemini_api_key": "test-gemini",
            "ark_api_key": "test-doubao",
            "use_linkai": False,
        })
        options = cap["model_options"]

        by_provider = {}
        for option in options:
            by_provider.setdefault(option["provider"], []).append(option["model"])

        self.assertEqual(len(options), 4)
        self.assertEqual({provider: len(models) for provider, models in by_provider.items()}, {
            "openai": 1,
            "deepseek": 1,
            "gemini": 1,
            "doubao": 1,
        })
        self.assertEqual(by_provider["openai"][0], const.GPT_55)
        self.assertEqual(by_provider["deepseek"][0], const.DEEPSEEK_V4_PRO)
        self.assertEqual(by_provider["gemini"][0], const.GEMINI_31_PRO_PRE)
        self.assertEqual(by_provider["doubao"][0], const.DOUBAO_SEED_2_PRO)
        self.assertTrue(any(option["current"] for option in options if option["model"] == const.DEEPSEEK_V4_PRO))
        openai = next(option for option in options if option["provider"] == "openai")
        self.assertFalse(openai["configured"])
        self.assertEqual(openai["hint"], "needs credentials")
        deepseek = next(option for option in options if option["model"] == const.DEEPSEEK_V4_PRO)
        self.assertEqual(deepseek["contextPolicy"]["contextWindowTokens"], 1000000)
        self.assertEqual(deepseek["contextPolicy"]["autoCompactTokenLimit"], 800000)
        self.assertEqual(deepseek["contextPolicy"]["tokenizerStatus"], "estimated")

    def test_chat_model_options_preserve_gpt55_as_current_openai_model(self):
        from channel.web.web_channel import ModelsHandler
        from common import const

        cap = ModelsHandler._chat_capability({
            "model": const.GPT_55,
            "bot_type": "chatGPT",
            "open_ai_api_key": "test-openai",
            "deepseek_api_key": "test-deepseek",
            "gemini_api_key": "test-gemini",
            "ark_api_key": "test-doubao",
            "use_linkai": False,
        })
        options = cap["model_options"]
        by_provider = {}
        for option in options:
            by_provider.setdefault(option["provider"], []).append(option)

        self.assertEqual(cap["current_provider"], "openai")
        self.assertEqual(cap["current_model"], const.GPT_55)
        self.assertEqual(len(options), 4)
        self.assertEqual(set(by_provider), {"openai", "deepseek", "gemini", "doubao"})
        self.assertTrue(all(len(provider_options) == 1 for provider_options in by_provider.values()))
        openai = by_provider["openai"][0]
        self.assertEqual(openai["model"], const.GPT_55)
        self.assertTrue(openai["current"])
        self.assertEqual(openai["contextPolicy"]["contextWindowTokens"], 1000000)
        self.assertEqual(openai["contextPolicy"]["tokenizerStatus"], "local_tokenizer")

    def test_chat_model_options_mark_current_custom_model_configured_when_provider_has_key(self):
        from channel.web.web_channel import ModelsHandler

        cap = ModelsHandler._chat_capability({
            "model": "custom-doubao-endpoint-id",
            "bot_type": "doubao",
            "ark_api_key": "test-doubao",
            "use_linkai": False,
        })

        current = cap["model_options"][0]
        self.assertEqual(current["provider"], "doubao")
        self.assertEqual(current["model"], "custom-doubao-endpoint-id")
        self.assertTrue(current["configured"])
        self.assertTrue(current["current"])

    def test_chat_capability_keeps_custom_gemini_alias_on_custom_route(self):
        from channel.web.web_channel import ModelsHandler
        from common import const

        cap = ModelsHandler._chat_capability({
            "model": "gemini-custom-proxy",
            "bot_type": const.CUSTOM,
            "custom_api_key": "test-custom",
            "custom_api_base": "https://custom-gemini.test/v1",
            "gemini_api_key": "test-official-gemini",
            "use_linkai": False,
        })

        current = next(option for option in cap["model_options"] if option["current"])
        self.assertEqual(cap["current_provider"], "custom")
        self.assertEqual(cap["capabilities"]["provider"], "custom")
        self.assertEqual(current["provider"], "custom")
        self.assertEqual(current["model"], "gemini-custom-proxy")
        self.assertEqual(current["modelAliasFamily"], "gemini")
        self.assertEqual(current["effectiveTransportProvider"], "custom")
        self.assertFalse(current["isOfficialGeminiProvider"])
        self.assertFalse(current["officialGeminiApiUsed"])

    def test_chat_model_options_expose_custom_gemini_candidate_when_custom_transport_configured(self):
        from channel.web.web_channel import ModelsHandler
        from common import const

        cap = ModelsHandler._chat_capability({
            "model": const.GEMINI_31_PRO_PRE,
            "bot_type": const.GEMINI,
            "gemini_api_key": "test-official-gemini",
            "custom_api_key": "test-custom",
            "custom_api_base": "https://custom-gemini.test/v1",
            "use_linkai": False,
        })

        custom = next(
            option
            for option in cap["model_options"]
            if option["provider"] == const.CUSTOM and option["model"] == const.GEMINI_31_PRO_PRE
        )
        official = next(
            option
            for option in cap["model_options"]
            if option["provider"] == const.GEMINI and option["model"] == const.GEMINI_31_PRO_PRE
        )

        self.assertEqual(cap["current_provider"], const.GEMINI)
        self.assertTrue(official["current"])
        self.assertFalse(custom["current"])
        self.assertTrue(custom["configured"])
        self.assertEqual(custom["modelAliasFamily"], "gemini")
        self.assertEqual(custom["effectiveTransportProvider"], const.CUSTOM)
        self.assertFalse(custom["isOfficialGeminiProvider"])

    def test_chat_capability_treats_nonofficial_gemini_base_as_custom_transport(self):
        from channel.web.web_channel import ModelsHandler
        from common import const

        cap = ModelsHandler._chat_capability({
            "model": const.GEMINI_31_PRO_PRE,
            "bot_type": const.GEMINI,
            "gemini_api_key": "legacy-custom-gemini-key",
            "gemini_api_base": "https://custom-gemini.test/v1",
            "use_linkai": False,
        })

        current = next(option for option in cap["model_options"] if option["current"])
        self.assertEqual(cap["current_provider"], const.CUSTOM)
        self.assertEqual(cap["capabilities"]["provider"], const.CUSTOM)
        self.assertEqual(current["provider"], const.CUSTOM)
        self.assertEqual(current["model"], const.GEMINI_31_PRO_PRE)
        self.assertEqual(current["modelAliasFamily"], "gemini")
        self.assertFalse(current["isOfficialGeminiProvider"])
        self.assertFalse(any(
            option["provider"] == const.GEMINI and option["configured"]
            for option in cap["model_options"]
        ))

    def test_chat_options_keep_legacy_custom_gemini_when_current_model_is_other_provider(self):
        from channel.web.web_channel import ModelsHandler
        from common import const

        local_config = {
            "model": "deepseek-v4-pro",
            "bot_type": "deepseek",
            "deepseek_api_key": "deepseek-key",
            "gemini_api_key": "legacy-custom-gemini-key",
            "gemini_api_base": "https://custom-gemini.test/v1",
            "use_linkai": False,
        }
        cap = ModelsHandler._chat_capability(local_config)

        custom = next(
            option
            for option in cap["model_options"]
            if option["provider"] == const.CUSTOM and option["model"] == const.GEMINI_31_PRO_PRE
        )
        valid, message = ModelsHandler._validate_chat_selection(local_config, const.CUSTOM, const.GEMINI_31_PRO_PRE)

        self.assertEqual(cap["current_provider"], "deepseek")
        self.assertTrue(custom["configured"])
        self.assertEqual(custom["modelAliasFamily"], "gemini")
        self.assertFalse(custom["isOfficialGeminiProvider"])
        self.assertTrue(valid, message)

    def test_set_chat_migrates_legacy_custom_gemini_transport_without_exposing_secret(self):
        from channel.web.web_channel import ModelsHandler
        from common import const

        local_config = {
            "model": const.GEMINI_31_PRO_PRE,
            "bot_type": const.GEMINI,
            "gemini_api_key": "legacy-custom-gemini-key",
            "gemini_api_base": "https://custom-gemini.test/v1",
            "use_linkai": False,
        }
        file_config = dict(local_config)
        handler = ModelsHandler()

        with patch("channel.web.web_channel.conf", return_value=local_config):
            with patch.object(ModelsHandler, "_read_file_config", return_value=file_config):
                with patch.object(ModelsHandler, "_write_file_config") as write_file:
                    with patch.object(ModelsHandler, "_reset_bridge", return_value={
                        "agentBridgePreserved": True,
                        "modelRoutesReset": 1,
                        "strategy": "refresh_chat_routing",
                    }):
                        result = json.loads(handler._handle_set_capability({
                            "capability": "chat",
                            "provider_id": const.CUSTOM,
                            "model": const.GEMINI_31_PRO_PRE,
                        }))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["provider"], const.CUSTOM)
        self.assertEqual(local_config["bot_type"], const.CUSTOM)
        self.assertEqual(file_config["bot_type"], const.CUSTOM)
        self.assertEqual(local_config["custom_api_key"], "legacy-custom-gemini-key")
        self.assertEqual(local_config["custom_api_base"], "https://custom-gemini.test/v1")
        self.assertEqual(file_config["custom_api_key"], "legacy-custom-gemini-key")
        self.assertEqual(file_config["custom_api_base"], "https://custom-gemini.test/v1")
        self.assertTrue(result["applied"]["custom_transport_migrated"])
        self.assertNotIn("legacy-custom-gemini-key", json.dumps(result))
        write_file.assert_called_once_with(file_config)

    def test_set_chat_migrates_legacy_custom_gemini_when_switching_from_gpt(self):
        from channel.web.web_channel import ModelsHandler
        from common import const

        local_config = {
            "model": const.GPT_55,
            "bot_type": const.CHATGPT,
            "gemini_api_key": "legacy-custom-gemini-key",
            "gemini_api_base": "http://custom-gemini.test:8080",
            "use_linkai": False,
        }
        file_config = dict(local_config)
        handler = ModelsHandler()

        with patch("channel.web.web_channel.conf", return_value=local_config):
            with patch.object(ModelsHandler, "_read_file_config", return_value=file_config):
                with patch.object(ModelsHandler, "_write_file_config") as write_file:
                    with patch.object(ModelsHandler, "_reset_bridge", return_value={
                        "agentBridgePreserved": True,
                        "modelRoutesReset": 1,
                        "strategy": "refresh_chat_routing",
                    }):
                        result = json.loads(handler._handle_set_capability({
                            "capability": "chat",
                            "provider_id": const.CUSTOM,
                            "model": const.GEMINI_31_PRO_PRE,
                        }))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["provider"], const.CUSTOM)
        self.assertEqual(local_config["bot_type"], const.CUSTOM)
        self.assertEqual(local_config["model"], const.GEMINI_31_PRO_PRE)
        self.assertEqual(local_config["custom_api_key"], "legacy-custom-gemini-key")
        self.assertEqual(local_config["custom_api_base"], "http://custom-gemini.test:8080/v1")
        self.assertEqual(file_config["custom_api_base"], "http://custom-gemini.test:8080/v1")
        self.assertTrue(result["contextContinuity"]["agentBridgePreserved"])
        self.assertNotIn("legacy-custom-gemini-key", json.dumps(result))
        write_file.assert_called_once_with(file_config)

    def test_chat_capability_downgrades_custom_openai_base(self):
        from channel.web.web_channel import ModelsHandler

        cap = ModelsHandler._chat_capability({
            "model": "gpt-5.5",
            "bot_type": "chatGPT",
            "open_ai_api_base": "https://coding-plan.test/v1",
            "use_linkai": False,
        })

        self.assertEqual(cap["current_provider"], "openai_compatible")
        self.assertEqual(cap["capabilities"]["provider"], "openai_compatible")
        self.assertTrue(cap["capabilities"]["supports_temperature"])
        self.assertFalse(cap["capabilities"]["supports_stream_usage"])
        self.assertFalse(cap["capabilities"]["supports_reasoning_effort"])
        self.assertFalse(cap["capabilities"]["supports_verbosity"])
        self.assertEqual(cap["capabilities"]["max_tokens_param"], "max_tokens")

    def test_chat_capability_routes_enterprise_openai_policy_to_openai_option(self):
        from channel.web.web_channel import ModelsHandler
        from common import const

        cap = ModelsHandler._chat_capability({
            "model": const.GPT_55,
            "bot_type": const.OPENAI,
            "open_ai_api_key": "enterprise-openai-key",
            "open_ai_api_base": "https://enterprise-openai.test/v1",
            "use_linkai": False,
        })

        self.assertEqual(cap["current_provider"], "openai")
        self.assertEqual(cap["capabilities"]["provider"], "openai")
        self.assertTrue(cap["capabilities"]["supports_reasoning_effort"])
        self.assertEqual(cap["capabilities"]["max_tokens_param"], "max_completion_tokens")
        current = next(option for option in cap["model_options"] if option["current"])
        self.assertEqual(current["provider"], "openai")
        self.assertTrue(current["configured"])

    def test_set_chat_preserves_image_generation_model(self):
        from channel.web.web_channel import ModelsHandler
        from common import const

        local_config = {
            "model": const.GPT_55,
            "bot_type": "chatGPT",
            "deepseek_api_key": "test-deepseek",
            "use_linkai": False,
            "text_to_image": "gpt-image-2-pro",
            "skills": {
                "image-generation": {
                    "provider": "openai",
                    "model": "gpt-image-2-pro",
                }
            },
        }
        file_config = json.loads(json.dumps(local_config))
        handler = ModelsHandler()

        with patch("channel.web.web_channel.conf", return_value=local_config):
            with patch.object(ModelsHandler, "_read_file_config", return_value=file_config):
                with patch.object(ModelsHandler, "_write_file_config") as write_file:
                    with patch.object(ModelsHandler, "_reset_bridge") as reset_bridge:
                        result = json.loads(handler._handle_set_capability({
                            "capability": "chat",
                            "provider_id": "deepseek",
                            "model": const.DEEPSEEK_V4_PRO,
                        }))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["model"], const.DEEPSEEK_V4_PRO)
        self.assertEqual(result["image_model"], "gpt-image-2-pro")
        self.assertEqual(result["context_policy"]["contextWindowTokens"], 1000000)
        self.assertEqual(result["context_policy"]["autoCompactTokenLimit"], 800000)
        self.assertEqual(local_config["model"], const.DEEPSEEK_V4_PRO)
        self.assertEqual(local_config["bot_type"], "deepseek")
        self.assertFalse(local_config["use_linkai"])
        self.assertEqual(local_config["model_context_window"], 1000000)
        self.assertEqual(local_config["model_auto_compact_token_limit"], 800000)
        self.assertEqual(local_config["agent_max_context_tokens"], 800000)
        self.assertEqual(local_config["text_to_image"], "gpt-image-2-pro")
        self.assertEqual(local_config["skills"]["image-generation"]["model"], "gpt-image-2-pro")
        self.assertEqual(file_config["skills"]["image-generation"]["model"], "gpt-image-2-pro")
        self.assertEqual(file_config["model_auto_compact_token_limit"], 800000)
        write_file.assert_called_once_with(file_config)
        reset_bridge.assert_called_once()

    def test_set_chat_accepts_custom_gemini_alias_and_reports_context_continuity(self):
        from channel.web.web_channel import ModelsHandler
        from common import const

        local_config = {
            "model": const.GPT_55,
            "bot_type": const.CHATGPT,
            "custom_api_key": "test-custom",
            "custom_api_base": "https://custom-gemini.test/v1",
            "use_linkai": False,
        }
        file_config = json.loads(json.dumps(local_config))
        handler = ModelsHandler()

        with patch("channel.web.web_channel.conf", return_value=local_config):
            with patch.object(ModelsHandler, "_read_file_config", return_value=file_config):
                with patch.object(ModelsHandler, "_write_file_config") as write_file:
                    with patch.object(ModelsHandler, "_reset_bridge", return_value={
                        "agentBridgePreserved": True,
                        "modelRoutesReset": 1,
                        "strategy": "refresh_chat_routing",
                    }) as reset_bridge:
                        result = json.loads(handler._handle_set_capability({
                            "capability": "chat",
                            "provider_id": "custom",
                            "model": "gemini-custom-proxy",
                        }))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["provider"], "custom")
        self.assertEqual(result["model"], "gemini-custom-proxy")
        self.assertEqual(result["modelAliasFamily"], "gemini")
        self.assertEqual(result["effectiveTransportProvider"], "custom")
        self.assertFalse(result["isOfficialGeminiProvider"])
        self.assertFalse(result["officialGeminiApiUsed"])
        self.assertTrue(result["contextContinuity"]["agentBridgePreserved"])
        self.assertEqual(result["contextContinuity"]["existingAgentRoutesReset"], 1)
        self.assertEqual(result["contextContinuity"]["artifactHistoryRefs"], "enabled")
        self.assertEqual(local_config["bot_type"], const.CUSTOM)
        self.assertEqual(local_config["model"], "gemini-custom-proxy")
        write_file.assert_called_once_with(file_config)
        reset_bridge.assert_called_once()

    def test_generic_config_model_change_refreshes_chat_routing_without_full_agent_reset(self):
        with open("channel/web/web_channel.py", "r", encoding="utf-8") as handle:
            source = handle.read()
        config_block = source[
            source.index('bridge_routing_keys = {"bot_type", "use_linkai", "model"}'):
            source.index('return json.dumps({"status": "success", "applied": applied}', source.index('bridge_routing_keys = {"bot_type", "use_linkai", "model"}'))
        ]

        self.assertIn('getattr(bridge, "refresh_chat_routing", None)', config_block)
        self.assertIn("refresh()", config_block)
        self.assertNotIn("Bridge().reset_bot()", config_block)

    def test_set_chat_rejects_unconfigured_provider(self):
        from channel.web.web_channel import ModelsHandler
        from common import const

        local_config = {
            "model": const.GPT_55,
            "bot_type": "chatGPT",
            "use_linkai": False,
            "text_to_image": "gpt-image-2-pro",
        }
        file_config = json.loads(json.dumps(local_config))
        handler = ModelsHandler()

        with patch("channel.web.web_channel.conf", return_value=local_config):
            with patch.object(ModelsHandler, "_read_file_config", return_value=file_config):
                with patch.object(ModelsHandler, "_write_file_config") as write_file:
                    with patch.object(ModelsHandler, "_reset_bridge") as reset_bridge:
                        result = json.loads(handler._handle_set_capability({
                            "capability": "chat",
                            "provider_id": "deepseek",
                            "model": const.DEEPSEEK_V4_PRO,
                        }))

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "CHAT_MODEL_NOT_CONFIGURED")
        self.assertEqual(local_config["model"], const.GPT_55)
        write_file.assert_not_called()
        reset_bridge.assert_not_called()

    def test_cached_enterprise_model_policy_applies_only_settings(self):
        import tempfile
        from pathlib import Path

        from config import _apply_cached_enterprise_model_policy

        cfg = {
            "model": "gpt-5.5",
            "bot_type": "chatGPT",
            "open_ai_api_key": "",
            "open_ai_api_base": "https://api.openai.com/v1",
        }

        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "enterprise-model-policy.json"
            policy_path.write_text(json.dumps({
                "configured": True,
                "provider": "openai",
                "model": "gpt-5.5",
                "userEmail": "should-not-copy@example.com",
                "settings": {
                    "model": "gpt-5.5",
                    "bot_type": "openai",
                    "open_ai_api_key": "enterprise-openai-key",
                    "open_ai_api_base": "https://enterprise-openai.test/v1",
                    "userEmail": "should-not-copy@example.com",
                },
            }), encoding="utf-8")

            with patch.dict(os.environ, {"ECOREX_ENTERPRISE_MODEL_POLICY_FILE": str(policy_path)}, clear=False):
                applied = _apply_cached_enterprise_model_policy(cfg)

        self.assertTrue(applied)
        self.assertEqual(cfg["open_ai_api_key"], "enterprise-openai-key")
        self.assertEqual(cfg["open_ai_api_base"], "https://enterprise-openai.test/v1")
        self.assertEqual(cfg["bot_type"], "openai")
        self.assertNotIn("userEmail", cfg)


if __name__ == "__main__":
    unittest.main()
