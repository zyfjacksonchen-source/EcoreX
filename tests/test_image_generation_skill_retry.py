# encoding:utf-8

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


def load_image_generation_module(name="ecorex_image_generation_retry_test"):
    script_path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "image-generation"
        / "scripts"
        / "generate.py"
    )
    spec = importlib.util.spec_from_file_location(name, script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code, data=None, headers=None, text=None, content=b""):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(self._data)
        self.reason = self.text
        self.url = "https://provider.test/image"
        self.content = content

    def json(self):
        return self._data


class TestImageGenerationSkillRetry(unittest.TestCase):
    def test_openai_retries_retry_after_then_succeeds(self):
        module = load_image_generation_module()
        provider = module.OpenAIProvider("sk-test", "https://api.openai.test/v1", "gpt-image-2")
        sleeps = []
        calls = []
        rate_limited = FakeResponse(
            429,
            {
                "error": {
                    "message": "image rate limit",
                    "code": "rate_limit_exceeded",
                    "type": "rate_limit",
                }
            },
            headers={"Retry-After": "0.25"},
        )
        success = FakeResponse(200, {"data": [{"b64_json": "aGVsbG8="}]})

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return [rate_limited, success][len(calls) - 1]

        def record_sleep(value):
            if value == 0.25:
                sleeps.append(value)

        with tempfile.TemporaryDirectory() as output_dir:
            with patch.object(module.time, "sleep", record_sleep):
                with patch.object(module.requests, "post", side_effect=fake_post):
                    with patch.object(module, "_save_image", return_value=str(Path(output_dir) / "out.png")):
                        paths = provider.generate("draw", output_dir=output_dir)

        self.assertEqual(paths, [str(Path(output_dir) / "out.png")])
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [0.25])

    def test_retry_helper_preserves_retry_after_for_all_provider_labels(self):
        module = load_image_generation_module("ecorex_image_generation_all_labels_retry_test")

        for label in ("OpenAI", "LinkAI", "Gemini", "Seedream", "Qwen", "MiniMax"):
            with self.subTest(label=label):
                sleeps = []
                calls = []
                rate_limited = FakeResponse(
                    429,
                    {
                        "error": {
                            "message": f"{label} rate limit",
                            "code": "rate_limit_exceeded",
                            "type": "rate_limit",
                        }
                    },
                    headers={"Retry-After": "0.1"},
                )
                success = FakeResponse(200, {"ok": True})

                def fake_post(url, **kwargs):
                    calls.append((url, kwargs))
                    return [rate_limited, success][len(calls) - 1]

                def record_sleep(value):
                    if value == 0.1:
                        sleeps.append(value)

                with patch.object(module.time, "sleep", record_sleep):
                    with patch.object(module.requests, "post", side_effect=fake_post):
                        result = module._post_json_with_retries(
                            label,
                            "https://provider.test/images",
                            {"Authorization": "Bearer test"},
                            {"prompt": "draw"},
                        )

                self.assertEqual(result, {"ok": True})
                self.assertEqual(len(calls), 2)
                self.assertEqual(sleeps, [0.1])

    def test_retry_after_parser_accepts_http_date_and_body_milliseconds(self):
        module = load_image_generation_module("ecorex_image_generation_retry_after_parser_test")

        past_http_date = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=5))
        self.assertEqual(module._parse_retry_after(past_http_date), 0.0)

        error = module._provider_error_from_body(
            "Qwen",
            {
                "message": "dashscope throttled",
                "code": "Throttling.User",
                "status_code": 429,
                "retry_after_ms": 250,
            },
            default_status=429,
        )
        self.assertEqual(error.retry_after, 0.25)
        self.assertEqual(error.taxonomy, "rate_limit")

    def test_main_fails_closed_on_non_retryable_4xx_without_provider_fallback(self):
        module = load_image_generation_module("ecorex_image_generation_fail_closed_test")
        bad_request = FakeResponse(
            400,
            {
                "error": {
                    "message": "bad image prompt",
                    "code": "invalid_prompt",
                    "type": "invalid_request_error",
                }
            },
        )
        stdout = io.StringIO()
        env = {
            "OPENAI_API_KEY": "sk-test",
            "LINKAI_API_KEY": "lk-test",
            "IMAGE_OUTPUT_DIR": tempfile.gettempdir(),
        }
        argv = ["generate.py", json.dumps({"prompt": "bad", "provider": "openai"})]

        with patch.dict(os.environ, env, clear=False):
            with patch.object(sys, "argv", argv):
                with patch.object(module.requests, "post", return_value=bad_request) as post:
                    with contextlib.redirect_stdout(stdout):
                        with self.assertRaises(SystemExit) as raised:
                            module.main()

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(post.call_count, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["provider_error"]["provider"], "OpenAI")
        self.assertEqual(payload["provider_error"]["status_code"], 400)
        self.assertEqual(payload["provider_error"]["taxonomy"], "client_error")
        self.assertFalse(payload["provider_error"]["fallback_allowed"])
        self.assertEqual(len(payload["attempted_providers"]), 1)

    def test_main_default_gpt_image_pro_falls_back_to_standard_after_retryable_exhaustion(self):
        module = load_image_generation_module("ecorex_image_generation_retry_model_fallback_test")
        outage = FakeResponse(
            503,
            {
                "error": {
                    "message": "openai unavailable",
                    "code": "server_error",
                    "type": "server_error",
                }
            },
        )
        success = FakeResponse(200, {"data": [{"b64_json": "aGVsbG8="}]})
        responses = [outage, outage, success]
        sleeps = []
        payload_models = []
        stdout = io.StringIO()
        env = {
            "OPENAI_API_KEY": "sk-test",
            "LINKAI_API_KEY": "lk-test",
            "IMAGE_OUTPUT_DIR": tempfile.gettempdir(),
        }
        argv = ["generate.py", json.dumps({"prompt": "recover with fallback"})]

        def fake_post(_url, **kwargs):
            payload_models.append(kwargs["json"]["model"])
            return responses.pop(0)

        def record_sleep(value):
            if value == 2.0:
                sleeps.append(value)

        with patch.dict(os.environ, env, clear=False):
            with patch.object(sys, "argv", argv):
                with patch.object(module.time, "sleep", record_sleep):
                    with patch.object(module.requests, "post", side_effect=fake_post) as post:
                        with patch.object(module, "_save_image", return_value="fallback.png"):
                            with contextlib.redirect_stdout(stdout):
                                module.main()

        self.assertEqual(post.call_count, 3)
        self.assertEqual(payload_models, ["gpt-image-2-pro", "gpt-image-2-pro", "gpt-image-2"])
        self.assertEqual(sleeps, [2.0])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["model"], "gpt-image-2")
        self.assertEqual(payload["images"], [{"url": "fallback.png"}])
        self.assertEqual(payload["model_fallback"]["used"], True)
        self.assertEqual(payload["model_fallback"]["from_model"], "gpt-image-2-pro")
        self.assertEqual(payload["model_fallback"]["to_model"], "gpt-image-2")
        self.assertEqual(payload["model_fallback"]["reason"], "server_error")

    def test_main_default_gpt_image_pro_falls_back_when_model_not_found(self):
        module = load_image_generation_module("ecorex_image_generation_model_not_found_fallback_test")
        not_found = FakeResponse(
            404,
            {
                "error": {
                    "message": "model gpt-image-2-pro does not exist or is unavailable",
                    "code": "model_not_found",
                    "type": "invalid_request_error",
                }
            },
        )
        success = FakeResponse(200, {"data": [{"b64_json": "aGVsbG8="}]})
        responses = [not_found, success]
        payload_models = []
        stdout = io.StringIO()
        env = {
            "OPENAI_API_KEY": "sk-test",
            "IMAGE_OUTPUT_DIR": tempfile.gettempdir(),
        }
        argv = ["generate.py", json.dumps({"prompt": "recover from unavailable pro"})]

        def fake_post(_url, **kwargs):
            payload_models.append(kwargs["json"]["model"])
            return responses.pop(0)

        with patch.dict(os.environ, env, clear=False):
            with patch.object(sys, "argv", argv):
                with patch.object(module.requests, "post", side_effect=fake_post) as post:
                    with patch.object(module, "_save_image", return_value="fallback.png"):
                        with contextlib.redirect_stdout(stdout):
                            module.main()

        self.assertEqual(post.call_count, 2)
        self.assertEqual(payload_models, ["gpt-image-2-pro", "gpt-image-2"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["model"], "gpt-image-2")
        self.assertEqual(payload["model_fallback"]["reason"], "client_error")

    def test_openai_image_edit_uses_gpt_image_pro_then_standard_fallback(self):
        module = load_image_generation_module("ecorex_image_generation_edit_fallback_test")
        provider = module.OpenAIProvider("sk-test", "https://api.openai.test/v1", "gpt-image-2-pro")
        calls = []
        outage = module.ImageProviderError(
            "pro temporarily unavailable",
            provider="OpenAI",
            status_code=503,
            error_type="server_error",
            retry_attempt=1,
            max_retries=1,
        )
        success = {"data": [{"b64_json": "aGVsbG8="}]}

        def fake_post_multipart(url, fields, files):
            calls.append({
                "url": url,
                "fields": dict(fields),
                "files": list(files),
            })
            if len(calls) == 1:
                raise outage
            return success

        with tempfile.TemporaryDirectory() as output_dir:
            with patch.object(provider, "_post_multipart", side_effect=fake_post_multipart):
                with patch.object(module, "_load_image", return_value=b"fake-input-image"):
                    with patch.object(module, "_compress_image", return_value=b"\x89PNG\r\nfake"):
                        with patch.object(module, "_save_image", return_value=str(Path(output_dir) / "edited.png")):
                            paths = provider.generate(
                                "make the logo sharper",
                                image_url="input.png",
                                output_dir=output_dir,
                                output_format="png",
                            )

        self.assertEqual(paths, [str(Path(output_dir) / "edited.png")])
        self.assertEqual([call["url"] for call in calls], [
            "https://api.openai.test/v1/images/edits",
            "https://api.openai.test/v1/images/edits",
        ])
        self.assertEqual([call["fields"]["model"] for call in calls], ["gpt-image-2-pro", "gpt-image-2"])
        self.assertEqual([call["fields"]["prompt"] for call in calls], ["make the logo sharper"] * 2)
        self.assertEqual(calls[0]["files"][0][0], "image")
        self.assertEqual(provider.model, "gpt-image-2")
        self.assertEqual(provider.model_fallback["used"], True)
        self.assertEqual(provider.model_fallback["from_model"], "gpt-image-2-pro")
        self.assertEqual(provider.model_fallback["to_model"], "gpt-image-2")
        self.assertEqual(provider.model_fallback["reason"], "server_error")

    def test_build_providers_defaults_to_gpt_image_pro_and_linkai_only_when_openai_missing(self):
        module = load_image_generation_module("ecorex_image_generation_default_provider_test")

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test",
            "LINKAI_API_KEY": "lk-test",
            "GEMINI_API_KEY": "gemini-test",
        }, clear=True):
            providers = module._build_providers("gpt-image-2-pro")
            self.assertEqual([label for label, _ in providers], ["OpenAI"])
            self.assertEqual(providers[0][1].model, "gpt-image-2-pro")

        with patch.dict(os.environ, {
            "LINKAI_API_KEY": "lk-test",
            "GEMINI_API_KEY": "gemini-test",
        }, clear=True):
            providers = module._build_providers("gpt-image-2-pro")
            self.assertEqual([label for label, _ in providers], ["LinkAI"])
            self.assertEqual(providers[0][1].model, "gpt-image-2-pro")

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-test"}, clear=True):
            self.assertEqual(module._build_providers("gpt-image-2-pro"), [])

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test",
            "GEMINI_API_KEY": "gemini-test",
            "DASHSCOPE_API_KEY": "dashscope-test",
        }, clear=True):
            providers = module._build_providers("gpt-image-2-pro", provider_id="gemini")
            self.assertEqual([label for label, _ in providers], ["OpenAI"])
            self.assertEqual(providers[0][1].model, "gpt-image-2-pro")

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-test"}, clear=True):
            self.assertEqual(module._build_providers("gpt-image-2-pro", provider_id="gemini"), [])

    def test_qwen_body_error_is_typed_fail_closed(self):
        module = load_image_generation_module("ecorex_image_generation_qwen_body_test")
        provider = module.QwenProvider("dashscope-key", "https://dashscope.test", "qwen-image-2.0")
        response = FakeResponse(
            200,
            {
                "code": "InvalidParameter",
                "message": "bad qwen size",
            },
        )

        with patch.object(module.requests, "post", return_value=response):
            with self.assertRaises(module.ImageProviderError) as raised:
                provider.generate("draw")

        error = raised.exception
        self.assertEqual(error.provider, "Qwen")
        self.assertEqual(error.status_code, 400)
        self.assertEqual(error.code, "InvalidParameter")
        self.assertEqual(error.taxonomy, "client_error")
        self.assertFalse(error.retryable)

    def test_linkai_empty_success_response_is_typed_protocol_failure(self):
        module = load_image_generation_module("ecorex_image_generation_linkai_empty_test")
        provider = module.LinkAIProvider("linkai-key", "https://linkai.test", "gpt-image-2-pro")
        response = FakeResponse(200, {"data": []})

        with patch.object(module.requests, "post", return_value=response):
            with self.assertRaises(module.ImageProviderError) as raised:
                provider.generate("draw")

        error = raised.exception
        self.assertEqual(error.provider, "LinkAI")
        self.assertEqual(error.status_code, 502)
        self.assertEqual(error.code, "empty_response")
        self.assertEqual(error.error_type, "provider_protocol_error")
        self.assertEqual(error.taxonomy, "server_error")
        self.assertTrue(error.retryable)

    def test_seedream_and_minimax_body_errors_are_typed_fail_closed(self):
        module = load_image_generation_module("ecorex_image_generation_body_error_test")
        cases = [
            (
                module.SeedreamProvider("ark-key", "https://ark.test/api/v3", "seedream"),
                FakeResponse(
                    200,
                    {
                        "error": {
                            "code": "InvalidPrompt",
                            "message": "bad seedream prompt",
                        }
                    },
                ),
                "Seedream",
                "InvalidPrompt",
            ),
            (
                module.MinimaxProvider("minimax-key", "https://minimax.test", "image-01"),
                FakeResponse(
                    200,
                    {
                        "base_resp": {
                            "status_code": 1001,
                            "status_msg": "bad minimax prompt",
                        }
                    },
                ),
                "MiniMax",
                "1001",
            ),
        ]

        for provider, response, expected_provider, expected_code in cases:
            with self.subTest(provider=expected_provider):
                with patch.object(module.requests, "post", return_value=response):
                    with self.assertRaises(module.ImageProviderError) as raised:
                        provider.generate("draw")

                error = raised.exception
                self.assertEqual(error.provider, expected_provider)
                self.assertEqual(error.status_code, 400)
                self.assertEqual(error.code, expected_code)
                self.assertEqual(error.taxonomy, "client_error")
                self.assertFalse(error.retryable)

    def test_v022_image_generation_tool_invocation_smoke_contract(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts" / "smoke-image-generation-tool-invocation.py").read_text(encoding="utf-8")

        self.assertIn("skills/image-generation/scripts/generate.py", script)
        self.assertIn("[sys.executable, str(GENERATE), \"--stdin\"]", script)
        self.assertIn("FakeImageApiServer", script)
        self.assertIn("/images/generations", script)
        self.assertIn("/images/edits", script)
        self.assertIn("gpt-image-2-pro", script)
        self.assertIn("gpt-image-2", script)
        self.assertIn("model_fallback", script)
        self.assertIn("artifact", script)
        self.assertIn("OPENAI_API_BASE", script)
        self.assertIn("OPENAI_API_KEY", script)
        self.assertIn("LINKAI_API_KEY", script)
        self.assertIn("SKILL_IMAGE_GENERATION_PROVIDER", script)
        self.assertIn("SKILL_IMAGE_GENERATION_MODEL", script)
        self.assertIn("generation route calls mismatch", script)
        self.assertIn("edit route did not receive multipart image file", script)
        self.assertIn("fallback target mismatch", script)
        self.assertIn("leaked fake API key", script)


if __name__ == "__main__":
    unittest.main()
