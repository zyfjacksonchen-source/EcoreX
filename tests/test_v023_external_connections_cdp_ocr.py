import ast
import base64
import hashlib
import importlib.machinery
import io
import json
import re
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
from pathlib import Path


def install_web_stub() -> None:
    sys.modules.setdefault("web", types.SimpleNamespace(
        header=lambda *args, **kwargs: None,
        data=lambda: b"{}",
        input=lambda **kwargs: types.SimpleNamespace(**kwargs),
        cookies=lambda: {},
        ctx=types.SimpleNamespace(env={}, method="GET", status="200 OK"),
        notfound=Exception,
        application=lambda *args, **kwargs: None,
        httpserver=types.SimpleNamespace(LogMiddleware=types.SimpleNamespace(log=lambda *args, **kwargs: None)),
    ))


def python_function_literal_return(path: Path, function_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            for statement in node.body:
                if (
                    isinstance(statement, ast.Return)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                ):
                    return statement.value.value
    raise AssertionError(f"{function_name} literal return not found in {path.name}")


class V023CdpOcrExternalConnectionsTests(unittest.TestCase):
    def test_browser_defaults_match_the_cow_runtime(self):
        import config
        from agent.tools.browser.browser_service import BrowserService

        browser_defaults = config.available_setting["tools"]["browser"]
        self.assertEqual(browser_defaults, {})
        self.assertEqual(config.available_setting["mcp_servers"], [])
        self.assertEqual(BrowserService(browser_defaults)._cdp_endpoint, "")

    def retired_legacy_browser_executable_discovery_does_not_return_missing_linux_command(
        self,
    ):
        from agent.tools.browser import browser_automation_service as service

        with patch.object(service.sys, "platform", "linux"), patch.object(service.shutil, "which", return_value=None):
            self.assertEqual(service.find_chrome_executable({}), "")

        with patch.object(service.sys, "platform", "linux"), patch.object(service.shutil, "which", return_value="/usr/bin/chromium"):
            self.assertEqual(service.find_chrome_executable({}), "/usr/bin/chromium")

    def retired_legacy_cdp_screenshot_uses_native_capture_fallback(self):
        source = Path("agent/tools/browser/browser_service.py").read_text(encoding="utf-8")

        self.assertIn('page.screenshot(path=filepath, full_page=full_page, animations="disabled")', source)
        self.assertIn('if self._launch_mode != "cdp":', source)
        self.assertIn("self._capture_screenshot_via_cdp(page, filepath, full_page=full_page)", source)
        self.assertIn('session.send("Page.captureScreenshot", params)', source)
        self.assertIn("base64.b64decode(image_data)", source)

    def retired_legacy_cdp_auto_launch_timeout_cleans_up_spawned_process(self):
        from agent.tools.browser import browser_automation_service as service

        class FakeProcess:
            def __init__(self):
                self.terminated = False
                self.killed = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                raise TimeoutError("still running")

            def kill(self):
                self.killed = True

        fake = FakeProcess()
        with (
            patch.object(service, "cdp_is_reachable", return_value=False),
            patch.object(service, "launch_cdp_browser", return_value=fake),
            patch.object(service.time, "time", side_effect=[0, 2]),
            patch.object(service.time, "sleep", return_value=None),
        ):
            with self.assertRaises(RuntimeError):
                service.ensure_cdp_browser({}, "http://127.0.0.1:9", timeout_seconds=0.01)

        self.assertTrue(fake.terminated)
        self.assertTrue(fake.killed)

    def retired_legacy_cdp_fallback_cleans_auto_launched_process_before_switching_launch_mode(
        self,
    ):
        source = Path("agent/tools/browser/browser_service.py").read_text(encoding="utf-8")

        fallback_idx = source.index('fallback_launch_mode = "persistent" if self._user_data_dir else "fresh"')
        shutdown_idx = source.index("self._shutdown_browser(force_cdp_process_cleanup=True)", fallback_idx)
        switch_idx = source.index("self._launch_mode = fallback_launch_mode", shutdown_idx)
        self.assertLess(fallback_idx, shutdown_idx)
        self.assertLess(shutdown_idx, switch_idx)

    def retired_legacy_cdp_session_persists_across_idle_and_reconnects_once(self):
        source = Path("agent/tools/browser/browser_service.py").read_text(encoding="utf-8")

        self.assertIn('self._cdp_persist_session: bool = self._config.get("cdp_persist_session", True) is not False', source)
        self.assertIn('self._idle_timeout = 0.0', source)
        self.assertIn("def _maybe_cdp_keepalive(self) -> None:", source)
        self.assertIn('self._page.evaluate("() => document.readyState")', source)
        self.assertIn("for attempt in range(2):", source)
        self.assertIn("CDP action hit stale connection; reconnecting once", source)

    def retired_legacy_cdp_persistent_shutdown_keeps_auto_launched_browser_unless_forced(
        self,
    ):
        from agent.tools.browser.browser_service import BrowserService

        class FakeProcess:
            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        service = BrowserService({"cdp_endpoint": "http://127.0.0.1:9222", "cdp_persist_session": True})
        first = FakeProcess()
        service._cdp_process = first
        service._shutdown_browser()
        self.assertFalse(first.terminated)
        self.assertIsNone(service._cdp_process)

        second = FakeProcess()
        service._cdp_process = second
        service._shutdown_browser(force_cdp_process_cleanup=True)
        self.assertTrue(second.terminated)

    def retired_legacy_chrome_devtools_mcp_defaults_use_full_cdp_profile(self):
        import config

        expected_args = config.chrome_devtools_mcp_args()
        self.assertIn("-y", expected_args)
        self.assertIn("--browserUrl", expected_args)
        self.assertIn("--no-usage-statistics", expected_args)
        self.assertIn("--no-performance-crux", expected_args)
        self.assertIn("--experimentalPageIdRouting", expected_args)
        self.assertIn("--experimentalVision", expected_args)
        self.assertIn("--memoryDebugging", expected_args)
        self.assertIn("--categoryExperimentalThirdParty", expected_args)
        self.assertIn("--categoryExperimentalWebmcp", expected_args)
        self.assertIn("--redactNetworkHeaders", expected_args)

        server = config.available_setting["mcp_servers"][0]
        self.assertEqual(server["name"], "chrome-devtools")
        self.assertEqual(server["args"], expected_args)
        self.assertGreaterEqual(server["timeout"], 45)

        template = json.loads(Path("config-template.json").read_text(encoding="utf-8"))
        template_server = template["mcp_servers"][0]
        self.assertEqual(template_server["args"], expected_args)
        self.assertGreaterEqual(template_server["timeout"], 45)

    def retired_legacy_chrome_devtools_mcp_defaults_are_synced_across_runtime_files(
        self,
    ):
        import config

        expected_args = config.chrome_devtools_mcp_args()
        template = json.loads(Path("config-template.json").read_text(encoding="utf-8"))
        runtime_config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        sidecar = Path("desktop/electron/sidecar.ts").read_text(encoding="utf-8")

        self.assertEqual(config.available_setting["mcp_servers"][0]["args"], expected_args)
        self.assertEqual(template["mcp_servers"][0]["args"], expected_args)
        self.assertEqual(runtime_config["mcp_servers"][0]["args"], expected_args)
        self.assertFalse(config.available_setting["mcp_auto_start"])
        self.assertFalse(template["mcp_auto_start"])
        self.assertFalse(runtime_config["mcp_auto_start"])

        match = re.search(
            r"const chromeDevtoolsMcpArgs = \(endpoint = cdpEndpoint\) => \[(?P<body>.*?)\];",
            sidecar,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        sidecar_args = []
        for raw in re.findall(r'"([^"]+)"|endpoint', match.group("body")):
            sidecar_args.append(raw or config.DEFAULT_CDP_ENDPOINT)
        self.assertEqual(sidecar_args, expected_args)
        self.assertIn("mcp_auto_start: false", sidecar)

    def retired_legacy_chrome_devtools_mcp_client_trust_classifier_requires_privacy_flags(
        self,
    ):
        from agent.tools.mcp.mcp_client import McpClient, _is_default_chrome_devtools_config
        import config

        expected_args = config.chrome_devtools_mcp_args()
        self.assertTrue(_is_default_chrome_devtools_config("chrome-devtools", "npx", expected_args))
        self.assertFalse(_is_default_chrome_devtools_config(
            "chrome-devtools",
            "npx",
            [item for item in expected_args if item != "--redactNetworkHeaders"],
        ))
        self.assertFalse(_is_default_chrome_devtools_config(
            "chrome-devtools",
            "npx",
            config.chrome_devtools_mcp_args("http://192.168.1.10:9222"),
        ))

        captured = {}

        class FakeBroker:
            def authorize_noninteractive(self, tool_name, arguments=None):
                captured["tool"] = tool_name
                captured["arguments"] = dict(arguments or {})
                return {"allowed": True, "reason": "test"}

        with patch(
            "common.ecorex_tool_permissions.get_tool_permission_broker",
            return_value=FakeBroker(),
        ):
            allowed = McpClient({"name": "chrome-devtools", "type": "stdio"})._authorize_stdio_start(
                "npx",
                expected_args,
            )

        self.assertTrue(allowed)
        self.assertEqual(captured["tool"], "browser")
        self.assertTrue(captured["arguments"]["trusted_default_chrome_devtools"])
        self.assertEqual(captured["arguments"]["args"], expected_args)

    def retired_legacy_chrome_devtools_optional_ability_upgrades_to_full_toolset(
        self,
    ):
        from agent.tools.optional_abilities.optional_abilities import (
            OptionalAbilities,
            _ability_defs,
            _chrome_devtools_mcp_is_full,
            _ensure_chrome_devtools_mcp,
        )
        import config

        runtime_config = {
            "mcp_auto_start": False,
            "tools": {"browser": {"cdp_endpoint": config.DEFAULT_CDP_ENDPOINT}},
            "mcp_servers": [{
                "name": "chrome-devtools",
                "type": "stdio",
                "command": "npx",
                "args": [
                    "chrome-devtools-mcp@latest",
                    "--browserUrl",
                    config.DEFAULT_CDP_ENDPOINT,
                    "--no-usage-statistics",
                ],
                "timeout": 30,
            }],
        }

        self.assertFalse(_chrome_devtools_mcp_is_full(runtime_config))
        _ensure_chrome_devtools_mcp(runtime_config)
        self.assertTrue(_chrome_devtools_mcp_is_full(runtime_config))
        self.assertEqual(runtime_config["mcp_servers"][0]["args"], config.chrome_devtools_mcp_args())
        self.assertGreaterEqual(runtime_config["mcp_servers"][0]["timeout"], 45)

        status = OptionalAbilities()._status_for(
            "chrome-devtools-mcp",
            _ability_defs()["chrome-devtools-mcp"],
            runtime_config,
        )
        self.assertTrue(status["configured"])
        self.assertTrue(status["fullToolset"])

    def retired_legacy_chrome_devtools_mcp_skills_are_bundled(self):
        root = Path("skills")
        for name in [
            "a11y-debugging",
            "chrome-devtools",
            "chrome-devtools-cli",
            "debug-optimize-lcp",
            "memory-leak-debugging",
            "troubleshooting",
        ]:
            self.assertTrue((root / name / "SKILL.md").exists(), name)

    def test_ocr_extract_urls_returns_browser_handoff(self):
        from agent.tools.ocr.ocr import OcrTool

        result = OcrTool().execute({
            "action": "extract_urls",
            "text": "请用 CDP 读取 http://xhslink.com/o/8IkhCq7byEL 这篇笔记链接",
        })

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result["urls"], ["http://xhslink.com/o/8IkhCq7byEL"])
        self.assertEqual(result.result["nextAction"]["tool"], "browser")
        self.assertEqual(result.result["nextAction"]["action"], "navigate")

    def test_ocr_extract_urls_normalizes_bare_domain_for_browser_handoff(self):
        from agent.tools.ocr.ocr import OcrTool

        result = OcrTool().execute({
            "action": "extract_urls",
            "text": "截图里写着 example.com/ecorex-4827，请打开它。",
        })

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result["urls"], ["https://example.com/ecorex-4827"])
        self.assertEqual(result.result["nextAction"], {
            "tool": "browser",
            "action": "navigate",
            "url": "https://example.com/ecorex-4827",
        })

    def test_ocr_extract_urls_repairs_ocr_misread_scheme_separator(self):
        from agent.tools.ocr.ocr import OcrTool

        result = OcrTool().execute({
            "action": "extract_urls",
            "text": "URL https/example.com/ecorex-4827",
        })

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result["urls"], ["https://example.com/ecorex-4827"])
        self.assertEqual(result.result["nextAction"]["url"], "https://example.com/ecorex-4827")

    def test_ocr_extract_urls_uses_rapidocr_provider_for_image_handoff(self):
        from PIL import Image
        from agent.tools.ocr.ocr import OcrTool, _CACHE, _RAPIDOCR_ENGINES

        class FakeRapidOCR:
            def __call__(self, image_path):
                return [([0, 0, 1, 1], "http://xhslink.com/o/8IkhCq7byEL", 0.99)]

        module = types.ModuleType("rapidocr_onnxruntime")
        module.__spec__ = importlib.machinery.ModuleSpec("rapidocr_onnxruntime", loader=None)
        module.RapidOCR = FakeRapidOCR
        previous = sys.modules.get("rapidocr_onnxruntime")
        sys.modules["rapidocr_onnxruntime"] = module
        _CACHE.clear()
        _RAPIDOCR_ENGINES.clear()
        try:
            buffer = io.BytesIO()
            Image.new("RGB", (320, 120), "white").save(buffer, format="PNG")
            image = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
            result = OcrTool().execute({"action": "extract_urls", "image": image, "timeout": 2})
        finally:
            _CACHE.clear()
            _RAPIDOCR_ENGINES.clear()
            if previous is None:
                sys.modules.pop("rapidocr_onnxruntime", None)
            else:
                sys.modules["rapidocr_onnxruntime"] = previous

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result["urls"], ["http://xhslink.com/o/8IkhCq7byEL"])
        self.assertEqual(result.result["ocr"]["provider"], "rapidocr_onnxruntime")
        self.assertEqual(result.result["nextAction"]["tool"], "browser")
        self.assertEqual(result.result["nextAction"]["action"], "navigate")

    def test_ocr_error_metadata_and_logs_redact_local_path_and_token_shaped_names(self):
        from agent.tools.ocr.ocr import OcrTool

        dangerous = r"C:\Users\alice\private-ocr-source-sk-ocr-secret-123456.png"
        with self.assertLogs("log", level="WARNING") as logs:
            result = OcrTool().execute({"action": "extract_urls", "image": dangerous})

        self.assertEqual(result.status, "error")
        ocr = result.result["ocr"]
        self.assertEqual(ocr["status"], "error")
        self.assertNotIn("error", ocr)
        self.assertTrue(ocr["errorSummary"]["redacted"])
        serialized = json.dumps(result.result, ensure_ascii=False) + "\n" + "\n".join(logs.output)
        self.assertNotIn(dangerous, serialized)
        self.assertNotIn("C:\\Users\\alice", serialized)
        self.assertNotIn("private-ocr-source", serialized)
        self.assertNotIn("sk-ocr-secret", serialized)

    def test_fast_ocr_provider_is_declared_for_runtime_packaging(self):
        root = Path(__file__).resolve().parents[1]
        requirements = (root / "requirements.txt").read_text(encoding="utf-8")
        core_requirements = (root / "runtime-packs" / "core-requirements.txt").read_text(encoding="utf-8")
        capabilities = json.loads((root / "runtime-packs" / "capabilities.json").read_text(encoding="utf-8"))
        packs = {str(item.get("id")): item for item in capabilities.get("packs") or []}

        self.assertIn("rapidocr-onnxruntime", requirements)
        self.assertIn("rapidocr-onnxruntime", core_requirements)
        self.assertIn("fast-ocr", packs)
        self.assertIn("rapidocr-onnxruntime", packs["fast-ocr"].get("requirements") or [])
        self.assertIn("rapidocr_onnxruntime", packs["fast-ocr"].get("moduleChecks") or [])

    def test_external_connection_projection_uses_channel_state_without_second_source(self):
        install_web_stub()
        from channel.web.web_channel import _external_connection_from_channel

        connection = _external_connection_from_channel({
            "id": "feishu",
            "type": "feishu",
            "name": "Feishu/Lark",
            "configured": True,
            "connected": False,
            "enabled": True,
            "callable": True,
            "status": "auth_required",
            "homeChannel": {"id": "oc_123", "name": "Ops"},
            "configSchema": {
                "fields": [
                    {"name": "app_secret", "secret": True, "value": "****"},
                    {"name": "allow_all_users", "type": "boolean", "value": False},
                ],
            },
        })

        self.assertEqual(connection["id"], "feishu")
        self.assertEqual(connection["platform"], "feishu")
        self.assertEqual(connection["logo"]["key"], "feishu")
        self.assertTrue(connection["configured"])
        self.assertTrue(connection["enabled"])
        self.assertFalse(connection["connected"])
        self.assertTrue(connection["homeChannel"]["configured"])
        self.assertTrue(str(connection["homeChannel"]["idHash"]).startswith("hmac:"))
        self.assertNotIn("id", connection["homeChannel"])
        self.assertEqual(connection["configSchema"]["fields"][0]["value"], "****")
        action_ids = {item["id"] for item in connection["actions"]}
        self.assertIn("save_config", action_ids)
        self.assertIn("set_home_channel", action_ids)

    def test_external_connection_short_masked_secret_is_not_treated_as_new_value(self):
        install_web_stub()
        from channel.web.web_channel import ChannelsHandler

        self.assertTrue(ChannelsHandler._is_masked_secret_value("***"))
        self.assertTrue(ChannelsHandler._is_masked_secret_value("****"))
        self.assertTrue(ChannelsHandler._is_masked_secret_value("abcd****wxyz"))
        self.assertFalse(ChannelsHandler._is_masked_secret_value("real-secret-value"))

    def test_external_connection_save_preserves_masked_secret_and_existing_fields(self):
        install_web_stub()
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as raw_workspace:
            config_path = Path(raw_workspace) / "config.json"
            config_path.write_text(json.dumps({
                "channel_type": "web",
                "feishu_app_id": "old-app",
                "feishu_app_secret": "short",
                "unrelated": "kept",
            }), encoding="utf-8")
            fake_conf = {
                "channel_type": "web",
                "feishu_app_id": "old-app",
                "feishu_app_secret": "short",
                "unrelated": "kept",
            }
            with (
                patch.object(web_channel, "conf", return_value=fake_conf),
                patch.object(web_channel.ChannelsHandler, "_config_path", return_value=str(config_path)),
                patch.object(web_channel.ChannelsHandler, "_refresh_runtime_capabilities", return_value=None),
            ):
                payload = json.loads(web_channel.ChannelsHandler()._handle_save("feishu", {
                    "feishu_app_id": "new-app",
                    "feishu_app_secret": "*****",
                }))
                saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "success")
        self.assertEqual(saved["feishu_app_id"], "new-app")
        self.assertEqual(saved["feishu_app_secret"], "short")
        self.assertEqual(saved["unrelated"], "kept")
        self.assertEqual(fake_conf["feishu_app_id"], "new-app")
        self.assertEqual(fake_conf["feishu_app_secret"], "short")

    def test_external_connection_invalid_number_does_not_mutate_config(self):
        install_web_stub()
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as raw_workspace:
            config_path = Path(raw_workspace) / "config.json"
            initial = {"wechatmp_port": 8080, "unrelated": "kept"}
            config_path.write_text(json.dumps(initial), encoding="utf-8")
            fake_conf = dict(initial)
            with (
                patch.object(web_channel, "conf", return_value=fake_conf),
                patch.object(web_channel.ChannelsHandler, "_config_path", return_value=str(config_path)),
            ):
                payload = json.loads(web_channel.ChannelsHandler()._handle_save("wechatmp", {
                    "wechatmp_port": "not-a-number",
                }))
                connect_payload = json.loads(web_channel.ChannelsHandler()._handle_connect("wechatmp", {
                    "wechatmp_port": "not-a-number",
                }))
                saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "error")
        self.assertIn("wechatmp_port must be a number", payload["message"])
        self.assertEqual(connect_payload["status"], "error")
        self.assertIn("wechatmp_port must be a number", connect_payload["message"])
        self.assertEqual(saved, initial)
        self.assertEqual(fake_conf, initial)

    def test_external_connection_save_write_failure_does_not_mutate_memory_config(self):
        install_web_stub()
        from channel.web import web_channel

        fake_conf = {"feishu_app_id": "old-app", "feishu_app_secret": "old-secret"}
        with (
            patch.object(web_channel, "conf", return_value=fake_conf),
            patch.object(web_channel.ChannelsHandler, "_read_file_config", return_value=dict(fake_conf)),
            patch.object(web_channel.ChannelsHandler, "_write_file_config_atomic", side_effect=OSError("disk full")),
        ):
            with self.assertRaises(OSError):
                web_channel.ChannelsHandler()._handle_save("feishu", {"feishu_app_id": "new-app"})

        self.assertEqual(fake_conf["feishu_app_id"], "old-app")
        self.assertEqual(fake_conf["feishu_app_secret"], "old-secret")

    def test_external_connection_start_write_failure_does_not_mutate_memory_config(self):
        install_web_stub()
        from channel.web import web_channel

        fake_conf = {"channel_type": "web"}
        with (
            patch.object(web_channel, "conf", return_value=fake_conf),
            patch.object(web_channel.ChannelsHandler, "_read_file_config", return_value={
                "channel_type": "web,slack",
                "feishu_app_id": "cli-existing",
                "feishu_app_secret": "existing-secret",
            }),
            patch.object(web_channel.ChannelsHandler, "_write_file_config_atomic", side_effect=OSError("disk full")),
        ):
            with self.assertRaises(OSError):
                web_channel.ChannelsHandler()._handle_connect("feishu", {})

        self.assertEqual(fake_conf["channel_type"], "web")
        self.assertNotIn("feishu_event_mode", fake_conf)

    def test_external_connection_stop_write_failure_does_not_mutate_memory_config(self):
        install_web_stub()
        from channel.web import web_channel

        fake_conf = {"channel_type": "web,feishu"}
        with (
            patch.object(web_channel, "conf", return_value=fake_conf),
            patch.object(web_channel.ChannelsHandler, "_read_file_config", return_value={"channel_type": "web,slack,feishu"}),
            patch.object(web_channel.ChannelsHandler, "_write_file_config_atomic", side_effect=OSError("disk full")),
        ):
            with self.assertRaises(OSError):
                web_channel.ChannelsHandler()._handle_disconnect("feishu")

        self.assertEqual(fake_conf["channel_type"], "web,feishu")

    def test_external_connection_home_channel_write_failure_does_not_mutate_memory_config(self):
        install_web_stub()
        from channel.web import web_channel

        fake_conf = {"feishu_home_channel": "old-home", "feishu_home_channel_name": "Old"}
        with (
            patch.object(web_channel, "conf", return_value=fake_conf),
            patch.object(web_channel.ChannelsHandler, "_read_file_config", return_value=dict(fake_conf)),
            patch.object(web_channel.ChannelsHandler, "_write_file_config_atomic", side_effect=OSError("disk full")),
        ):
            with self.assertRaises(OSError):
                web_channel.ExternalConnectionActionHandler._handle_home_channel("feishu", "set_home_channel", {
                    "homeChannel": "new-home",
                    "homeChannelName": "New",
                })
            with self.assertRaises(OSError):
                web_channel.ExternalConnectionActionHandler._handle_home_channel("feishu", "clear_home_channel", {})

        self.assertEqual(fake_conf["feishu_home_channel"], "old-home")
        self.assertEqual(fake_conf["feishu_home_channel_name"], "Old")

    def test_external_connection_start_writes_channel_type_once_without_starting_real_thread(self):
        install_web_stub()
        from channel.web import web_channel

        class DummyThread:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

            def start(self):
                return None

        with tempfile.TemporaryDirectory() as raw_workspace:
            config_path = Path(raw_workspace) / "config.json"
            config_path.write_text(json.dumps({
                "channel_type": "web",
                "feishu_app_secret": "short",
            }), encoding="utf-8")
            fake_conf = {"channel_type": "web", "feishu_app_secret": "short"}
            with (
                patch.object(web_channel, "conf", return_value=fake_conf),
                patch.object(web_channel.ChannelsHandler, "_config_path", return_value=str(config_path)),
                patch.object(web_channel.threading, "Thread", DummyThread),
            ):
                first = json.loads(web_channel.ChannelsHandler()._handle_connect("feishu", {
                    "feishu_app_id": "cli-new",
                    "feishu_app_secret": "*****",
                }))
                second = json.loads(web_channel.ChannelsHandler()._handle_connect("feishu", {}))
                saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "success")
        self.assertEqual(saved["channel_type"], "web,feishu")
        self.assertEqual(saved["feishu_app_id"], "cli-new")
        self.assertEqual(saved["feishu_app_secret"], "short")
        self.assertEqual(saved["feishu_event_mode"], "websocket")

    def test_external_connection_start_ignores_non_dict_config(self):
        install_web_stub()
        from channel.web import web_channel

        class DummyThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                return None

        with tempfile.TemporaryDirectory() as raw_workspace:
            config_path = Path(raw_workspace) / "config.json"
            config_path.write_text(json.dumps({
                "channel_type": "web",
                "feishu_app_id": "cli-existing",
                "feishu_app_secret": "existing-secret",
            }), encoding="utf-8")
            fake_conf = {"channel_type": "web"}
            with (
                patch.object(web_channel, "conf", return_value=fake_conf),
                patch.object(web_channel.ChannelsHandler, "_config_path", return_value=str(config_path)),
                patch.object(web_channel.threading, "Thread", DummyThread),
            ):
                payload = json.loads(web_channel.ChannelsHandler()._handle_connect("feishu", "not-a-dict"))
                saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "success")
        self.assertEqual(saved["channel_type"], "web,feishu")
        self.assertEqual(saved["feishu_event_mode"], "websocket")

    def test_external_connection_start_requires_existing_or_submitted_credentials(self):
        install_web_stub()
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as raw_workspace:
            config_path = Path(raw_workspace) / "config.json"
            config_path.write_text(json.dumps({"channel_type": "web"}), encoding="utf-8")
            fake_conf = {"channel_type": "web"}
            with (
                patch.object(web_channel, "conf", return_value=fake_conf),
                patch.object(web_channel.ChannelsHandler, "_config_path", return_value=str(config_path)),
            ):
                payload = json.loads(web_channel.ChannelsHandler()._handle_connect("feishu", {}))
                saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "error")
        self.assertIn("missing required config fields", payload["message"])
        self.assertIn("feishu_app_id", payload["missingFields"])
        self.assertIn("feishu_app_secret", payload["missingFields"])
        self.assertEqual(saved["channel_type"], "web")
        self.assertEqual(fake_conf["channel_type"], "web")

    def test_external_connection_home_channel_action_writes_and_clears_atomically(self):
        install_web_stub()
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as raw_workspace:
            config_path = Path(raw_workspace) / "config.json"
            config_path.write_text(json.dumps({"unrelated": "kept"}), encoding="utf-8")
            fake_conf = {"unrelated": "kept"}
            with (
                patch.object(web_channel, "conf", return_value=fake_conf),
                patch.object(web_channel.ChannelsHandler, "_config_path", return_value=str(config_path)),
            ):
                set_payload = json.loads(web_channel.ExternalConnectionActionHandler._handle_home_channel("feishu", "set_home_channel", {
                    "homeChannel": "oc_home",
                    "homeChannelName": "Ops",
                }))
                after_set = json.loads(config_path.read_text(encoding="utf-8"))
                clear_payload = json.loads(web_channel.ExternalConnectionActionHandler._handle_home_channel("feishu", "clear_home_channel", {}))
                after_clear = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(set_payload["status"], "success")
        self.assertTrue(set_payload["homeChannelConfigured"])
        self.assertEqual(after_set["feishu_home_channel"], "oc_home")
        self.assertEqual(after_set["feishu_home_channel_name"], "Ops")
        self.assertEqual(fake_conf.get("feishu_home_channel"), None)
        self.assertEqual(clear_payload["status"], "success")
        self.assertFalse(clear_payload["homeChannelConfigured"])
        self.assertNotIn("feishu_home_channel", after_clear)
        self.assertEqual(after_clear["unrelated"], "kept")

    def test_external_connection_home_channel_projects_from_channel_state(self):
        install_web_stub()
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as raw_workspace:
            config_path = Path(raw_workspace) / "config.json"
            config_path.write_text(json.dumps({"channel_type": "web"}), encoding="utf-8")
            fake_conf = {"channel_type": "web"}
            with (
                patch.object(web_channel, "_require_auth", return_value=None),
                patch.object(web_channel, "conf", return_value=fake_conf),
                patch.object(web_channel.ChannelsHandler, "_config_path", return_value=str(config_path)),
            ):
                set_payload = json.loads(web_channel.ExternalConnectionActionHandler._handle_home_channel("feishu", "set_home_channel", {
                    "homeChannel": "oc_home",
                    "homeChannelName": "Ops",
                }))
                projection = json.loads(web_channel.ExternalConnectionsHandler().GET())
                clear_payload = json.loads(web_channel.ExternalConnectionActionHandler._handle_home_channel("feishu", "clear_home_channel", {}))
                projection_after_clear = json.loads(web_channel.ExternalConnectionsHandler().GET())

        self.assertEqual(set_payload["status"], "success")
        feishu = next(item for item in projection["connections"] if item["id"] == "feishu")
        self.assertTrue(feishu["homeChannel"]["configured"])
        self.assertTrue(str(feishu["homeChannel"]["idHash"]).startswith("hmac:"))
        self.assertEqual(feishu["homeChannel"]["name"], "Ops")
        self.assertNotIn("id", feishu["homeChannel"])
        self.assertEqual(clear_payload["status"], "success")
        feishu_after_clear = next(item for item in projection_after_clear["connections"] if item["id"] == "feishu")
        self.assertEqual(feishu_after_clear["homeChannel"], {})

    def test_external_connection_start_stop_merge_channel_type_from_file_config(self):
        install_web_stub()
        from channel.web import web_channel

        class DummyThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                return None

        with tempfile.TemporaryDirectory() as raw_workspace:
            config_path = Path(raw_workspace) / "config.json"
            config_path.write_text(json.dumps({
                "channel_type": "web,slack",
                "feishu_app_id": "cli-existing",
                "feishu_app_secret": "existing-secret",
            }), encoding="utf-8")
            fake_conf = {"channel_type": "web"}
            with (
                patch.object(web_channel, "conf", return_value=fake_conf),
                patch.object(web_channel.ChannelsHandler, "_config_path", return_value=str(config_path)),
                patch.object(web_channel.threading, "Thread", DummyThread),
            ):
                start_payload = json.loads(web_channel.ChannelsHandler()._handle_connect("feishu", {}))
                after_start = json.loads(config_path.read_text(encoding="utf-8"))
                memory_after_start = fake_conf["channel_type"]
                stop_payload = json.loads(web_channel.ChannelsHandler()._handle_disconnect("feishu"))
                after_stop = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(start_payload["status"], "success")
        self.assertEqual(after_start["channel_type"], "web,slack,feishu")
        self.assertEqual(memory_after_start, "web,slack,feishu")
        self.assertEqual(stop_payload["status"], "success")
        self.assertEqual(after_stop["channel_type"], "web,slack")
        self.assertEqual(fake_conf["channel_type"], "web,slack")

    def test_external_connection_runtime_errors_are_redacted_in_projection(self):
        install_web_stub()
        from channel.web import web_channel

        class FakeChannel:
            _startup_error = (
                "provider failed token=secret-token sk-runtime-secret-123456 "
                "xoxb-slacksecret123456 xapp-appsecret123456 123456789:ABCDEFGHIJKLMNOPQRST_uv"
            )
            _startup_event = None

        observed = web_channel.ChannelsHandler._channel_startup_observation(FakeChannel())
        connection = web_channel._external_connection_from_channel({
            "name": "feishu",
            "label": {"zh": "飞书"},
            "running": False,
            "last_error": "api_key=sk-raw-error-123456 xoxb-rawslack123456 123456789:ABCDEFGHIJKLMNOPQRST_uv",
            "auth": {},
            "agentSurface": {},
            "fields": [],
        })

        serialized = json.dumps({"observed": observed, "connection": connection}, ensure_ascii=False)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("sk-runtime-secret", serialized)
        self.assertNotIn("sk-raw-error", serialized)
        self.assertNotIn("xoxb-slacksecret", serialized)
        self.assertNotIn("xapp-appsecret", serialized)
        self.assertNotIn("xoxb-rawslack", serialized)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRST_uv", serialized)
        self.assertNotIn("should-not-render", web_channel.ChannelsHandler._redact_runtime_error("authorization=Bearer should-not-render"))
        self.assertIn("***", serialized)

    def test_messaging_adapter_contract_normalizes_receive_send_and_dedupes(self):
        from bridge.context import Context, ContextType
        from bridge.reply import Reply, ReplyType
        from channel.messaging_adapter_contract import (
            MessageIngressGate,
            deliver_reply,
            normalize_inbound_context,
            produce_context_once,
        )

        class FakeMessage:
            msg_id = "platform-msg-1"
            create_time = 123
            ctype = ContextType.TEXT
            content = "hello xoxb-secret-token-123456"
            from_user_id = "user-1"
            to_user_id = "bot-1"
            other_user_id = "thread-1"
            is_group = True
            is_at = True
            actual_user_id = "user-1"

        class FakeChannel:
            def __init__(self):
                self.produced = []
                self.sent = []

            def produce(self, context):
                self.produced.append(context)

            def send(self, reply, context):
                self.sent.append((reply, context))

        context = Context(ContextType.TEXT, "open this xoxb-secret-token-123456")
        context.kwargs = {
            "channel_type": "slack",
            "session_id": "thread-1",
            "receiver": "thread-1",
            "isgroup": True,
            "msg": FakeMessage(),
        }
        gate = MessageIngressGate(ttl_seconds=60)
        channel = FakeChannel()

        inbound = normalize_inbound_context(context)
        first = produce_context_once(channel, context, gate=gate)
        second = produce_context_once(channel, context, gate=gate)
        delivery = deliver_reply(channel, Reply(ReplyType.TEXT, "ok"), context)

        serialized = json.dumps({"inbound": inbound, "first": first, "second": second, "delivery": delivery})
        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(channel.produced), 1)
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(inbound["direction"], "inbound")
        self.assertEqual(inbound["platform"], "slack")
        self.assertEqual(inbound["contextType"], "TEXT")
        self.assertEqual(inbound["sessionId"], "thread-1")
        self.assertEqual(inbound["receiver"], "thread-1")
        self.assertTrue(inbound["isGroup"])
        self.assertEqual(inbound["contentPreview"], "[redacted-content]")
        self.assertTrue(inbound["contentHash"])
        self.assertEqual(inbound["contentLength"], len("open this xoxb-secret-token-123456"))
        self.assertGreater(inbound["contentBytes"], 0)
        self.assertEqual(inbound["message"]["messageId"], "platform-msg-1")
        self.assertEqual(inbound["message"]["type"], "TEXT")
        self.assertTrue(inbound["message"]["isGroup"])
        self.assertTrue(inbound["message"]["isAt"])
        self.assertEqual(inbound["message"]["contentPreview"], "[redacted-content]")
        self.assertTrue(inbound["message"]["contentHash"])
        self.assertEqual(inbound["queue"]["entrypoint"], "ChatChannel.produce")
        self.assertEqual(delivery["delivery"]["direction"], "outbound")
        self.assertEqual(delivery["delivery"]["platform"], "slack")
        self.assertEqual(delivery["delivery"]["replyType"], "TEXT")
        self.assertNotIn("sessionId", delivery["delivery"])
        self.assertNotIn("receiver", delivery["delivery"])
        self.assertTrue(delivery["delivery"]["sessionHash"])
        self.assertTrue(delivery["delivery"]["receiverHash"])
        self.assertEqual(delivery["delivery"]["sessionSummary"]["chars"], len("thread-1"))
        self.assertEqual(delivery["delivery"]["receiverSummary"]["chars"], len("thread-1"))
        self.assertEqual(delivery["delivery"]["entrypoint"], "Channel.send")
        self.assertEqual(delivery["delivery"]["contentPreview"], "[redacted-content]")
        self.assertTrue(delivery["delivery"]["contentHash"])
        self.assertNotIn("thread-1", json.dumps(delivery, ensure_ascii=False))
        self.assertFalse(first["inbound"]["queue"]["usesHermesActiveSessionQueue"])
        self.assertNotIn("xoxb-secret-token", serialized)
        self.assertNotIn("open this", serialized)
        self.assertNotIn("hello xoxb", serialized)

    def test_messaging_adapter_ingress_failure_releases_dedupe_for_retry(self):
        from bridge.context import Context, ContextType
        from channel.messaging_adapter_contract import MessageIngressGate, produce_context_once

        class FakeMessage:
            msg_id = "retry-msg-1"
            ctype = ContextType.TEXT
            content = "hello"
            from_user_id = "user-1"
            other_user_id = "thread-1"
            is_group = False

        class FlakyChannel:
            def __init__(self):
                self.calls = 0

            def produce(self, _context):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary adapter failure")

        context = Context(ContextType.TEXT, "hello")
        context.kwargs = {
            "channel_type": "telegram",
            "session_id": "thread-1",
            "receiver": "thread-1",
            "msg": FakeMessage(),
        }
        gate = MessageIngressGate(ttl_seconds=60)
        channel = FlakyChannel()

        with self.assertRaises(RuntimeError):
            produce_context_once(channel, context, gate=gate)
        retry = produce_context_once(channel, context, gate=gate)

        self.assertEqual(channel.calls, 2)
        self.assertEqual(retry["status"], "queued")

    def test_chat_channel_produce_applies_adapter_dedupe_without_second_queue(self):
        from bridge.context import Context, ContextType
        from channel.chat_channel import ChatChannel
        from channel.messaging_adapter_contract import DEFAULT_INGRESS_GATE, ingress_dedupe_key

        class FakeMessage:
            msg_id = "chat-produce-msg-1"
            ctype = ContextType.TEXT
            content = "hello"
            from_user_id = "user-1"
            other_user_id = "thread-1"
            is_group = False

        channel = ChatChannel.__new__(ChatChannel)
        channel.lock = threading.RLock()
        channel.sessions = {}
        channel.futures = {}

        context = Context(ContextType.TEXT, "hello")
        context.kwargs = {
            "channel_type": "feishu",
            "session_id": "thread-1",
            "receiver": "thread-1",
            "msg": FakeMessage(),
        }
        DEFAULT_INGRESS_GATE.forget(ingress_dedupe_key(context))
        try:
            channel.produce(context)
            channel.produce(context)
            queue = channel.sessions["thread-1"][0]
            self.assertEqual(queue.qsize(), 1)
            self.assertNotIn("hermes", type(queue).__module__.lower())
        finally:
            DEFAULT_INGRESS_GATE.forget(ingress_dedupe_key(context))

    def test_messaging_adapter_dedupe_scope_and_no_message_id_contexts(self):
        from bridge.context import Context, ContextType
        from channel.chat_channel import ChatChannel
        from channel.messaging_adapter_contract import DEFAULT_INGRESS_GATE, ingress_dedupe_key

        class FakeMessage:
            ctype = ContextType.TEXT
            content = "hello"
            from_user_id = "user-1"
            other_user_id = "thread-1"
            is_group = False

            def __init__(self, msg_id):
                self.msg_id = msg_id

        def make_channel():
            channel = ChatChannel.__new__(ChatChannel)
            channel.lock = threading.RLock()
            channel.sessions = {}
            channel.futures = {}
            return channel

        contexts = []
        for platform, session, ctype in [
            ("feishu", "thread-1", ContextType.TEXT),
            ("slack", "thread-1", ContextType.TEXT),
            ("feishu", "thread-2", ContextType.TEXT),
            ("feishu", "thread-1", ContextType.FILE),
        ]:
            context = Context(ctype, "hello")
            context.kwargs = {
                "channel_type": platform,
                "session_id": session,
                "receiver": session,
                "msg": FakeMessage("shared-msg-id"),
            }
            contexts.append(context)

        no_id_context = Context(ContextType.TEXT, "hello")
        no_id_context.kwargs = {
            "channel_type": "feishu",
            "session_id": "no-id-session",
            "receiver": "no-id-session",
            "msg": FakeMessage(""),
        }

        for context in contexts + [no_id_context]:
            DEFAULT_INGRESS_GATE.forget(ingress_dedupe_key(context))
        try:
            channel = make_channel()
            for context in contexts:
                channel.produce(context)
            channel.produce(contexts[0])
            self.assertEqual(channel.sessions["thread-1"][0].qsize(), 3)
            self.assertEqual(channel.sessions["thread-2"][0].qsize(), 1)

            no_id_channel = make_channel()
            no_id_channel.produce(no_id_context)
            no_id_channel.produce(no_id_context)
            self.assertEqual(ingress_dedupe_key(no_id_context), "")
            self.assertEqual(no_id_channel.sessions["no-id-session"][0].qsize(), 2)
        finally:
            for context in contexts + [no_id_context]:
                DEFAULT_INGRESS_GATE.forget(ingress_dedupe_key(context))

    def test_messaging_adapter_helper_with_real_chat_channel_does_not_double_dedupe(self):
        from bridge.context import Context, ContextType
        from channel.chat_channel import ChatChannel
        from channel.messaging_adapter_contract import DEFAULT_INGRESS_GATE, ingress_dedupe_key, produce_context_once

        class FakeMessage:
            msg_id = "helper-real-channel-msg-1"
            ctype = ContextType.TEXT
            content = "hello"
            from_user_id = "user-1"
            other_user_id = "thread-1"
            is_group = False

        channel = ChatChannel.__new__(ChatChannel)
        channel.lock = threading.RLock()
        channel.sessions = {}
        channel.futures = {}
        context = Context(ContextType.TEXT, "hello")
        context.kwargs = {
            "channel_type": "feishu",
            "session_id": "thread-1",
            "receiver": "thread-1",
            "msg": FakeMessage(),
        }
        DEFAULT_INGRESS_GATE.forget(ingress_dedupe_key(context))
        try:
            first = produce_context_once(channel, context)
            second = produce_context_once(channel, context)
            self.assertEqual(first["status"], "queued")
            self.assertEqual(second["status"], "duplicate")
            self.assertEqual(channel.sessions["thread-1"][0].qsize(), 1)
        finally:
            DEFAULT_INGRESS_GATE.forget(ingress_dedupe_key(context))

    def test_external_connection_runtime_events_project_lifecycle_test_ingress_delivery(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests
        from bridge.context import Context, ContextType
        from bridge.reply import Reply, ReplyType
        from channel.messaging_adapter_contract import (
            EXTERNAL_CONNECTION_EVENT_SESSION_ID,
            MessageIngressGate,
            deliver_reply,
            produce_context_once,
            record_external_connection_runtime_event,
        )

        class FakeMessage:
            msg_id = "runtime-msg-1"
            create_time = 123
            ctype = ContextType.TEXT
            content = "hello private xoxb-runtime-ingress-123456"
            from_user_id = "user-1"
            to_user_id = "bot-1"
            other_user_id = "thread-runtime"
            is_group = False

        class FakeChannel:
            def __init__(self):
                self.produced = []
                self.sent = []

            def produce(self, context):
                self.produced.append(context)

            def send(self, reply, context):
                self.sent.append((reply, context))

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "external-runtime-events.db")
            record_external_connection_runtime_event(
                "slack",
                "external_connection.lifecycle.start_requested",
                {"action": "start", "status": "starting", "operation_id": "op-runtime"},
                operation_id="op-runtime",
            )
            record_external_connection_runtime_event(
                "slack",
                "external_connection.test.completed",
                {
                    "action": "test",
                    "status": "success",
                    "configured": True,
                    "connected": False,
                    "callable": True,
                    "mode": "projection_dry_run",
                    "lastError": "api_key=sk-runtime-test-secret-123456",
                },
            )
            context = Context(ContextType.TEXT, "open this private xoxb-runtime-context-123456")
            context.kwargs = {
                "channel_type": "slack",
                "session_id": "thread-runtime",
                "receiver": "thread-runtime",
                "msg": FakeMessage(),
            }
            gate = MessageIngressGate(ttl_seconds=60)
            channel = FakeChannel()
            produce_context_once(channel, context, gate=gate)
            deliver_reply(channel, Reply(ReplyType.TEXT, "ok private xoxb-runtime-delivery-123456"), context)
            projection = RuntimeProjectionService(ledger).session_projection(
                EXTERNAL_CONNECTION_EVENT_SESSION_ID,
                limit=0,
            )
            events = ledger.list_events(session_id=EXTERNAL_CONNECTION_EVENT_SESSION_ID, limit=0)

        reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-external-runtime-reset.db")

        serialized = json.dumps({"projection": projection, "events": events}, ensure_ascii=False)
        event_types = [event["event_type"] for event in events]
        self.assertIn("external_connection.lifecycle.start_requested", event_types)
        self.assertIn("external_connection.test.completed", event_types)
        self.assertIn("external_connection.ingress.queued", event_types)
        self.assertIn("external_connection.delivery.sent", event_types)
        projected = {}
        for request in projection["requests"]:
            for item in request.get("external_connections") or []:
                projected[item["platform"]] = item
        self.assertIn("slack", projected)
        self.assertEqual(projected["slack"]["platform"], "slack")
        self.assertIn(projected["slack"]["status"], {"sent", "success", "queued", "starting"})
        self.assertIn("lastEventType", projected["slack"])
        self.assertIn("lastIngress", serialized)
        self.assertIn("lastDelivery", serialized)
        last_delivery = projected["slack"]["lastDelivery"]
        self.assertNotIn("sessionId", last_delivery)
        self.assertNotIn("receiver", last_delivery)
        self.assertTrue(last_delivery["sessionHash"])
        self.assertTrue(last_delivery["receiverHash"])
        self.assertEqual(last_delivery["sessionSummary"]["chars"], len("thread-runtime"))
        self.assertEqual(last_delivery["receiverSummary"]["chars"], len("thread-runtime"))
        self.assertNotIn("thread-runtime", json.dumps({"lastDelivery": last_delivery}, ensure_ascii=False))
        self.assertNotIn("xoxb-runtime", serialized)
        self.assertNotIn("open this private", serialized)
        self.assertNotIn("hello private", serialized)
        self.assertNotIn("ok private", serialized)
        self.assertNotIn("sk-runtime-test-secret", serialized)

    def test_external_connections_api_includes_runtime_projection_from_ledger(self):
        install_web_stub()
        from agent.protocol import reset_run_event_ledger_for_tests
        from channel.messaging_adapter_contract import record_external_connection_runtime_event
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as root:
            reset_run_event_ledger_for_tests(Path(root) / "external-connections-api-events.db")
            record_external_connection_runtime_event(
                "feishu",
                "external_connection.test.completed",
                {
                    "action": "test",
                    "status": "success",
                    "configured": True,
                    "connected": False,
                    "callable": True,
                    "lastError": "secret=sk-api-runtime-secret-123456",
                },
            )
            channels_payload = {
                "status": "success",
                "channels": [{
                    "name": "feishu",
                    "label": {"zh": "飞书"},
                    "description": "",
                    "icon": "message",
                    "color": "blue",
                    "active": True,
                    "configured": True,
                    "running": False,
                    "last_error": "",
                    "status": "configured",
                    "configState": {"state": "configured"},
                    "auth": {},
                    "agentSurface": {"callable": True},
                    "fields": [],
                    "adapterContract": {"version": "ecorex.messaging_adapter.v1"},
                    "homeChannel": {},
                }],
            }
            with (
                patch.object(web_channel, "_require_auth", return_value=None),
                patch.object(web_channel.ChannelsHandler, "GET", return_value=json.dumps(channels_payload, ensure_ascii=False)),
            ):
                payload = json.loads(web_channel.ExternalConnectionsHandler().GET())

        reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-external-api-reset.db")

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["runtimeProjection"]["source"], "RunEventLedger")
        connection = payload["connections"][0]
        self.assertEqual(connection["runtimeProjectionSource"], "RunEventLedger")
        self.assertEqual(connection["runtimeProjection"]["platform"], "feishu")
        self.assertEqual(connection["runtimeProjection"]["lastAction"], "test")
        self.assertEqual(connection["runtimeProjection"]["status"], "success")
        self.assertNotIn("sk-api-runtime-secret", serialized)

    def test_external_connection_delivery_error_redacts_exception_in_projection_and_api(self):
        install_web_stub()
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests
        from bridge.context import Context, ContextType
        from bridge.reply import Reply, ReplyType
        from channel.messaging_adapter_contract import EXTERNAL_CONNECTION_EVENT_SESSION_ID, deliver_reply
        from channel.web import web_channel

        private_error = "send failed for private outbound body without token marker"

        class FakeMessage:
            msg_id = "delivery-error-msg-1"
            create_time = 123
            ctype = ContextType.TEXT
            content = "input private body without token marker"
            from_user_id = "user-1"
            to_user_id = "bot-1"
            other_user_id = "thread-delivery-error"
            is_group = False

        class FailingChannel:
            def send(self, reply, context):
                raise RuntimeError(private_error)

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "external-delivery-error-events.db")
            context = Context(ContextType.TEXT, "input private body without token marker")
            context.kwargs = {
                "channel_type": "slack",
                "session_id": "thread-delivery-error",
                "receiver": "thread-delivery-error",
                "msg": FakeMessage(),
            }
            result = deliver_reply(
                FailingChannel(),
                Reply(ReplyType.TEXT, "reply private body without token marker"),
                context,
                platform="slack",
            )
            events = ledger.list_events(session_id=EXTERNAL_CONNECTION_EVENT_SESSION_ID, limit=0)
            projection = RuntimeProjectionService(ledger).external_connections_projection(limit=0)
            channels_payload = {
                "status": "success",
                "channels": [{
                    "name": "slack",
                    "label": {"zh": "Slack"},
                    "description": "",
                    "icon": "message",
                    "color": "purple",
                    "active": True,
                    "configured": True,
                    "running": False,
                    "last_error": "",
                    "status": "configured",
                    "configState": {"state": "configured"},
                    "auth": {},
                    "agentSurface": {"callable": True},
                    "fields": [],
                    "adapterContract": {"version": "ecorex.messaging_adapter.v1"},
                    "homeChannel": {},
                }],
            }
            with (
                patch.object(web_channel, "_require_auth", return_value=None),
                patch.object(web_channel.ChannelsHandler, "GET", return_value=json.dumps(channels_payload, ensure_ascii=False)),
            ):
                api_payload = json.loads(web_channel.ExternalConnectionsHandler().GET())

        reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-external-delivery-error-reset.db")

        serialized = json.dumps({
            "result": result,
            "events": events,
            "projection": projection,
            "api": api_payload,
        }, ensure_ascii=False)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "delivery_failed")
        self.assertTrue(result["errorSummary"]["redacted"])
        self.assertIn("external_connection.delivery.error", [event["event_type"] for event in events])
        projected = {
            item["platform"]: item
            for item in projection.get("external_connections") or []
        }
        self.assertEqual(projected["slack"]["status"], "error")
        self.assertEqual(projected["slack"]["error"], "delivery_failed")
        self.assertTrue(projected["slack"]["errorSummary"]["redacted"])
        projected_delivery = projected["slack"]["lastDelivery"]
        self.assertNotIn("sessionId", projected_delivery)
        self.assertNotIn("receiver", projected_delivery)
        self.assertTrue(projected_delivery["sessionHash"])
        self.assertTrue(projected_delivery["receiverHash"])
        self.assertEqual(projected_delivery["sessionSummary"]["chars"], len("thread-delivery-error"))
        self.assertEqual(projected_delivery["receiverSummary"]["chars"], len("thread-delivery-error"))
        connection_projection = api_payload["connections"][0]["runtimeProjection"]
        self.assertEqual(connection_projection["error"], "delivery_failed")
        self.assertTrue(connection_projection["errorSummary"]["redacted"])
        api_delivery = connection_projection["lastDelivery"]
        self.assertTrue(api_delivery["sessionHash"])
        self.assertTrue(api_delivery["receiverHash"])
        self.assertNotIn("sessionId", api_delivery)
        self.assertNotIn("receiver", api_delivery)
        self.assertNotIn(private_error, serialized)
        self.assertNotIn("thread-delivery-error", json.dumps({
            "projectedDelivery": projected_delivery,
            "apiDelivery": api_delivery,
        }, ensure_ascii=False))
        self.assertNotIn("private outbound body", serialized)
        self.assertNotIn("reply private body", serialized)
        self.assertNotIn("input private body", serialized)

    def test_external_connection_error_labels_reject_token_shaped_values(self):
        install_web_stub()
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests
        from channel.messaging_adapter_contract import EXTERNAL_CONNECTION_EVENT_SESSION_ID, record_external_connection_runtime_event
        from channel.web import web_channel

        slack_like = "xoxb-123456789012-ABCDEFGHIJKLMN"
        app_like = "xapp-123456789012-ABCDEFGHIJKLMN"
        telegram_like = "123456789:ABCDEFGHIJKLMNOPQRST_uvwx"
        github_like = "github" + "_pat_" + "1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "external-error-label-token-shapes.db")
            record_external_connection_runtime_event(
                "slack",
                "external_connection.delivery.error",
                {
                    "status": "error",
                    "error": slack_like,
                    "lastError": github_like,
                    "mode": app_like,
                    "reason": telegram_like,
                    "adapter": {
                        "reason": github_like,
                        "deliveryMode": app_like,
                    },
                },
            )
            raw_events = ledger.list_events(session_id=EXTERNAL_CONNECTION_EVENT_SESSION_ID, limit=0)
            projection = RuntimeProjectionService(ledger).external_connections_projection(limit=0)
            session_projection = RuntimeProjectionService(ledger).session_projection(
                EXTERNAL_CONNECTION_EVENT_SESSION_ID,
                limit=0,
            )
            channels_payload = {
                "status": "success",
                "channels": [{
                    "name": "slack",
                    "label": {"zh": "Slack"},
                    "description": "",
                    "icon": "message",
                    "color": "purple",
                    "active": True,
                    "configured": True,
                    "running": False,
                    "last_error": "",
                    "status": "configured",
                    "configState": {"state": "configured"},
                    "auth": {},
                    "agentSurface": {"callable": True},
                    "fields": [],
                    "adapterContract": {"version": "ecorex.messaging_adapter.v1"},
                    "homeChannel": {},
                }],
            }
            with (
                patch.object(web_channel, "_require_auth", return_value=None),
                patch.object(web_channel.ChannelsHandler, "GET", return_value=json.dumps(channels_payload, ensure_ascii=False)),
            ):
                api_payload = json.loads(web_channel.ExternalConnectionsHandler().GET())

        reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-external-error-label-reset.db")

        serialized = json.dumps({
            "rawEvents": raw_events,
            "projection": projection,
            "sessionProjection": session_projection,
            "api": api_payload,
        }, ensure_ascii=False)
        projected = {
            item["platform"]: item
            for item in projection.get("external_connections") or []
        }
        self.assertEqual(projected["slack"]["error"], "external_connection_error")
        self.assertEqual(projected["slack"]["lastError"], "external_connection_error")
        self.assertNotIn(slack_like, serialized)
        self.assertNotIn(app_like, serialized)
        self.assertNotIn(telegram_like, serialized)
        self.assertNotIn(github_like, serialized)
        self.assertIn("telegram-***", serialized)

    def test_external_connection_event_identifiers_reject_token_shaped_values(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests
        from channel.messaging_adapter_contract import EXTERNAL_CONNECTION_EVENT_SESSION_ID, record_external_connection_runtime_event

        slack_like = "xoxb-1234567890-1234567890123-abcdefghijklmnop"
        github_like = "github" + "_pat_" + "1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd"
        explicit_request_id = f"external-connection:slack:{slack_like}"

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "external-event-identifier-token-shapes.db")
            record_external_connection_runtime_event(
                "slack",
                "external_connection.lifecycle.start_requested",
                {
                    "action": "start",
                    "status": "starting",
                    "operation_id": github_like,
                },
                operation_id=github_like,
            )
            record_external_connection_runtime_event(
                "slack",
                "external_connection.lifecycle.stop_requested",
                {
                    "action": "stop",
                    "status": "stopping",
                    "operation_id": slack_like,
                },
                request_id=explicit_request_id,
            )
            projection = RuntimeProjectionService(ledger).external_connections_projection(limit=0)
            session_projection = RuntimeProjectionService(ledger).session_projection(
                EXTERNAL_CONNECTION_EVENT_SESSION_ID,
                limit=0,
            )
            raw_events = ledger.list_events(session_id=EXTERNAL_CONNECTION_EVENT_SESSION_ID, limit=0)

        reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-external-event-id-reset.db")

        serialized = json.dumps({
            "rawEvents": raw_events,
            "projection": projection,
            "sessionProjection": session_projection,
        }, ensure_ascii=False)
        event_request_ids = [
            str(event.get("request_id") or "")
            for event in projection.get("events") or []
        ]
        self.assertEqual(len(event_request_ids), 2)
        self.assertNotIn(slack_like, serialized)
        self.assertNotIn(github_like, serialized)
        self.assertNotIn(explicit_request_id, serialized)
        self.assertTrue(all(slack_like not in request_id and github_like not in request_id for request_id in event_request_ids))

    def test_external_connection_real_action_paths_emit_runtime_events(self):
        install_web_stub()
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests
        from channel.messaging_adapter_contract import EXTERNAL_CONNECTION_EVENT_SESSION_ID
        from channel.web import web_channel

        class DummyThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                return None

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "external-action-path-events.db")
            config_path = Path(root) / "config.json"
            config_path.write_text(json.dumps({
                "channel_type": "web",
                "feishu_app_id": "cli-existing",
                "feishu_app_secret": "existing-secret",
            }), encoding="utf-8")
            fake_conf = {
                "channel_type": "web",
                "feishu_app_id": "cli-existing",
                "feishu_app_secret": "existing-secret",
            }
            with (
                patch.object(web_channel, "conf", return_value=fake_conf),
                patch.object(web_channel.ChannelsHandler, "_config_path", return_value=str(config_path)),
                patch.object(web_channel.threading, "Thread", DummyThread),
            ):
                start_payload = json.loads(web_channel.ChannelsHandler()._handle_connect("feishu", {}))
                stop_payload = json.loads(web_channel.ChannelsHandler()._handle_disconnect("feishu"))

            channels_payload = {
                "status": "success",
                "channels": [{
                    "name": "feishu",
                    "label": {"zh": "飞书"},
                    "description": "",
                    "icon": "message",
                    "color": "blue",
                    "active": True,
                    "configured": True,
                    "running": False,
                    "last_error": "token=sk-action-test-secret-123456",
                    "status": "configured",
                    "configState": {"state": "configured"},
                    "auth": {},
                    "agentSurface": {"callable": True},
                    "fields": [],
                    "adapterContract": {"version": "ecorex.messaging_adapter.v1"},
                    "homeChannel": {},
                }],
            }
            with (
                patch.object(web_channel, "conf", return_value=fake_conf),
                patch.object(web_channel.ChannelsHandler, "GET", return_value=json.dumps(channels_payload, ensure_ascii=False)),
            ):
                test_payload = json.loads(web_channel.ExternalConnectionActionHandler._handle_test("feishu"))

            events = ledger.list_events(session_id=EXTERNAL_CONNECTION_EVENT_SESSION_ID, limit=0)
            projection = RuntimeProjectionService(ledger).external_connections_projection(limit=0)

        reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-external-action-reset.db")

        serialized = json.dumps({"events": events, "projection": projection}, ensure_ascii=False)
        event_types = [event["event_type"] for event in events]
        self.assertEqual(start_payload["status"], "success")
        self.assertEqual(stop_payload["status"], "success")
        self.assertEqual(test_payload["status"], "success")
        self.assertIn("external_connection.lifecycle.start_requested", event_types)
        self.assertIn("external_connection.lifecycle.stop_requested", event_types)
        self.assertIn("external_connection.test.completed", event_types)
        projected = {
            item["platform"]: item
            for item in projection.get("external_connections") or []
        }
        self.assertEqual(projected["feishu"]["lastAction"], "test")
        self.assertEqual(projected["feishu"]["status"], "success")
        self.assertNotIn("sk-action-test-secret", serialized)

    def test_external_connections_api_runtime_projection_uses_latest_event_after_window(self):
        install_web_stub()
        from agent.protocol import reset_run_event_ledger_for_tests
        from channel.messaging_adapter_contract import record_external_connection_runtime_event
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "external-connections-window-events.db")
            for index in range(501):
                final = index == 500
                record_external_connection_runtime_event(
                    "feishu",
                    "external_connection.test.completed",
                    {
                        "action": "test",
                        "status": "success" if final else "starting",
                        "configured": True,
                        "connected": final,
                        "callable": True,
                    },
                    operation_id=f"window-{index}",
                )
            latest_event_id = ledger.latest_event_id()
            channels_payload = {
                "status": "success",
                "channels": [{
                    "name": "feishu",
                    "label": {"zh": "飞书"},
                    "description": "",
                    "icon": "message",
                    "color": "blue",
                    "active": True,
                    "configured": True,
                    "running": False,
                    "last_error": "",
                    "status": "configured",
                    "configState": {"state": "configured"},
                    "auth": {},
                    "agentSurface": {"callable": True},
                    "fields": [],
                    "adapterContract": {"version": "ecorex.messaging_adapter.v1"},
                    "homeChannel": {},
                }],
            }
            with (
                patch.object(web_channel, "_require_auth", return_value=None),
                patch.object(web_channel.ChannelsHandler, "GET", return_value=json.dumps(channels_payload, ensure_ascii=False)),
            ):
                payload = json.loads(web_channel.ExternalConnectionsHandler().GET())

        reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-external-window-reset.db")

        connection = payload["connections"][0]
        self.assertEqual(payload["runtimeProjection"]["latestEventId"], latest_event_id)
        self.assertEqual(connection["runtimeProjection"]["lastEventId"], latest_event_id)
        self.assertEqual(connection["runtimeProjection"]["status"], "success")
        self.assertTrue(connection["runtimeProjection"]["connected"])

    def test_chat_channel_worker_failure_releases_adapter_dedupe_for_retry(self):
        from bridge.context import Context, ContextType
        from channel.chat_channel import ChatChannel
        from channel.messaging_adapter_contract import DEFAULT_INGRESS_GATE, ingress_dedupe_key

        class FakeMessage:
            msg_id = "worker-failure-msg-1"
            ctype = ContextType.TEXT
            content = "private body"
            from_user_id = "user-1"
            other_user_id = "thread-1"
            is_group = False

        class FakeSemaphore:
            def __init__(self):
                self.releases = 0

            def release(self):
                self.releases += 1

        class FailedWorker:
            def exception(self):
                return RuntimeError("private worker failure sk-worker-secret-123456")

        channel = ChatChannel.__new__(ChatChannel)
        channel.lock = threading.RLock()
        channel.sessions = {}
        channel.futures = {}
        channel._fail_callback = lambda *args, **kwargs: None

        context = Context(ContextType.TEXT, "private body")
        context.kwargs = {
            "channel_type": "feishu",
            "session_id": "thread-1",
            "receiver": "thread-1",
            "msg": FakeMessage(),
        }
        key = ingress_dedupe_key(context)
        DEFAULT_INGRESS_GATE.forget(key)
        try:
            channel.produce(context)
            semaphore = FakeSemaphore()
            channel.sessions["thread-1"][1] = semaphore
            callback = channel._thread_pool_callback("thread-1", context=context)
            callback(FailedWorker())

            retry_context = Context(ContextType.TEXT, "private body")
            retry_context.kwargs = dict(context.kwargs)
            channel.produce(retry_context)
            self.assertEqual(channel.sessions["thread-1"][0].qsize(), 2)
            self.assertEqual(semaphore.releases, 1)
        finally:
            DEFAULT_INGRESS_GATE.forget(key)

    def test_chat_channel_worker_failure_releases_custom_adapter_gate_for_retry(self):
        from bridge.context import Context, ContextType
        from channel.chat_channel import ChatChannel
        from channel.messaging_adapter_contract import MessageIngressGate, ingress_dedupe_key, produce_context_once

        class FakeMessage:
            msg_id = "custom-gate-worker-failure-msg-1"
            ctype = ContextType.TEXT
            content = "private body"
            from_user_id = "user-1"
            other_user_id = "thread-1"
            is_group = False

        class FakeSemaphore:
            def release(self):
                pass

        class FailedWorker:
            def exception(self):
                return RuntimeError("private worker failure")

        channel = ChatChannel.__new__(ChatChannel)
        channel.lock = threading.RLock()
        channel.sessions = {}
        channel.futures = {}
        channel._fail_callback = lambda *args, **kwargs: None
        gate = MessageIngressGate(ttl_seconds=60)

        context = Context(ContextType.TEXT, "private body")
        context.kwargs = {
            "channel_type": "slack",
            "session_id": "thread-1",
            "receiver": "thread-1",
            "msg": FakeMessage(),
        }
        key = ingress_dedupe_key(context)
        try:
            first = produce_context_once(channel, context, gate=gate)
            channel.sessions["thread-1"][1] = FakeSemaphore()
            callback = channel._thread_pool_callback("thread-1", context=context)
            callback(FailedWorker())

            retry_context = Context(ContextType.TEXT, "private body")
            retry_context.kwargs = dict(context.kwargs)
            retry = produce_context_once(channel, retry_context, gate=gate)
            self.assertEqual(first["status"], "queued")
            self.assertEqual(retry["status"], "queued")
            self.assertEqual(channel.sessions["thread-1"][0].qsize(), 2)
        finally:
            gate.forget(key)

    def test_subagent_sse_start_uses_public_task_summary_not_raw_arguments(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")

        self.assertIn("def _public_subagent_task(", source)
        self.assertIn("_subagent_public_name(public_task", source)
        self.assertIn('"summaryHash": public_task.get("summaryHash") or ""', source)
        self.assertNotIn('arguments.get("summary") or arguments.get("task") or ""', source)
        self.assertNotIn('arguments.get("name") or arguments.get("summary") or arguments.get("task")', source)
        self.assertNotIn('task.get("name") or task.get("summary") or task.get("id") or "Subagent"', source)

    def test_r23_09_public_logs_use_summaries_for_scheduler_and_chat_bodies(self):
        root = Path(__file__).resolve().parents[1]
        scheduler_source = (root / "agent" / "tools" / "scheduler" / "integration.py").read_text(encoding="utf-8")
        chat_source = (root / "channel" / "chat_channel.py").read_text(encoding="utf-8")
        agent_bridge_source = (root / "bridge" / "agent_bridge.py").read_text(encoding="utf-8")

        self.assertIn("_body_summary(e)", scheduler_source)
        self.assertIn("receiverHash=", scheduler_source)
        self.assertNotIn("receiver={receiver}", scheduler_source)
        self.assertNotIn("result sent to {receiver}", scheduler_source)
        self.assertNotIn("Failed to send result: {e}", scheduler_source)
        self.assertNotIn("Failed to remember delivered output for {session_id}: {e}", scheduler_source)

        self.assertIn("_body_log_summary(e)", chat_source)
        self.assertIn("_context_log_summary(context)", chat_source)
        self.assertNotIn('"handling context: {}".format(context)', chat_source)
        self.assertNotIn('"sendMsg error: {}".format(e)', chat_source)
        self.assertNotIn('"decorate reply: {}".format(reply)', chat_source)
        self.assertNotIn('any to wav error, use raw path. " + str(e)', chat_source)
        self.assertIn('any to wav error, use raw path. {}".format(_body_log_summary(e))', chat_source)
        self.assertIn('reference query skipped content={}".format(_body_log_summary(content))', chat_source)
        self.assertIn("Added prefix to message summary: {_web_body_log_summary(prompt)}", (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8"))
        self.assertNotIn("logger.debug(content)", chat_source)
        self.assertNotIn("Added prefix to message: {prompt}", (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8"))
        self.assertIn("Eager scheduler init failed: {_exception_log_summary(e)}", agent_bridge_source)
        self.assertIn("Failed to attach context to scheduler: {_exception_log_summary(e)}", agent_bridge_source)
        self.assertNotIn("Eager scheduler init failed: {e}", agent_bridge_source)
        self.assertNotIn("Failed to attach context to scheduler: {e}", agent_bridge_source)

    def test_web_public_exception_surfaces_redact_worker_and_preworker_errors(self):
        install_web_stub()
        from agent.protocol import reset_run_ledger_for_tests
        from bridge.context import Context, ContextType
        from channel.web import web_channel

        secret_error = RuntimeError("private prompt body with sk-public-error-123456")
        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_ledger_for_tests(Path(root) / "run-ledger.db")
            channel = web_channel.WebChannel()

            worker_request_id = "req-worker-public-error"
            worker_session_id = "session-worker-public-error"
            ledger.create_run(worker_request_id, worker_session_id, phase="running", status="running")
            channel.request_to_session[worker_request_id] = worker_session_id
            channel._ensure_sse_state(worker_request_id)
            context = Context(ContextType.TEXT, "hello")
            context.kwargs = {"request_id": worker_request_id, "session_id": worker_session_id}
            channel._finalize_request_after_worker(context, secret_error)

            pre_request_id = "req-pre-public-error"
            pre_session_id = "session-pre-public-error"
            ledger.create_run(pre_request_id, pre_session_id, phase="running", status="running")
            channel.request_to_session[pre_request_id] = pre_session_id
            channel._ensure_sse_state(pre_request_id)
            public_message = web_channel._public_exception_message(
                "Message request failed before worker start.",
                secret_error,
            )
            public_extra = web_channel._public_exception_summary(secret_error)
            channel._abort_pre_worker_request(
                pre_request_id,
                pre_session_id,
                message=public_message,
                reason="post_message_exception",
                error_code="POST_MESSAGE_EXCEPTION",
                error_extra=public_extra,
            )
            message_response = {"status": "error", "message": public_message, **public_extra}

            payload = {
                "workerRun": ledger.get_run(worker_request_id),
                "preRun": ledger.get_run(pre_request_id),
                "workerEvents": channel.sse_events.get(worker_request_id, []),
                "preEvents": channel.sse_events.get(pre_request_id, []),
                "active": channel.active_requests_snapshot(),
                "messageResponse": message_response,
            }
        reset_run_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-public-error-reset.db")

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("private prompt body", serialized)
        self.assertNotIn("sk-public-error", serialized)
        self.assertIn("Details redacted", serialized)
        self.assertIn("errorHash", serialized)
        self.assertIn("errorLength", serialized)

    def test_scheduler_terminal_error_redacts_exception_text(self):
        from agent.protocol import reset_run_ledger_for_tests
        from agent.tools.scheduler import integration

        private_error = RuntimeError("private scheduler body xoxb-scheduler-secret-123456")
        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_ledger_for_tests(Path(root) / "scheduler-ledger.db")
            task = {
                "id": "task-public-error",
                "name": "public error",
                "enabled": True,
                "action": {
                    "type": "agent_task",
                    "channel_type": "web",
                    "receiver": "receiver-public-error",
                    "task_description": "safe description",
                },
            }
            with patch.object(integration, "_authorize_scheduled_execution", side_effect=private_error):
                ok = integration._execute_scheduled_task(task, object())
            run = ledger.get_run(task["_scheduler_run_request_id"])
        reset_run_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-scheduler-error-reset.db")

        serialized = json.dumps({"ok": ok, "run": run}, ensure_ascii=False)
        self.assertFalse(ok)
        self.assertNotIn("private scheduler body", serialized)
        self.assertNotIn("xoxb-scheduler-secret", serialized)
        self.assertIn("Details redacted", serialized)
        self.assertIn("SCHEDULER_EXECUTION_EXCEPTION", serialized)

    def test_agent_stream_error_redacts_before_webchannel_sse_and_run_ledger(self):
        from queue import Queue
        from agent.protocol import reset_run_ledger_for_tests
        from agent.protocol.agent_stream import AgentStreamExecutor

        install_web_stub()
        from channel.web import web_channel

        private_error = RuntimeError("private agent stream body sk-agent-stream-secret-123456")
        events = []
        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}),
            model=types.SimpleNamespace(model="test-model"),
            system_prompt="",
            tools=[],
            on_event=events.append,
        )

        def fail_llm(*args, **kwargs):
            raise private_error

        executor._trim_messages = lambda: None
        executor._validate_and_fix_messages = lambda: None
        executor._call_llm_stream = fail_llm
        with self.assertRaises(RuntimeError):
            executor.run_stream("hello")

        agent_error = next(event["data"] for event in events if event["type"] == "error")
        serialized_agent_error = json.dumps(agent_error, ensure_ascii=False)
        self.assertNotIn("private agent stream body", serialized_agent_error)
        self.assertNotIn("sk-agent-stream-secret", serialized_agent_error)
        self.assertIn("Details redacted", agent_error["error"])
        self.assertTrue(agent_error["errorHash"])
        self.assertTrue(agent_error["errorLength"])

        request_id = "req-agent-stream-redacted-error"
        session_id = "session-agent-stream-redacted-error"
        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_ledger_for_tests(Path(root) / "agent-stream-ledger.db")
            ledger.create_run(request_id, session_id)
            channel = web_channel.WebChannel()
            channel.request_to_session = {request_id: session_id}
            channel.sse_queues = {request_id: Queue()}
            callback = channel._make_sse_callback(request_id)

            with patch.object(channel, "_fetch_agent_usage", return_value=None):
                callback({"type": "error", "data": agent_error})

            sse_event = channel.sse_queues[request_id].get(timeout=1)
            run = ledger.get_run(request_id)

        reset_run_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-agent-stream-error-reset.db")

        serialized = json.dumps({"sse": sse_event, "run": run}, ensure_ascii=False)
        self.assertNotIn("private agent stream body", serialized)
        self.assertNotIn("sk-agent-stream-secret", serialized)
        self.assertIn("Details redacted", serialized)
        self.assertIn("errorHash", serialized)
        self.assertEqual(run["status"], "failed")
        self.assertIn("Details redacted", run["error_message"])

    def test_agent_stream_model_error_logs_redact_exception_and_error_chunks(self):
        import io
        import logging
        from agent.protocol.agent_stream import AgentStreamExecutor
        from common.log import logger

        class RaisingModel:
            model = "raising-model"

            def call_stream(self, request):
                raise RuntimeError("private model raise sk-agent-log-raise-123456")

        class ErrorChunkModel:
            model = "error-chunk-model"

            def call_stream(self, request):
                yield {
                    "error": {
                        "message": "private model chunk xoxb-agent-log-chunk-123456",
                        "code": "server_error",
                        "type": "api_error",
                    },
                    "status_code": 503,
                }

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            for model in (RaisingModel(), ErrorChunkModel()):
                executor = AgentStreamExecutor(
                    agent=types.SimpleNamespace(last_usage={}, memory_manager=None),
                    model=model,
                    system_prompt="",
                    tools=[],
                    messages=[{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
                )
                executor._prepare_messages = lambda: [{"role": "user", "content": "hello"}]
                with self.assertRaises(Exception):
                    executor._call_llm_stream(retry_on_empty=False, max_retries=0)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

        logs = stream.getvalue()
        self.assertNotIn("private model raise", logs)
        self.assertNotIn("sk-agent-log-raise", logs)
        self.assertNotIn("private model chunk", logs)
        self.assertNotIn("xoxb-agent-log-chunk", logs)
        self.assertIn("Details redacted", logs)
        self.assertIn("errorHash", logs)

    def test_agent_stream_tool_error_payloads_and_mcp_sync_logs_redact_private_text(self):
        import io
        import logging
        from agent.protocol.agent_stream import AgentStreamExecutor
        from common.log import logger

        class SuccessModel:
            model = "success-model"

            def call_stream(self, request):
                yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

        class BadToolMap:
            def get(self, name):
                raise RuntimeError("private tool lookup sk-tool-lookup-123456")

        class BadTool:
            name = "safe_tool"

            def execute_tool(self, arguments):
                raise RuntimeError("private tool execution xoxb-tool-exec-123456")

        events = []
        executor = AgentStreamExecutor(
            agent=types.SimpleNamespace(last_usage={}, memory_manager=None, skill_manager=None),
            model=SuccessModel(),
            system_prompt="",
            tools=[],
            messages=[{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            on_event=events.append,
        )
        executor._prepare_messages = lambda: [{"role": "user", "content": "hello"}]
        executor._validate_and_fix_messages = lambda: None

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            with patch("agent.tools.ToolManager", side_effect=RuntimeError("private mcp sync sk-mcp-sync-123456")):
                executor._call_llm_stream(retry_on_empty=False, max_retries=0)

            executor.tools = BadToolMap()
            lookup_result = executor._execute_tool({"id": "tool-lookup", "name": "safe_missing_tool", "arguments": {}})

            executor.tools = {"safe_tool": BadTool()}
            executor._authorize_tool_execution = lambda *args, **kwargs: {"allowed": True}
            execution_result = executor._execute_tool({"id": "tool-exec", "name": "safe_tool", "arguments": {}})
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

        serialized = json.dumps({
            "logs": stream.getvalue(),
            "lookup": lookup_result,
            "execution": execution_result,
            "events": events,
        }, ensure_ascii=False)
        self.assertNotIn("private mcp sync", serialized)
        self.assertNotIn("sk-mcp-sync", serialized)
        self.assertNotIn("private tool lookup", serialized)
        self.assertNotIn("sk-tool-lookup", serialized)
        self.assertNotIn("private tool execution", serialized)
        self.assertNotIn("xoxb-tool-exec", serialized)
        self.assertIn("Details redacted", serialized)
        self.assertIn("errorHash", serialized)

    def test_agent_bridge_error_reply_redacts_private_exception_text(self):
        import io
        import logging

        from bridge.agent_bridge import AgentBridge
        from bridge.context import Context, ContextType
        from bridge.reply import ReplyType
        from common.log import logger

        class FailingAgent:
            def __init__(self):
                self.tools = []
                self.model = types.SimpleNamespace()
                self.messages = [{"role": "user", "content": "safe"}]
                self.messages_lock = threading.RLock()
                self._last_run_new_messages = []

            def run_stream(self, **_kwargs):
                raise RuntimeError("private bridge reply sk-bridge-reply-123456")

        bridge = AgentBridge.__new__(AgentBridge)
        bridge.get_agent = lambda session_id=None: FailingAgent()
        bridge._pre_persist_user_message = lambda *_args, **_kwargs: False
        bridge._persist_messages = lambda *_args, **_kwargs: None
        bridge._schedule_mcp_hot_reload = lambda *_args, **_kwargs: None

        context = Context(ContextType.TEXT, "hello")
        context.kwargs = {
            "session_id": "session-agentbridge-error-redaction",
            "request_id": "req-agentbridge-error-redaction",
        }

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            reply = bridge.agent_reply("hello", context)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

        serialized = json.dumps({"reply": str(reply), "content": reply.content, "logs": stream.getvalue()}, ensure_ascii=False)
        self.assertEqual(reply.type, ReplyType.ERROR)
        self.assertNotIn("private bridge reply", serialized)
        self.assertNotIn("sk-bridge-reply", serialized)
        self.assertIn("Details redacted", serialized)
        self.assertIn("hash=", serialized)

    def test_scheduler_tool_error_results_and_logs_redact_private_exception_text(self):
        import io
        import logging

        from agent.tools.scheduler.scheduler_tool import SchedulerTool
        from common.log import logger

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            unexpected_tool = SchedulerTool()
            unexpected_tool.task_store = object()
            with patch.object(unexpected_tool, "_list_tasks", side_effect=RuntimeError("private scheduler tool xoxb-scheduler-tool-123456")):
                unexpected_result = unexpected_tool.execute({"action": "list"})

            permission_tool = SchedulerTool()
            permission_tool.task_store = object()
            with patch("common.ecorex_tool_permissions.get_tool_permission_broker", side_effect=RuntimeError("private scheduler permission sk-scheduler-tool-perm-123456")):
                permission_result = permission_tool.execute({"action": "delete", "task_id": "task-safe"})

            lazy_tool = SchedulerTool()
            with patch("agent.tools.scheduler.integration.ensure_scheduler_runtime", side_effect=RuntimeError("private scheduler lazy xoxb-scheduler-tool-lazy-123456")):
                lazy_result = lazy_tool.execute({"action": "list"})
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

        serialized = json.dumps({
            "unexpected": {
                "status": unexpected_result.status,
                "result": unexpected_result.result,
            },
            "permission": {
                "status": permission_result.status,
                "result": permission_result.result,
            },
            "lazy": {
                "status": lazy_result.status,
                "result": lazy_result.result,
            },
            "logs": stream.getvalue(),
        }, ensure_ascii=False)
        for token in (
            "private scheduler tool",
            "xoxb-scheduler-tool",
            "private scheduler permission",
            "sk-scheduler-tool-perm",
            "private scheduler lazy",
            "xoxb-scheduler-tool-lazy",
        ):
            self.assertNotIn(token, serialized)
        self.assertEqual(unexpected_result.status, "error")
        self.assertIn("Details redacted", serialized)
        self.assertIn("errorHash", serialized)

    def test_scheduler_projection_redacts_load_and_permission_failures(self):
        projection_module = __import__("agent.tools.scheduler.projection", fromlist=["scheduler_projection"])
        task_store_module = __import__("agent.tools.scheduler.task_store", fromlist=["TaskStore"])

        load_error = RuntimeError("private load failure sk-load-secret-123456")
        permission_error = RuntimeError("private permission failure xoxb-permission-secret-123456")
        with tempfile.TemporaryDirectory() as root:
            with (
                patch.object(projection_module, "conf", return_value={"agent_workspace": root, "scheduler_enabled": True}),
                patch.object(task_store_module, "TaskStore", side_effect=load_error),
                patch("common.ecorex_tool_permissions.get_tool_permission_broker", side_effect=permission_error),
            ):
                projection = projection_module.scheduler_projection(root)

        serialized = json.dumps(projection, ensure_ascii=False)
        self.assertNotIn("private load failure", serialized)
        self.assertNotIn("sk-load-secret", serialized)
        self.assertNotIn("private permission failure", serialized)
        self.assertNotIn("xoxb-permission-secret", serialized)
        self.assertIn("Scheduler task store unavailable", projection["loadError"])
        self.assertTrue(projection["loadErrorHash"])
        self.assertEqual(projection["loadErrorType"], "RuntimeError")
        self.assertIn("Permission broker unavailable", projection["modifyBlockingReason"])
        self.assertTrue(projection["modifyBlockingReasonHash"])
        self.assertFalse(projection["canModify"])

    def test_scheduler_handler_get_post_fallbacks_redact_exceptions(self):
        install_web_stub()
        from channel.web import web_channel

        get_error = RuntimeError("private scheduler GET prompt sk-get-secret-123456")
        post_error = RuntimeError("private scheduler POST prompt xoxb-post-secret-123456")
        with (
            patch.object(web_channel, "_require_auth", return_value=None),
            patch.object(web_channel.SchedulerHandler, "_projection", side_effect=get_error),
        ):
            get_payload = json.loads(web_channel.SchedulerHandler().GET())
        with (
            patch.object(web_channel, "_require_auth", return_value=None),
            patch.object(web_channel.web, "data", return_value=json.dumps({"action": "start"}).encode("utf-8")),
            patch.object(web_channel.SchedulerHandler, "_mutation_blocked", return_value=""),
            patch.object(web_channel.SchedulerHandler, "_store", side_effect=post_error),
        ):
            post_payload = json.loads(web_channel.SchedulerHandler().POST())

        serialized = json.dumps({"get": get_payload, "post": post_payload}, ensure_ascii=False)
        self.assertNotIn("private scheduler GET prompt", serialized)
        self.assertNotIn("sk-get-secret", serialized)
        self.assertNotIn("private scheduler POST prompt", serialized)
        self.assertNotIn("xoxb-post-secret", serialized)
        self.assertEqual(get_payload["status"], "error")
        self.assertIn("Details redacted", get_payload["message"])
        self.assertTrue(get_payload["errorHash"])
        self.assertEqual(post_payload["status"], "error")
        self.assertIn("Details redacted", post_payload["message"])
        self.assertTrue(post_payload["errorHash"])

    def test_r23_09_public_api_fallback_payloads_redact_private_exceptions(self):
        install_web_stub()
        from channel.web import web_channel

        channel = web_channel.WebChannel()
        with (
            patch.object(web_channel, "_require_auth", return_value=None),
            patch.object(web_channel.web, "data", return_value=json.dumps({}).encode("utf-8")),
            patch.object(channel, "prepare_request_retry", side_effect=RuntimeError("private retry cookie sk-retry-public-123456")),
        ):
            retry_payload = json.loads(web_channel.RequestRetryPrepareHandler().POST("req-public-redact"))

        runtime_params = types.SimpleNamespace(
            request_id="req-public-redact",
            session_id="",
            after_event_id="0",
            limit="1000",
            include_events="",
            history_page="",
            page_size="20",
        )
        with (
            patch.object(web_channel, "_require_auth", return_value=None),
            patch.object(web_channel.web, "input", return_value=runtime_params),
            patch("agent.protocol.RuntimeProjectionService", side_effect=RuntimeError("private runtime bearer xoxb-runtime-public-123456")),
        ):
            runtime_payload = json.loads(web_channel.RuntimeProjectionHandler().GET())

        with (
            patch.object(web_channel, "_require_auth", return_value=None),
            patch.object(web_channel.ChannelsHandler, "GET", side_effect=RuntimeError("private external cookie sk-external-public-123456")),
        ):
            external_payload = json.loads(web_channel.ExternalConnectionsHandler().GET())

        with (
            patch.object(web_channel, "_require_auth", return_value=None),
            patch.object(web_channel.web, "data", return_value=json.dumps({"action": "test"}).encode("utf-8")),
            patch.object(web_channel.ExternalConnectionActionHandler, "_handle_test", side_effect=RuntimeError("private action token xoxb-action-public-123456")),
        ):
            action_payload = json.loads(web_channel.ExternalConnectionActionHandler().POST("feishu"))

        serialized = json.dumps({
            "retry": retry_payload,
            "runtime": runtime_payload,
            "external": external_payload,
            "action": action_payload,
        }, ensure_ascii=False)
        for raw in (
            "private retry cookie",
            "sk-retry-public",
            "private runtime bearer",
            "xoxb-runtime-public",
            "private external cookie",
            "sk-external-public",
            "private action token",
            "xoxb-action-public",
        ):
            self.assertNotIn(raw, serialized)
        for payload in (retry_payload, runtime_payload, external_payload, action_payload):
            self.assertEqual(payload["status"], "error")
            self.assertIn("Details redacted", payload["message"])
            self.assertTrue(payload["errorHash"])
            self.assertIn("errorType", payload)

    def test_run_center_stale_locks_use_redacted_frontend_contract(self):
        root = Path(__file__).resolve().parents[1]
        api_source = (root / "desktop" / "src" / "services" / "ecorexApi.ts").read_text(encoding="utf-8")
        app_source = (root / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
        console_source = (root / "channel" / "web" / "static" / "js" / "console.js").read_text(encoding="utf-8")

        self.assertIn("sessionHash?: string", api_source)
        self.assertIn("lockPath?:", api_source)
        self.assertIn("removeError?: boolean", api_source)
        self.assertIn("deadOwner?: boolean", api_source)

        self.assertIn("function staleLockKey", app_source)
        self.assertIn("function staleLockDisplayName", app_source)
        self.assertIn("function staleLockStatusLabel", app_source)
        self.assertIn("function canOpenStaleLockSession", app_source)
        self.assertIn("lock.sessionHash || lockPathHash", app_source)
        self.assertGreaterEqual(app_source.count('typeof lock.lockPath?.pathHash === "string" ? lock.lockPath.pathHash : ""'), 2)
        self.assertIn('return `lock ${String(lockPathHash).slice(0, 8)}`;', app_source)
        self.assertIn("lock.removeError ? \" · remove error\"", app_source)
        self.assertNotIn('key={`${lock.path || lock.session_id || "lock"}-${index}`}', app_source)
        self.assertNotIn('<strong>{lock.session_id || "session lock"}</strong>', app_source)
        self.assertNotIn('lock.dead_owner ? "dead owner" : "stale"}{formatRunAge', app_source)
        self.assertIn("function showChannelStatus(chName, msgKey, isError, messageText)", console_source)
        self.assertIn("el.textContent = messageText || t(msgKey)", console_source)
        self.assertEqual(console_source.count("showChannelStatus(chName, 'channels_save_error', true, data.message)"), 2)
        save_body = re.search(r"function saveChannelConfig\(chName\) \{(?P<body>.*?)\nfunction connectChannelConfig", console_source, re.DOTALL)
        connect_body = re.search(r"function connectChannelConfig\(chName\) \{(?P<body>.*?)\n// --- Add channel panel ---", console_source, re.DOTALL)
        self.assertIsNotNone(save_body)
        self.assertIsNotNone(connect_body)
        self.assertIn("showChannelStatus(chName, 'channels_save_error', true, data.message)", save_body.group("body"))
        self.assertIn("showChannelStatus(chName, 'channels_save_error', true, data.message)", connect_body.group("body"))
        self.assertIn('id="add-channel-status"', console_source)
        self.assertIn("function showAddChannelStatus(msgKey, isError, messageText)", console_source)
        self.assertIn("showAddChannelStatus('channels_save_error', true, data.message)", console_source)

    def test_static_web_app_bundle_uses_redacted_stale_lock_contract(self):
        root = Path(__file__).resolve().parents[1]
        app_dir = root / "channel" / "web" / "static" / "app"
        index_html = (app_dir / "index.html").read_text(encoding="utf-8")
        script_names = re.findall(r'<script[^>]+src="\./assets/([^"]+\.js)"', index_html)
        self.assertTrue(script_names)

        bundle = "\n".join((app_dir / "assets" / name).read_text(encoding="utf-8") for name in script_names)
        self.assertIn("sessionHash", bundle)
        self.assertIn("lockPath", bundle)
        self.assertIn("removeError", bundle)
        self.assertIn("Stale lock details are redacted", bundle)
        self.assertIn("Stale lock session id is redacted", bundle)
        self.assertIn("lockPath?.pathHash", bundle)
        self.assertIn("lock ${String", bundle)
        self.assertNotIn('session_id||"session lock"', bundle)

    def test_scheduler_fallback_logs_redact_private_exception_text(self):
        import io
        import logging

        install_web_stub()
        from agent.tools.scheduler import integration
        from bridge.agent_bridge import AgentBridge
        from bridge.context import Context, ContextType
        from channel import messaging_adapter_contract as contract
        from channel.web import web_channel
        from common.log import logger

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            with patch("common.ecorex_tool_permissions.get_tool_permission_broker", side_effect=RuntimeError("private web permission sk-web-log-secret-123456")):
                self.assertIn("Permission broker unavailable", web_channel.SchedulerHandler._mutation_blocked())

            with patch("agent.tools.scheduler.integration.get_scheduler_service", side_effect=RuntimeError("private web stop xoxb-web-stop-secret-123456")):
                web_channel.SchedulerHandler._stop_runtime()

            with patch.object(contract, "probe_messaging_adapter", side_effect=RuntimeError("private readiness sk-readiness-secret-123456")):
                self.assertFalse(integration._is_channel_ready("slack", "C1"))

            with patch("common.ecorex_tool_permissions.get_tool_permission_broker", side_effect=RuntimeError("private scheduler permission sk-scheduler-log-secret-123456")):
                self.assertFalse(integration._authorize_scheduled_execution({"id": "task-log", "action": {"type": "agent_task"}}))
                self.assertFalse(integration._authorize_scheduled_tool_call(object(), "tool-log", {}, {"id": "task-tool-log"}))

            with patch("common.ecorex_tool_permissions.get_tool_permission_broker") as broker:
                broker.return_value.authorize_noninteractive.return_value = {
                    "allowed": False,
                    "reason": "private denied reason xoxb-denied-secret-123456",
                }
                self.assertFalse(integration._authorize_scheduled_execution({"id": "task-denied", "action": {"type": "agent_task"}}))

            with (
                patch.object(integration, "conf", return_value={"scheduler_enabled": True}),
                patch("bridge.bridge.Bridge") as bridge_cls,
            ):
                bridge_cls.return_value.get_agent_bridge.side_effect = RuntimeError("private bridge init sk-bridge-secret-123456")
                self.assertFalse(integration.ensure_scheduler_runtime())

            with (
                patch("bridge.agent_bridge.conf", return_value={"scheduler_enabled": True, "self_evolution_enabled": False}),
                patch("agent.tools.scheduler.integration.init_scheduler", side_effect=RuntimeError("private eager scheduler init sk-agentbridge-init-123456")),
            ):
                bridge_instance = AgentBridge(object())
                self.assertFalse(bridge_instance.scheduler_initialized)

            class FakeSchedulerTool:
                name = "scheduler"

            class FakeAgent:
                def __init__(self):
                    self.tools = [FakeSchedulerTool()]
                    self.model = types.SimpleNamespace()
                    self.messages = [{"role": "assistant", "content": "ok"}]
                    self.messages_lock = threading.Lock()
                    self._last_run_new_messages = []
                    self.stream_executor = types.SimpleNamespace(files_to_send=[])

                def run_stream(self, **_kwargs):
                    return "ok"

            attach_bridge = AgentBridge.__new__(AgentBridge)
            attach_bridge.get_agent = lambda session_id=None: FakeAgent()
            attach_bridge._pre_persist_user_message = lambda *_args, **_kwargs: False
            attach_bridge._persist_messages = lambda *_args, **_kwargs: None
            attach_bridge._schedule_mcp_hot_reload = lambda *_args, **_kwargs: None
            attach_context = Context(ContextType.TEXT, "hello")
            attach_context.kwargs = {"session_id": "session-agentbridge-attach"}
            with patch("agent.tools.scheduler.integration.attach_scheduler_to_tool", side_effect=RuntimeError("private attach scheduler xoxb-agentbridge-attach-123456")):
                reply = attach_bridge.agent_reply("hello", attach_context)
                self.assertEqual(str(reply.type), "TEXT")

            with patch("agent.protocol.get_run_ledger", side_effect=RuntimeError("private ledger create sk-ledger-create-secret-123456")):
                integration._mark_scheduler_run_created({"id": "task-ledger-create", "action": {}}, "req-ledger-create")
            with patch("agent.protocol.get_run_ledger", side_effect=RuntimeError("private ledger phase xoxb-ledger-phase-secret-123456")):
                integration._mark_scheduler_run_phase("req-ledger-phase", "authorizing")
            with patch("agent.protocol.get_run_ledger", side_effect=RuntimeError("private ledger terminal sk-ledger-terminal-secret-123456")):
                integration._mark_scheduler_run_terminal("req-ledger-terminal", "failed")
            with (
                patch.object(integration, "_is_channel_ready", return_value=True),
                patch.object(integration, "_authorize_scheduled_execution", return_value=False),
                patch("agent.protocol.get_cancel_registry", side_effect=RuntimeError("private cancel token xoxb-cancel-secret-123456")),
            ):
                integration._execute_scheduled_task({
                    "id": "task-cancel-log",
                    "action": {
                        "type": "agent_task",
                        "channel_type": "web",
                        "receiver": "receiver",
                        "task_description": "safe",
                    },
                }, object())

            channel = web_channel.WebChannel()

            class BadLock:
                def __init__(self, text):
                    self.text = text

                def release(self):
                    raise RuntimeError(self.text)

            with patch("agent.protocol.get_run_ledger", side_effect=RuntimeError("private web ledger phase sk-web-ledger-phase-123456")):
                channel._mark_run_phase("req-web-phase", "running")
            with patch("agent.protocol.get_run_ledger", side_effect=RuntimeError("private web ledger terminal xoxb-web-ledger-terminal-123456")):
                channel._mark_run_terminal("req-web-terminal", "failed")

            lock_context = Context(ContextType.TEXT, "hello")
            lock_context.kwargs = {"session_lock": BadLock("private web session lock sk-web-lock-123456")}
            channel._release_context_session_lock(lock_context)

            channel._ensure_sse_state("req-web-pre-log")
            with (
                patch.object(channel, "_push_error_event_once", side_effect=RuntimeError("private web pre sse sk-web-pre-sse-123456")),
                patch("agent.protocol.get_cancel_registry", side_effect=RuntimeError("private web pre token xoxb-web-pre-token-123456")),
            ):
                channel._abort_pre_worker_request(
                    "req-web-pre-log",
                    "session-web-pre-log",
                    message="public",
                    reason="test",
                    error_code="TEST",
                    session_lock=BadLock("private web pre lock sk-web-pre-lock-123456"),
                )

            channel._ensure_sse_state("req-web-worker-log")
            worker_context = Context(ContextType.TEXT, "hello")
            worker_context.kwargs = {"request_id": "req-web-worker-log", "session_id": "session-web-worker-log"}
            with (
                patch("agent.protocol.get_cancel_registry", side_effect=RuntimeError("private web worker cancel xoxb-web-worker-cancel-123456")),
                patch.object(channel, "_push_error_event_once", side_effect=RuntimeError("private web worker sse sk-web-worker-sse-123456")),
            ):
                channel._finalize_request_after_worker(worker_context, RuntimeError("public worker failure"))

            produce_context = Context(ContextType.TEXT, "hello")
            with patch.object(channel, "produce", side_effect=RuntimeError("private web produce sk-web-produce-123456")):
                channel._produce_with_session_lock(
                    produce_context,
                    BadLock("private web produce lock xoxb-web-produce-lock-123456"),
                )

            with patch("agent.protocol.get_cancel_registry", side_effect=RuntimeError("private active lookup sk-active-lookup-123456")):
                self.assertEqual(channel._active_request_ids_for_session("session-active-log"), [])

            with patch("common.ecorex_workspace.list_session_locks", side_effect=RuntimeError("private dead lock scan xoxb-dead-scan-123456")):
                self.assertEqual(channel._recover_interrupted_runs_for_removed_session_locks("session-dead-log"), [])

            with (
                patch("common.ecorex_workspace.list_session_locks", return_value=[{
                    "session_id": "session-dead-log",
                    "dead_owner": True,
                    "path": str(Path(tempfile.gettempdir()) / "dead-lock-log.lock"),
                }]),
                patch.object(web_channel.Path, "unlink", side_effect=RuntimeError("private dead lock unlink sk-dead-unlink-123456")),
            ):
                self.assertEqual(channel._recover_interrupted_runs_for_removed_session_locks("session-dead-log"), [])

            with patch("agent.protocol.get_cancel_registry", side_effect=RuntimeError("private backpressure xoxb-backpressure-123456")):
                snapshot = channel._backpressure_snapshot("session-backpressure-log")
                self.assertEqual(snapshot["session_id"], "session-backpressure-log")

            class EmptyLedger:
                def active_snapshot(self):
                    return []

            with patch("common.ecorex_workspace.list_session_locks", side_effect=RuntimeError("private stale lock sk-stale-lock-123456")):
                self.assertEqual(channel._recover_stale_active_runs(EmptyLedger(), registry_by_request={}), [])

            with patch("agent.protocol.get_run_ledger", side_effect=RuntimeError("private sidecar recovery xoxb-sidecar-123456")):
                self.assertIsNone(channel._recover_sidecar_interrupted_stream_event("req-sidecar-log"))

            with patch("agent.protocol.RuntimeProjectionService", side_effect=RuntimeError("private owner lookup sk-owner-lookup-123456")):
                self.assertEqual(channel._request_session_mismatch_event("req-owner-log", "session-owner-log"), {})

            class BadQueue:
                def put(self, _event):
                    raise RuntimeError("private sse mirror xoxb-sse-mirror-123456")

            channel._ensure_sse_state("req-sse-mirror-log")
            channel.sse_queues["req-sse-mirror-log"] = BadQueue()
            channel._push_sse_event("req-sse-mirror-log", {"type": "delta", "content": "safe"})

            with patch("agent.memory.get_conversation_store", side_effect=RuntimeError("private latest seq sk-latest-seq-123456")):
                self.assertEqual(channel._fetch_latest_pair_seqs("session-latest-seq-log"), {"user_seq": None, "bot_seq": None})

            with patch("bridge.bridge.Bridge", side_effect=RuntimeError("private usage xoxb-usage-123456")):
                self.assertIsNone(channel._fetch_agent_usage("session-usage-log"))

            channel.request_artifacts["req-artifact-log"] = [{"kind": "file", "path": "safe.txt"}]
            with patch("agent.memory.get_conversation_store", side_effect=RuntimeError("private artifact persist sk-artifact-persist-123456")):
                channel._persist_request_artifacts("req-artifact-log", "session-artifact-log")

            with (
                patch.object(channel, "_persist_request_artifacts", return_value=None),
                patch.object(channel, "_fetch_latest_pair_seqs", return_value={"user_seq": 1, "bot_seq": 2}),
                patch("agent.memory.get_conversation_store", side_effect=RuntimeError("private identity persist xoxb-identity-persist-123456")),
            ):
                done_event = channel._build_done_event("req-identity-log", "session-identity-log", "safe")
                self.assertEqual(done_event["request_id"], "req-identity-log")

            with patch("agent.protocol.get_cancel_registry", side_effect=RuntimeError("private active snapshot sk-active-snapshot-123456")):
                active_payload = channel.active_requests_snapshot()
                active_serialized = json.dumps(active_payload, ensure_ascii=False)
                self.assertNotIn("sk-active-snapshot", active_serialized)
                self.assertIn("Details redacted", active_payload["message"])

            with patch("agent.protocol.get_run_event_ledger", side_effect=RuntimeError("private diagnostic runtime xoxb-diagnostic-runtime-123456")):
                diagnostic_payload = web_channel._diagnostic_runtime_events_payload()
                self.assertEqual(diagnostic_payload["status"], "error")

            with patch("common.ecorex_capability_policy.load_capability_policy", side_effect=RuntimeError("private diagnostic policy sk-diagnostic-policy-123456")):
                policy_payload = web_channel._diagnostic_capability_policy_payload()
                self.assertEqual(policy_payload["status"], "error")

            with patch("agent.tools.host_diagnostics.host_diagnostics._candidate_log_paths", side_effect=RuntimeError("private log path xoxb-log-path-123456")):
                resolved_log = web_channel._resolve_run_log_path(Path(tempfile.gettempdir()))
                self.assertEqual(resolved_log.name, "run.log")

            with patch("agent.tools.tool_manager.ToolManager", side_effect=RuntimeError("private tool snapshot sk-tool-snapshot-123456")):
                self.assertIsNone(web_channel.ChannelsHandler._agent_tool_names())

            with patch("bridge.bridge.Bridge", side_effect=RuntimeError("private runtime refresh xoxb-runtime-refresh-123456")):
                web_channel.ChannelsHandler._refresh_runtime_capabilities("test")

            with (
                patch.object(channel, "_runtime_event_ledger_enabled", return_value=True),
                patch("agent.protocol.get_run_event_ledger", side_effect=RuntimeError("private event append sk-event-append-123456")),
            ):
                append_result = channel._append_runtime_event("req-event-append-log", event_type="run.failed", payload={"safe": True})
                self.assertTrue(append_result["append_failed"])
                failure_tail = channel.runtime_event_append_failure_tail[-1]
                self.assertTrue(failure_tail["redacted"])
                self.assertNotIn("error_message", failure_tail)
                self.assertTrue(failure_tail["error_hash"])

            fake_registry = types.SimpleNamespace(snapshot=lambda: [])
            fake_ledger = types.SimpleNamespace(
                active_snapshot=lambda: [],
                terminal_snapshot=lambda **_kwargs: [],
                get_run=lambda _request_id: None,
            )
            raw_lock_path = str(Path(tempfile.gettempdir()) / "private-sk-lock-path.lock")
            with (
                patch("agent.protocol.get_cancel_registry", return_value=fake_registry),
                patch("agent.protocol.get_run_ledger", return_value=fake_ledger),
                patch("common.ecorex_workspace.list_session_locks", return_value=[{
                    "session_id": "session-private-stale-sk-123456",
                    "pid": 999999,
                    "dead_owner": True,
                    "stale": True,
                    "path": raw_lock_path,
                }]),
                patch.object(web_channel.Path, "unlink", side_effect=RuntimeError("private stale remove xoxb-stale-remove-123456")),
            ):
                stale_payload = channel.active_requests_snapshot()
                stale_serialized = json.dumps(stale_payload, ensure_ascii=False)
                self.assertNotIn("session-private-stale", stale_serialized)
                self.assertNotIn("private-sk-lock-path", stale_serialized)
                self.assertNotIn("xoxb-stale-remove", stale_serialized)
                self.assertEqual(len(stale_payload["staleLocks"]), 1)
                self.assertTrue(stale_payload["staleLocks"][0]["sessionHash"])
                self.assertTrue(stale_payload["staleLocks"][0]["lockPath"]["redacted"])
                self.assertTrue(stale_payload["staleLocks"][0]["removeError"])

            with patch("agent.tools.subagent.subagent.cancel_children_for_default_workspace", side_effect=RuntimeError("private subagent cancel sk-subagent-cancel-123456")):
                subagent_cancel = channel._cancel_subagents_for_parent("session-subagent-cancel-log")
                subagent_serialized = json.dumps(subagent_cancel, ensure_ascii=False)
                self.assertNotIn("sk-subagent-cancel", subagent_serialized)
                self.assertIn("Details redacted", subagent_cancel["error"])
                self.assertTrue(subagent_cancel["errorHash"])

            with patch("agent.tools.subagent.subagent.interrupt_orphan_task", side_effect=RuntimeError("private subagent orphan xoxb-subagent-orphan-123456")):
                orphan_result = channel._interrupt_orphan_subagent_state(
                    {"metadata": {"task_id": "task-safe"}, "request_id": "subagent-safe"},
                    reason="test",
                    error_code="TEST",
                    error_message="safe",
                )
                orphan_serialized = json.dumps(orphan_result, ensure_ascii=False)
                self.assertNotIn("xoxb-subagent-orphan", orphan_serialized)
                self.assertIn("Details redacted", orphan_result["error"])
                self.assertTrue(orphan_result["errorHash"])

            with tempfile.TemporaryDirectory() as raw_log_dir:
                log_path = Path(raw_log_dir) / "run.log"
                log_path.write_text("safe\n", encoding="utf-8")
                previous_env = dict(getattr(web_channel.web.ctx, "env", {}) or {})
                web_channel.web.ctx.env = {"HTTP_ACCEPT": "text/event-stream"}
                try:
                    with (
                        patch.object(web_channel, "_require_auth", return_value=None),
                        patch.object(web_channel, "_resolve_run_log_path", return_value=log_path),
                        patch("agent.tools.host_diagnostics.host_diagnostics._tail_text", side_effect=RuntimeError("private log stream sk-log-stream-123456")),
                    ):
                        stream_payload = b"".join(web_channel.LogsHandler().GET()).decode("utf-8", errors="replace")
                finally:
                    web_channel.web.ctx.env = previous_env
                self.assertNotIn("sk-log-stream", stream_payload)
                self.assertIn("Details redacted", stream_payload)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

        logs = stream.getvalue()
        self.assertNotIn("private web permission", logs)
        self.assertNotIn("sk-web-log-secret", logs)
        self.assertNotIn("xoxb-web-stop-secret", logs)
        self.assertNotIn("sk-readiness-secret", logs)
        self.assertNotIn("sk-scheduler-log-secret", logs)
        self.assertNotIn("xoxb-denied-secret", logs)
        self.assertNotIn("sk-bridge-secret", logs)
        self.assertNotIn("sk-agentbridge-init", logs)
        self.assertNotIn("xoxb-agentbridge-attach", logs)
        self.assertNotIn("sk-ledger-create-secret", logs)
        self.assertNotIn("xoxb-ledger-phase-secret", logs)
        self.assertNotIn("sk-ledger-terminal-secret", logs)
        self.assertNotIn("xoxb-cancel-secret", logs)
        self.assertNotIn("sk-web-ledger-phase", logs)
        self.assertNotIn("xoxb-web-ledger-terminal", logs)
        self.assertNotIn("sk-web-lock", logs)
        self.assertNotIn("sk-web-pre-sse", logs)
        self.assertNotIn("xoxb-web-pre-token", logs)
        self.assertNotIn("sk-web-pre-lock", logs)
        self.assertNotIn("xoxb-web-worker-cancel", logs)
        self.assertNotIn("sk-web-worker-sse", logs)
        self.assertNotIn("sk-web-produce", logs)
        self.assertNotIn("xoxb-web-produce-lock", logs)
        for token in (
            "sk-active-lookup",
            "xoxb-dead-scan",
            "sk-dead-unlink",
            "xoxb-backpressure",
            "sk-stale-lock",
            "xoxb-sidecar",
            "sk-owner-lookup",
            "xoxb-sse-mirror",
            "sk-latest-seq",
            "xoxb-usage",
            "sk-artifact-persist",
            "xoxb-identity-persist",
            "sk-active-snapshot",
            "xoxb-diagnostic-runtime",
            "sk-diagnostic-policy",
            "xoxb-log-path",
            "sk-tool-snapshot",
            "xoxb-runtime-refresh",
            "sk-event-append",
            "xoxb-stale-remove",
            "session-private-stale",
            "private-sk-lock-path",
            "sk-subagent-cancel",
            "xoxb-subagent-orphan",
            "sk-log-stream",
        ):
            self.assertNotIn(token, logs)
        self.assertIn("hash", logs)

        web_source = (Path(__file__).resolve().parents[1] / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
        self.assertNotIn("conflict session lock release skipped: {e}", web_source)
        self.assertNotIn("backpressure session lock release skipped: {e}", web_source)
        self.assertNotIn("pre-register cancel token skipped: {e}", web_source)
        self.assertNotIn('run ledger unavailable for {request_id}: {e}", exc_info=True', web_source)
        self.assertNotIn("session lock release skipped: {e}", web_source)
        self.assertNotIn("logger.exception", web_source)
        self.assertNotIn("exc_info=True", web_source)
        self.assertNotRegex(web_source, r"logger\.(?:debug|warning|error)\(f[\"'][^\n]*\{(?:e|exc)\}")
        self.assertNotRegex(web_source, r"return json\.dumps\(\{\"status\": \"error\", \"message\": str\((?:e|exc)\)")
        self.assertNotIn('"detail": str(exc)', web_source)
        self.assertNotIn('"error": str(exc)', web_source)
        self.assertNotIn('{"error": str(e)}', web_source)
        self.assertNotIn('message": "{e}"', web_source)

    def test_external_connection_projection_includes_backend_adapter_contract(self):
        install_web_stub()
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as raw_workspace:
            config_path = Path(raw_workspace) / "config.json"
            config_path.write_text(json.dumps({
                "channel_type": "web,feishu",
                "feishu_app_id": "cli-existing",
                "feishu_app_secret": "existing-secret",
            }), encoding="utf-8")
            fake_conf = {
                "channel_type": "web,feishu",
                "feishu_app_id": "cli-existing",
                "feishu_app_secret": "existing-secret",
            }
            with (
                patch.object(web_channel, "_require_auth", return_value=None),
                patch.object(web_channel, "conf", return_value=fake_conf),
                patch.object(web_channel.ChannelsHandler, "_config_path", return_value=str(config_path)),
                patch.object(web_channel.ChannelsHandler, "_channel_runtime_observations", return_value={}),
                patch.object(web_channel.ChannelsHandler, "_agent_tool_names", return_value=set()),
            ):
                projection = json.loads(web_channel.ExternalConnectionsHandler().GET())

        feishu = next(item for item in projection["connections"] if item["id"] == "feishu")
        contract = feishu["adapterContract"]
        self.assertEqual(contract["version"], "ecorex.messaging_adapter.v1")
        self.assertEqual(contract["ingress"]["entrypoint"], "ChatChannel.produce")
        self.assertFalse(contract["ingress"]["usesHermesActiveSessionQueue"])
        self.assertTrue(contract["projection"]["backendCanonical"])

    def test_external_connection_test_is_projection_dry_run_not_remote_pass(self):
        install_web_stub()
        from channel.web import web_channel

        fake_conf = {
            "channel_type": "web,feishu",
            "feishu_app_id": "cli-existing",
            "feishu_app_secret": "existing-secret",
        }
        with (
            patch.object(web_channel, "_require_auth", return_value=None),
            patch.object(web_channel, "conf", return_value=fake_conf),
            patch.object(web_channel.ChannelsHandler, "_channel_runtime_observations", return_value={}),
            patch.object(web_channel.ChannelsHandler, "_agent_tool_names", return_value=set()),
        ):
            payload = json.loads(web_channel.ExternalConnectionActionHandler._handle_test("feishu"))

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["test"]["status"], "success")
        self.assertEqual(payload["test"]["mode"], "projection_dry_run")
        self.assertFalse(payload["test"]["remoteConnectivityProbed"])
        self.assertEqual(payload["adapter"]["testMode"], "projection_dry_run")
        self.assertFalse(payload["adapter"]["remoteConnectivityProbed"])

    def test_feishu_external_connection_test_blocks_missing_credentials(self):
        install_web_stub()
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests
        from channel import messaging_adapter_contract as contract
        from channel.web import web_channel

        class ReadyEvent:
            def is_set(self):
                return True

        class StaleLiveChannel:
            _startup_event = ReadyEvent()
            _startup_error = ""

        class StaleManager:
            def get_channel(self, channel_name):
                return StaleLiveChannel() if channel_name == "feishu" else None

        fake_conf = {
            "channel_type": "web,feishu",
            "feishu_app_id": "",
            "feishu_app_secret": "",
        }
        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "feishu-missing-credential-test.db")
            with (
                patch.object(web_channel, "_require_auth", return_value=None),
                patch.object(web_channel, "conf", return_value=fake_conf),
                patch.object(web_channel.ChannelsHandler, "_channel_runtime_observations", return_value={
                    "feishu": {"running": True, "status": "active", "last_error": ""},
                }),
                patch.object(web_channel.ChannelsHandler, "_agent_tool_names", return_value={"feishu_cli"}),
                patch.object(contract, "_default_manager", return_value=StaleManager()),
            ):
                payload = json.loads(web_channel.ExternalConnectionActionHandler._handle_test("feishu"))
                external_payload = json.loads(web_channel.ExternalConnectionsHandler().GET())
            projection = RuntimeProjectionService(ledger).external_connections_projection(limit=0)

        reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-feishu-missing-credential-reset.db")

        serialized = json.dumps({"payload": payload, "projection": projection}, ensure_ascii=False)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["test"]["status"], "blocked")
        self.assertFalse(payload["connection"]["configured"])
        self.assertEqual(payload["connection"]["status"], "blocked")
        self.assertFalse(payload["adapter"]["configured"])
        self.assertFalse(payload["adapter"]["running"])
        self.assertEqual(payload["adapter"]["readiness"], "not_configured")
        self.assertFalse(payload["adapter"]["safeToSend"])
        self.assertEqual(payload["adapter"]["reason"], "channel is not configured")
        external_feishu = next(item for item in external_payload["connections"] if item["id"] == "feishu")
        self.assertFalse(external_feishu["configured"])
        self.assertEqual(external_feishu["status"], "blocked")
        self.assertFalse(external_feishu["running"])
        self.assertFalse(external_feishu["connected"])
        self.assertEqual(external_feishu["configState"], "missing")
        self.assertFalse(external_feishu["adapterContract"]["readiness"]["configured"])
        self.assertFalse(external_feishu["adapterContract"]["readiness"]["running"])
        projected = {
            item["platform"]: item
            for item in projection.get("external_connections") or []
        }
        self.assertEqual(projected["feishu"]["lastAction"], "test")
        self.assertEqual(projected["feishu"]["status"], "blocked")
        self.assertFalse(projected["feishu"]["adapter"]["running"])
        self.assertNotIn("existing-secret", serialized)

        disabled_conf = {
            "channel_type": "web",
            "feishu_app_id": "",
            "feishu_app_secret": "",
        }
        with tempfile.TemporaryDirectory() as disabled_root:
            disabled_ledger = reset_run_event_ledger_for_tests(Path(disabled_root) / "feishu-disabled-stale-test.db")
            with (
                patch.object(web_channel, "_require_auth", return_value=None),
                patch.object(web_channel, "conf", return_value=disabled_conf),
                patch.object(web_channel.ChannelsHandler, "_channel_runtime_observations", return_value={
                    "feishu": {"running": True, "status": "active", "last_error": ""},
                }),
                patch.object(web_channel.ChannelsHandler, "_agent_tool_names", return_value={"feishu_cli"}),
                patch.object(contract, "_default_manager", return_value=StaleManager()),
            ):
                disabled_payload = json.loads(web_channel.ExternalConnectionActionHandler._handle_test("feishu"))
                disabled_external_payload = json.loads(web_channel.ExternalConnectionsHandler().GET())
            disabled_projection = RuntimeProjectionService(disabled_ledger).external_connections_projection(limit=0)
        disabled_feishu = next(item for item in disabled_external_payload["connections"] if item["id"] == "feishu")
        self.assertFalse(disabled_feishu["enabled"])
        self.assertFalse(disabled_feishu["configured"])
        self.assertEqual(disabled_feishu["status"], "blocked")
        self.assertFalse(disabled_feishu["running"])
        self.assertFalse(disabled_feishu["connected"])
        self.assertEqual(disabled_feishu["adapterContract"]["readiness"]["status"], "blocked")
        self.assertFalse(disabled_payload["adapter"]["running"])
        disabled_projected = {
            item["platform"]: item
            for item in disabled_projection.get("external_connections") or []
        }
        self.assertFalse(disabled_projected["feishu"]["adapter"]["running"])

    def test_external_connection_frontend_marks_projection_dry_run_status_check(self):
        app_source = Path("desktop/src/App.tsx").read_text(encoding="utf-8")
        api_source = Path("desktop/src/services/ecorexApi.ts").read_text(encoding="utf-8")
        backend_source = Path("channel/web/web_channel.py").read_text(encoding="utf-8")
        static_js = "\n".join(
            path.read_text(encoding="utf-8")
            for path in Path("channel/web/static/app/assets").glob("index-*.js")
        )

        self.assertIn("状态检查", app_source)
        self.assertIn("未探测远端连通", app_source)
        self.assertIn("需授权", app_source)
        self.assertNotIn('action === "test" ? "测试完成"', app_source)
        available_guard_index = app_source.index('if (status === "available") return false;')
        needs_auth_index = app_source.index('if (externalConnectionNeedsAuthorization(connection)) return "需授权";')
        needs_config_index = app_source.index('if (externalConnectionNeedsConfiguration(connection)) return "需配置";')
        enabled_index = app_source.index('if (connection.enabled) return "已启用";')
        self.assertLess(available_guard_index, app_source.index('configState === "missing"'))
        self.assertLess(needs_auth_index, enabled_index)
        self.assertLess(needs_config_index, enabled_index)
        self.assertIn('externalConnectionNeedsAuthorization(connection)', app_source)
        self.assertIn('if (externalConnectionNeedsConfiguration(connection) || externalConnectionNeedsAuthorization(connection)) return "blocked";', app_source)
        self.assertIn('className={`external-connection-card is-${cardState}`}', app_source)
        self.assertIn("ExternalConnectionActionResponse", api_source)
        self.assertIn("adapterContract?: ExternalConnectionAdapterContract", api_source)
        self.assertIn("remoteConnectivityProbed?: boolean", api_source)
        self.assertIn('"test", "label": "状态检查"', backend_source)
        self.assertIn("需授权", static_js)
        self.assertIn("auth_required", static_js)
        self.assertIn("需配置", static_js)
        self.assertIn("not_configured", static_js)

    def test_r23_existing_platform_batch_projects_metadata_logos_and_honest_readiness(self):
        install_web_stub()
        from channel.web import web_channel

        fake_conf = {
            "channel_type": "web,weixin,dingtalk,wecom,qq,wechatcom_app,wechat_kf,wechatmp,wechatmp_service,telegram,slack,discord",
        }
        with (
            patch.object(web_channel, "_require_auth", return_value=None),
            patch.object(web_channel, "conf", return_value=fake_conf),
            patch.object(web_channel.ChannelsHandler, "_channel_runtime_observations", return_value={}),
            patch.object(web_channel.ChannelsHandler, "_agent_tool_names", return_value=set()),
        ):
            payload = json.loads(web_channel.ExternalConnectionsHandler().GET())

        by_id = {item["id"]: item for item in payload["connections"]}
        expected_logos = {
            "weixin": "wechat",
            "dingtalk": "dingtalk",
            "wecom_bot": "wecom",
            "wechatcom_app": "wecom",
            "qq": "qq",
            "wechat_kf": "wechat",
            "wechatmp": "wechat",
            "wechatmp_service": "wechat",
            "telegram": "telegram",
            "slack": "slack",
            "discord": "discord",
        }
        self.assertTrue(set(expected_logos).issubset(set(by_id)))
        for platform, logo_key in expected_logos.items():
            self.assertEqual(by_id[platform]["logo"]["key"], logo_key)
            self.assertFalse(by_id[platform]["running"], platform)
            self.assertFalse(by_id[platform]["connected"], platform)

        weixin = by_id["weixin"]
        self.assertTrue(weixin["enabled"])
        self.assertFalse(weixin["configured"])
        self.assertEqual(weixin["status"], "auth_required")
        self.assertEqual(weixin["configState"], "auth_required")
        self.assertEqual(weixin["auth"]["mode"], "qr_login")
        self.assertEqual(weixin["auth"]["authEndpoint"], "/api/weixin/qrlogin")
        self.assertTrue(weixin["auth"]["runtimeAuthorizationRequired"])
        self.assertFalse(weixin["auth"]["runtimeAuthorized"])
        self.assertEqual(weixin["fields"], [])

        credential_platforms = set(expected_logos) - {"weixin"}
        for platform in sorted(credential_platforms):
            connection = by_id[platform]
            self.assertTrue(connection["enabled"], platform)
            self.assertFalse(connection["configured"], platform)
            self.assertEqual(connection["status"], "blocked", platform)
            self.assertEqual(connection["configState"], "missing", platform)
            self.assertEqual(connection["auth"]["statusProbe"], "credential_configured_only", platform)
            self.assertTrue(connection["fields"], platform)
            self.assertFalse(connection["adapterContract"]["readiness"]["configured"], platform)
            self.assertEqual(connection["adapterContract"]["readiness"]["status"], "blocked", platform)

    def test_messaging_adapter_probe_blocks_missing_platform_context(self):
        from bridge.context import Context, ContextType
        from channel import messaging_adapter_contract as contract

        cfg = {
            "channel_type": "slack,telegram,discord",
            "slack_bot_token": "xoxb-valid-token",
            "slack_app_token": "xapp-valid-token",
            "telegram_token": "123456789:ABCDEFGHIJKLMNOPQRST_uv",
            "discord_token": "discord-token",
        }
        slack = contract.probe_messaging_adapter("slack", config=cfg, receiver="C1")
        telegram = contract.probe_messaging_adapter("telegram", config=cfg, receiver="123")
        discord = contract.probe_messaging_adapter("discord", config=cfg, receiver="456")

        tg_context = Context(ContextType.TEXT, "hello")
        tg_context.kwargs = {"channel_type": "telegram", "receiver": "123", "telegram_chat_id": "123"}
        telegram_ready = contract.probe_messaging_adapter("telegram", config=cfg, context=tg_context)

        self.assertFalse(slack["safeToSend"])
        self.assertIn("slack_channel", slack["missingContext"])
        self.assertFalse(telegram["safeToSend"])
        self.assertIn("telegram_chat_id", telegram["missingContext"])
        self.assertFalse(discord["safeToSend"])
        self.assertIn("discord_channel_id", discord["missingContext"])
        self.assertTrue(telegram_ready["safeToSend"])
        self.assertFalse(telegram_ready["usesHermesActiveSessionQueue"])

    def test_messaging_adapter_probe_blocks_disabled_and_bad_startup_states(self):
        from channel import messaging_adapter_contract as contract

        disabled_cfg = {
            "channel_type": "web",
            "feishu_app_id": "cli-existing",
            "feishu_app_secret": "existing-secret",
        }
        disabled = contract.probe_messaging_adapter("feishu", config=disabled_cfg, receiver="oc_home")
        self.assertFalse(disabled["safeToSend"])
        self.assertEqual(disabled["reason"], "channel is not enabled")

        missing_credential_cfg = {
            "channel_type": "web,feishu",
            "feishu_app_id": "",
            "feishu_app_secret": "",
        }
        missing_credential = contract.probe_messaging_adapter("feishu", config=missing_credential_cfg, receiver="oc_home")
        self.assertFalse(missing_credential["configured"])
        self.assertEqual(missing_credential["readiness"], "not_configured")
        self.assertFalse(missing_credential["safeToSend"])
        self.assertEqual(missing_credential["reason"], "channel is not configured")

        class ErrorChannel:
            _startup_error = "bad token sk-should-mask-123456"
            _startup_event = None

        class FakeThread:
            def is_alive(self):
                return True

        class StartingChannel:
            def __init__(self):
                self._startup_error = ""
                self._startup_event = threading.Event()

        class Manager:
            def __init__(self, channel, thread=None):
                self.channel = channel
                self._threads = {"feishu": thread} if thread else {}

            def get_channel(self, _name):
                return self.channel

        active_cfg = {
            "channel_type": "web,feishu",
            "feishu_app_id": "cli-existing",
            "feishu_app_secret": "existing-secret",
        }
        errored = contract.probe_messaging_adapter("feishu", config=active_cfg, manager=Manager(ErrorChannel()), receiver="oc_home")
        starting = contract.probe_messaging_adapter("feishu", config=active_cfg, manager=Manager(StartingChannel(), FakeThread()), receiver="oc_home")

        self.assertFalse(errored["safeToSend"])
        self.assertNotIn("sk-should-mask", json.dumps(errored))
        self.assertFalse(starting["safeToSend"])
        self.assertEqual(starting["readiness"], "starting")

    def test_scheduler_channel_readiness_uses_adapter_probe_without_creating_channel(self):
        from agent.tools.scheduler import integration
        from channel import messaging_adapter_contract as contract

        cfg = {
            "channel_type": "slack",
            "slack_bot_token": "xoxb-valid-token",
            "slack_app_token": "xapp-valid-token",
        }
        with (
            patch.object(contract, "conf", return_value=cfg),
            patch("channel.channel_factory.create_channel", side_effect=AssertionError("must not create channel")),
        ):
            ready = integration._is_channel_ready("slack", "C1")

        self.assertFalse(ready)

        disabled_cfg = {
            "channel_type": "web",
            "feishu_app_id": "cli-existing",
            "feishu_app_secret": "existing-secret",
        }
        with (
            patch.object(contract, "conf", return_value=disabled_cfg),
            patch("channel.channel_factory.create_channel", side_effect=AssertionError("must not create channel")),
        ):
            disabled_ready = integration._is_channel_ready("feishu", "oc_home")

        self.assertFalse(disabled_ready)

    def test_scheduler_web_readiness_treats_live_web_thread_as_ready(self):
        from agent.tools.scheduler import integration
        from channel import messaging_adapter_contract as contract

        class WebStyleChannel:
            def __init__(self):
                self._startup_error = ""
                self._startup_event = threading.Event()

        class AliveThread:
            def is_alive(self):
                return True

        class Manager:
            _threads = {"web": AliveThread()}

            def get_channel(self, name):
                self.requested = name
                return WebStyleChannel()

        with (
            patch.object(contract, "_default_manager", return_value=Manager()),
            patch.object(contract, "conf", return_value={"channel_type": "web"}),
            patch("channel.channel_factory.create_channel", side_effect=AssertionError("must not create channel")),
        ):
            ready = integration._is_channel_ready("web", "web-session-1")
            probe = contract.probe_messaging_adapter("web", manager=Manager(), config={"channel_type": "web"}, receiver="web-session-1")

        self.assertTrue(ready)
        self.assertEqual(probe["readiness"], "ready")
        self.assertTrue(probe["safeToSend"])

    def test_scheduler_readiness_does_not_peek_web_or_weixin_private_state(self):
        from agent.tools.scheduler import integration
        from channel import messaging_adapter_contract as contract

        class HostileChannel:
            def __getattribute__(self, name):
                if name in {"session_queues", "sessions", "_context_tokens", "request_to_session"}:
                    raise AssertionError(f"private state should not be read: {name}")
                return object.__getattribute__(self, name)

        class Manager:
            def get_channel(self, _name):
                return HostileChannel()

        with (
            patch.object(contract, "_default_manager", return_value=Manager()),
            patch.object(contract, "conf", return_value={"channel_type": "web,weixin"}),
            patch("channel.channel_factory.create_channel", side_effect=AssertionError("must not create channel")),
        ):
            web_ready = integration._is_channel_ready("web", "session-without-queue")
            weixin_ready = integration._is_channel_ready("weixin", "receiver-without-token")

        self.assertFalse(web_ready)
        self.assertFalse(weixin_ready)

    def test_scheduler_create_uses_external_home_channel_as_delivery_target(self):
        from agent.tools.scheduler.scheduler_tool import SchedulerTool
        from bridge.context import Context, ContextType

        cfg = {
            "channel_type": "web,feishu",
            "feishu_home_channel": "oc_home",
            "feishu_home_channel_name": "Ops",
        }

        class FakeStore:
            def __init__(self):
                self.task = None

            def add_task(self, task):
                self.task = task

        context = Context(ContextType.TEXT, "schedule")
        context.kwargs = {
            "session_id": "web-session-1",
            "receiver": "web-session-1",
            "channel_type": "web",
        }
        store = FakeStore()
        tool = SchedulerTool({"channel_type": "web,feishu"})
        tool.task_store = store
        tool.current_context = context

        with patch("agent.tools.scheduler.delivery_target.conf", return_value=cfg):
            result = tool._create_task(
                name="Daily external delivery",
                message="hello",
                schedule_type="once",
                schedule_value="+1h",
            )

        action = store.task["action"]
        self.assertIn("定时任务创建成功", result)
        self.assertIn("receiverHash=", result)
        self.assertIn("[redacted-content]", result)
        self.assertNotIn("web-session-1", result)
        self.assertNotIn("oc_home", result)
        self.assertNotIn("Ops", result)
        self.assertNotIn("hello", result)
        self.assertEqual(action["channel_type"], "feishu")
        self.assertEqual(action["receiver"], "oc_home")
        self.assertEqual(action["receiver_name"], "Ops")
        self.assertEqual(action["delivery_target_source"], "home_channel")
        self.assertTrue(action["home_channel_required"])

    def test_scheduler_tool_public_get_redacts_receiver_and_body(self):
        from agent.tools.scheduler.scheduler_tool import SchedulerTool

        class FakeStore:
            def get_task(self, _task_id):
                return {
                    "id": "task-private-get",
                    "name": "Private get",
                    "enabled": True,
                    "created_at": "2026-06-26T12:00:00",
                    "schedule": {"type": "once", "run_at": "2026-06-26T12:05:00"},
                    "action": {
                        "type": "send_message",
                        "content": "private reminder xoxb-scheduler-get-123456",
                        "receiver": "oc_private_scheduler_get",
                        "receiver_name": "Ops Secret",
                    },
                }

        tool = SchedulerTool({})
        tool.task_store = FakeStore()
        result = tool._get_task(task_id="task-private-get")

        self.assertIn("receiverHash=", result)
        self.assertIn("[redacted-content]", result)
        self.assertNotIn("oc_private_scheduler_get", result)
        self.assertNotIn("Ops Secret", result)
        self.assertNotIn("private reminder", result)
        self.assertNotIn("xoxb-scheduler-get", result)

    def test_scheduler_home_channel_delivery_records_external_connection_event(self):
        from agent.protocol import reset_run_event_ledger_for_tests
        from agent.tools.scheduler import integration
        from channel import messaging_adapter_contract as contract
        from channel.messaging_adapter_contract import EXTERNAL_CONNECTION_EVENT_SESSION_ID

        class ReadyChannel:
            def __init__(self):
                self._startup_error = ""
                self._startup_event = threading.Event()
                self._startup_event.set()

        class Manager:
            def get_channel(self, name):
                return ReadyChannel() if name == "feishu" else None

        class FakeChannel:
            def __init__(self):
                self.sent = []

            def send(self, reply, context):
                self.sent.append((reply, context))

        cfg = {
            "channel_type": "web,feishu",
            "feishu_app_id": "cli-existing",
            "feishu_app_secret": "existing-secret",
            "feishu_home_channel": "oc_home",
            "feishu_home_channel_name": "Ops",
        }
        task = {
            "id": "task-home-delivery",
            "name": "Home delivery",
            "action": {
                "type": "send_message",
                "content": "private body xoxb-scheduler-home-123456",
                "channel_type": "feishu",
                "receiver": "oc_home",
                "receiver_name": "Ops",
                "delivery_target_source": "home_channel",
                "home_channel_required": True,
                "home_channel_platform": "feishu",
            },
        }

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "scheduler-home-events.db")
            channel = FakeChannel()
            with (
                patch("agent.tools.scheduler.delivery_target.conf", return_value=cfg),
                patch.object(contract, "conf", return_value=cfg),
                patch.object(contract, "_default_manager", return_value=Manager()),
                patch("channel.channel_factory.create_channel", return_value=channel),
                patch.object(integration, "_authorize_scheduled_execution", return_value=True),
            ):
                ok = integration._execute_scheduled_task(task, object())
            events = ledger.list_events(session_id=EXTERNAL_CONNECTION_EVENT_SESSION_ID, limit=0)

        reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-scheduler-home-reset.db")

        serialized = json.dumps(events, ensure_ascii=False)
        event_types = [event["event_type"] for event in events]
        self.assertTrue(ok)
        self.assertEqual(len(channel.sent), 1)
        self.assertIn("external_connection.delivery.sent", event_types)
        self.assertIn("receiverHash", serialized)
        self.assertIn("sessionHash", serialized)
        self.assertNotIn("oc_home", serialized)
        self.assertNotIn("xoxb-scheduler-home", serialized)
        self.assertNotIn("private body", serialized)

    def test_scheduler_home_channel_delivery_error_redacts_receiver_in_event(self):
        from agent.protocol import reset_run_event_ledger_for_tests
        from agent.tools.scheduler import integration
        from channel import messaging_adapter_contract as contract
        from channel.messaging_adapter_contract import EXTERNAL_CONNECTION_EVENT_SESSION_ID

        class ReadyChannel:
            def __init__(self):
                self._startup_error = ""
                self._startup_event = threading.Event()
                self._startup_event.set()

        class Manager:
            def get_channel(self, name):
                return ReadyChannel() if name == "feishu" else None

        class FailingChannel:
            def send(self, _reply, _context):
                raise RuntimeError("private delivery failure without token marker")

        receiver = "oc_private_delivery_receiver_without_marker"
        cfg = {
            "channel_type": "web,feishu",
            "feishu_app_id": "cli-existing",
            "feishu_app_secret": "existing-secret",
            "feishu_home_channel": receiver,
            "feishu_home_channel_name": "Ops",
        }
        task = {
            "id": "task-home-delivery-error",
            "name": "Home delivery error",
            "action": {
                "type": "send_message",
                "content": "private body xoxb-scheduler-home-error-123456",
                "channel_type": "feishu",
                "receiver": receiver,
                "receiver_name": "Ops",
                "delivery_target_source": "home_channel",
                "home_channel_required": True,
                "home_channel_platform": "feishu",
            },
        }

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "scheduler-home-delivery-error-events.db")
            with (
                patch("agent.tools.scheduler.delivery_target.conf", return_value=cfg),
                patch.object(contract, "conf", return_value=cfg),
                patch.object(contract, "_default_manager", return_value=Manager()),
                patch("channel.channel_factory.create_channel", return_value=FailingChannel()),
                patch.object(integration, "_authorize_scheduled_execution", return_value=True),
            ):
                ok = integration._execute_scheduled_task(task, object())
            events = ledger.list_events(session_id=EXTERNAL_CONNECTION_EVENT_SESSION_ID, limit=0)

        reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-scheduler-home-delivery-error-reset.db")

        serialized = json.dumps(events, ensure_ascii=False)
        self.assertFalse(ok)
        self.assertIn("external_connection.delivery.error", [event["event_type"] for event in events])
        self.assertIn("receiverHash", serialized)
        self.assertIn("sessionHash", serialized)
        self.assertNotIn(receiver, serialized)
        self.assertNotIn("private delivery failure", serialized)
        self.assertNotIn("xoxb-scheduler-home-error", serialized)
        self.assertNotIn("private body", serialized)

    def test_scheduler_home_channel_missing_blocks_before_channel_creation_and_audits(self):
        from agent.protocol import reset_run_event_ledger_for_tests
        from agent.tools.scheduler import integration
        from channel.messaging_adapter_contract import EXTERNAL_CONNECTION_EVENT_SESSION_ID

        cfg = {
            "channel_type": "web,feishu",
            "feishu_app_id": "cli-existing",
            "feishu_app_secret": "existing-secret",
        }
        task = {
            "id": "task-home-missing",
            "name": "Missing home",
            "action": {
                "type": "send_message",
                "content": "hello",
                "channel_type": "feishu",
                "delivery_target_source": "home_channel",
                "home_channel_required": True,
                "home_channel_platform": "feishu",
            },
        }

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "scheduler-home-missing-events.db")
            with (
                patch("agent.tools.scheduler.delivery_target.conf", return_value=cfg),
                patch("channel.channel_factory.create_channel", side_effect=AssertionError("must not create channel")),
            ):
                ok = integration._execute_scheduled_task(task, object())
            events = ledger.list_events(session_id=EXTERNAL_CONNECTION_EVENT_SESSION_ID, limit=0)

        reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-scheduler-home-missing-reset.db")

        self.assertFalse(ok)
        self.assertNotIn("_scheduler_run_request_id", task)
        self.assertIn("external_connection.delivery.blocked", [event["event_type"] for event in events])
        self.assertIn("scheduler_home_channel_missing", json.dumps(events, ensure_ascii=False))

    def test_scheduler_disabled_home_channel_platform_defers_and_audits_reason(self):
        from agent.protocol import reset_run_event_ledger_for_tests
        from agent.tools.scheduler import integration
        from channel import messaging_adapter_contract as contract
        from channel.messaging_adapter_contract import EXTERNAL_CONNECTION_EVENT_SESSION_ID

        cfg = {
            "channel_type": "web",
            "feishu_app_id": "cli-existing",
            "feishu_app_secret": "existing-secret",
            "feishu_home_channel": "oc_home",
        }
        task = {
            "id": "task-home-disabled",
            "name": "Disabled home",
            "action": {
                "type": "send_message",
                "content": "hello",
                "channel_type": "feishu",
                "receiver": "oc_home",
                "delivery_target_source": "home_channel",
                "home_channel_required": True,
                "home_channel_platform": "feishu",
            },
        }

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "scheduler-home-disabled-events.db")
            with (
                patch("agent.tools.scheduler.delivery_target.conf", return_value=cfg),
                patch.object(contract, "conf", return_value=cfg),
                patch("channel.channel_factory.create_channel", side_effect=AssertionError("must not create channel")),
            ):
                ok = integration._execute_scheduled_task(task, object())
            events = ledger.list_events(session_id=EXTERNAL_CONNECTION_EVENT_SESSION_ID, limit=0)

        reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-scheduler-home-disabled-reset.db")

        serialized = json.dumps(events, ensure_ascii=False)
        self.assertFalse(ok)
        self.assertIn("external_connection.delivery.blocked", [event["event_type"] for event in events])
        self.assertIn("adapter_not_ready", serialized)
        self.assertIn("reasonSummary", serialized)
        self.assertNotIn("channel is not enabled", serialized)

    def test_scheduler_adapter_readiness_reason_is_summarized_in_logs_and_events(self):
        from agent.protocol import reset_run_event_ledger_for_tests
        from agent.tools.scheduler import integration
        from channel import messaging_adapter_contract as contract
        from channel.messaging_adapter_contract import EXTERNAL_CONNECTION_EVENT_SESSION_ID

        raw_reason = "private readiness body without token marker"
        task = {
            "id": "task-private-readiness-reason",
            "name": "Private readiness reason",
            "action": {
                "type": "send_message",
                "content": "hello",
                "channel_type": "feishu",
                "receiver": "oc_home",
                "delivery_target_source": "home_channel",
                "home_channel_required": True,
                "home_channel_platform": "feishu",
            },
        }
        state = {"readiness": "blocked", "safeToSend": False, "reason": raw_reason}
        cfg = {"channel_type": "web,feishu", "feishu_home_channel": "oc_home"}

        with tempfile.TemporaryDirectory() as root:
            ledger = reset_run_event_ledger_for_tests(Path(root) / "scheduler-readiness-summary-events.db")
            with (
                patch("agent.tools.scheduler.delivery_target.conf", return_value=cfg),
                patch.object(contract, "probe_messaging_adapter", return_value=state),
                patch.object(integration.logger, "warning") as warning_log,
                patch("channel.channel_factory.create_channel", side_effect=AssertionError("must not create channel")),
            ):
                ok = integration._execute_scheduled_task(task, object())
            events = ledger.list_events(session_id=EXTERNAL_CONNECTION_EVENT_SESSION_ID, limit=0)

        reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-scheduler-readiness-summary-reset.db")

        logged = " ".join(" ".join(str(part) for part in call.args) for call in warning_log.call_args_list)
        serialized = json.dumps(events, ensure_ascii=False)
        self.assertFalse(ok)
        self.assertNotIn(raw_reason, logged)
        self.assertIn(integration._summary_hash(raw_reason), logged)
        self.assertNotIn(raw_reason, serialized)
        self.assertIn("adapter_not_ready", serialized)
        self.assertIn("reasonSummary", serialized)
        self.assertIn(integration._summary_hash(raw_reason), serialized)

    def test_scheduler_projection_includes_redacted_delivery_target(self):
        from agent.tools.scheduler.projection import project_task

        task = {
            "id": "task-projected-target",
            "name": "Projected target",
            "enabled": True,
            "schedule": {"type": "once", "run_at": "2026-06-26T12:00:00"},
            "action": {
                "type": "send_message",
                "content": "private content",
                "channel_type": "feishu",
                "receiver": "oc_home_should_not_leak",
                "receiver_name": "Ops",
                "delivery_target_source": "home_channel",
                "home_channel_required": True,
                "home_channel_platform": "feishu",
            },
        }
        cfg = {"channel_type": "web,feishu", "feishu_home_channel": "oc_home_should_not_leak"}
        with patch("agent.tools.scheduler.delivery_target.conf", return_value=cfg):
            projection = project_task(task)

        serialized = json.dumps(projection, ensure_ascii=False)
        target = projection["action"]["deliveryTarget"]
        self.assertEqual(target["source"], "home_channel")
        self.assertEqual(target["channelType"], "feishu")
        self.assertTrue(target["homeChannelRequired"])
        self.assertTrue(target["homeChannelConfigured"])
        self.assertIn("receiverNameHash", projection["action"])
        self.assertNotIn("receiverName", projection["action"])
        self.assertNotIn("oc_home_should_not_leak", serialized)
        self.assertNotIn("Ops", serialized)

    def test_frontend_scheduler_delivery_target_type_is_redacted_only(self):
        source = Path("desktop/src/services/ecorexApi.ts").read_text(encoding="utf-8")
        self.assertIn("export type RuntimeSchedulerDeliveryTarget", source)
        self.assertIn("deliveryTarget?: RuntimeSchedulerDeliveryTarget", source)
        action_start = source.index("export type RuntimeSchedulerTaskAction")
        action_end = source.index("export type RuntimeSchedulerTask =", action_start)
        action_block = source[action_start:action_end]
        self.assertIn("receiverNameHash?: string", action_block)
        self.assertNotIn("receiverName?: string", action_block)
        start = source.index("export type RuntimeSchedulerDeliveryTarget")
        end = source.index("export type RuntimeSchedulerTaskAction", start)
        block = source[start:end]
        for field in [
            "status?: string",
            "channelType?: string",
            "source?: string",
            "reason?: string",
            "receiverHash?: string",
            "homeChannelRequired?: boolean",
            "homeChannelConfigured?: boolean",
        ]:
            self.assertIn(field, block)
        self.assertNotIn("receiver?:", block)
        self.assertNotIn("homeChannel?:", block)

    def test_scheduler_home_channel_receiver_is_not_exposed_in_active_requests(self):
        install_web_stub()
        from agent.protocol import reset_run_ledger_for_tests
        from agent.tools.scheduler import integration
        from channel.web import web_channel

        task = {
            "id": "task-home-active-redaction",
            "name": "Home active redaction",
            "action": {
                "type": "agent_task",
                "task_description": "safe",
                "channel_type": "feishu",
                "receiver": "oc_private_home_channel_should_not_leak",
                "delivery_target_source": "home_channel",
                "home_channel_required": True,
                "home_channel_platform": "feishu",
            },
        }

        with tempfile.TemporaryDirectory() as root:
            reset_run_ledger_for_tests(Path(root) / "scheduler-home-active-redaction.db")
            integration._mark_scheduler_run_created(task, "scheduler-home-active-redaction")
            snapshot = web_channel.WebChannel().active_requests_snapshot()

        reset_run_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-v023-scheduler-home-active-reset.db")

        serialized = json.dumps(snapshot, ensure_ascii=False)
        self.assertIn("scheduler_feishu_", serialized)
        self.assertIn("receiverHash", serialized)
        self.assertNotIn("oc_private_home_channel_should_not_leak", serialized)

    def test_scheduler_agent_task_context_session_uses_hashed_external_receiver(self):
        from agent.tools.scheduler import integration

        receiver = "oc_agent_task_private_home_should_not_leak"
        task = {
            "id": "task-agent-session-redaction",
            "name": "Agent session redaction",
            "action": {
                "type": "agent_task",
                "task_description": "summarize safely",
                "channel_type": "feishu",
                "receiver": receiver,
                "delivery_target_source": "home_channel",
                "home_channel_required": True,
                "home_channel_platform": "feishu",
            },
        }

        class FakeBridge:
            context = None

            def agent_reply(self, _query, context=None, **_kwargs):
                self.context = context
                return types.SimpleNamespace(content="done")

        bridge = FakeBridge()
        expected_session = f"scheduler_feishu_{integration._summary_hash(receiver)}_{task['id']}"
        with (
            patch("channel.channel_factory.create_channel", return_value=object()),
            patch.object(integration, "_send_channel_reply", return_value=True),
        ):
            ok = integration._execute_agent_task(task, bridge)

        self.assertTrue(ok)
        self.assertEqual(bridge.context["session_id"], expected_session)
        self.assertNotIn(receiver, bridge.context["session_id"])

    def test_scheduler_skill_call_context_session_uses_hashed_external_receiver(self):
        from agent.tools.scheduler import integration

        receiver = "oc_skill_call_private_home_should_not_leak"
        task = {
            "id": "task-skill-session-redaction",
            "name": "Skill session redaction",
            "action": {
                "type": "skill_call",
                "call_name": "image-generation",
                "call_params": {"prompt": "safe prompt"},
                "channel_type": "feishu",
                "receiver": receiver,
                "delivery_target_source": "home_channel",
                "home_channel_required": True,
                "home_channel_platform": "feishu",
            },
        }

        class FakeBridge:
            context = None

            def agent_reply(self, _query, context=None, **_kwargs):
                self.context = context
                return types.SimpleNamespace(content="done")

        bridge = FakeBridge()
        expected_session = f"scheduler_feishu_{integration._summary_hash(receiver)}_{task['id']}"
        with (
            patch("channel.channel_factory.create_channel", return_value=object()),
            patch.object(integration, "_send_channel_reply", return_value=True),
        ):
            ok = integration._execute_skill_call(task, bridge)

        self.assertTrue(ok)
        self.assertEqual(bridge.context["session_id"], expected_session)
        self.assertNotIn(receiver, bridge.context["session_id"])

    def test_scheduler_unknown_channel_is_not_ready(self):
        from agent.tools.scheduler import integration

        self.assertFalse(integration._is_channel_ready("unknown", "receiver"))

    def test_scheduler_comma_channel_fails_closed_before_channel_creation(self):
        from agent.tools.scheduler import integration

        task = {
            "id": "task-comma-channel",
            "name": "Comma channel",
            "action": {
                "type": "send_message",
                "content": "hello",
                "channel_type": "web,feishu",
                "receiver": "web-session-1",
            },
        }
        with patch("channel.channel_factory.create_channel", side_effect=AssertionError("must not create channel")):
            ok = integration._execute_scheduled_task(task, object())

        self.assertFalse(ok)
        self.assertNotIn("_scheduler_run_request_id", task)

    def test_scheduler_non_web_probe_exception_fails_closed(self):
        from agent.tools.scheduler import integration
        from channel import messaging_adapter_contract as contract

        with patch.object(contract, "probe_messaging_adapter", side_effect=RuntimeError("private probe failure xoxb-probe-123456")):
            self.assertFalse(integration._is_channel_ready("feishu", "oc_home"))
            self.assertTrue(integration._is_channel_ready("web", "web-session-1"))

    def test_external_connection_atomic_write_uses_private_temp_and_cleans_stale_files(self):
        install_web_stub()
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as raw_workspace:
            config_path = Path(raw_workspace) / "config.json"
            stale = Path(str(config_path) + ".tmp-stale")
            stale.write_text("secret", encoding="utf-8")
            with patch.object(web_channel.ChannelsHandler, "_config_path", return_value=str(config_path)):
                web_channel.ChannelsHandler._write_file_config_atomic({"feishu_app_secret": "secret-value"})
                leftovers = list(Path(raw_workspace).glob("config.json.tmp-*"))
                saved = json.loads(config_path.read_text(encoding="utf-8"))

        source = Path("channel/web/web_channel.py").read_text(encoding="utf-8")
        self.assertFalse(leftovers)
        self.assertEqual(saved["feishu_app_secret"], "secret-value")
        self.assertIn("os.open(tmp_path, flags, 0o600)", source)
        self.assertIn("_cleanup_stale_config_temps(path)", source)

    def test_external_connection_atomic_write_removes_temp_on_fsync_failure(self):
        install_web_stub()
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as raw_workspace:
            config_path = Path(raw_workspace) / "config.json"
            with (
                patch.object(web_channel.ChannelsHandler, "_config_path", return_value=str(config_path)),
                patch.object(web_channel.os, "fsync", side_effect=OSError("fsync failed")),
            ):
                with self.assertRaises(OSError):
                    web_channel.ChannelsHandler._write_file_config_atomic({"feishu_app_secret": "secret-value"})
                leftovers = list(Path(raw_workspace).glob("config.json.tmp-*"))

        self.assertFalse(leftovers)

    def test_tool_disclosure_redacts_skill_draft_file_content_before_ui(self):
        from common.ecorex_public_payload import redact_public_tool_value

        public = redact_public_tool_value({
            "action": "create_skill_draft",
            "files": [{
                "path": "SKILL.md",
                "content": "secret draft body with sk-testsecret123456",
            }],
            "api_key": "sk-api-secret-123456",
        })

        serialized = json.dumps(public, ensure_ascii=False)
        self.assertIn("[redacted-content]", serialized)
        self.assertIn('"api_key": "[redacted]"', serialized)
        self.assertNotIn("secret draft body", serialized)
        self.assertNotIn("sk-testsecret", serialized)

    def test_tool_disclosure_redacts_json_string_result_content_before_ui(self):
        from common.ecorex_public_payload import redact_public_tool_value

        public = redact_public_tool_value(json.dumps({
            "draft": {
                "files": [{
                    "path": "SKILL.md",
                    "content": "private workflow body with sk-json-secret-123456",
                }]
            },
            "cookie": "session=should-not-project",
        }))
        serialized = json.dumps(public, ensure_ascii=False)

        self.assertIn("[redacted-content]", serialized)
        self.assertIn('"cookie": "[redacted]"', serialized)
        self.assertNotIn("private workflow body", serialized)
        self.assertNotIn("sk-json-secret", serialized)
        self.assertNotIn("should-not-project", serialized)

    def test_tool_end_sse_result_redacts_json_string_content(self):
        install_web_stub()
        from channel.web.web_channel import WebChannel

        result, meta = WebChannel()._bounded_tool_result_for_sse(json.dumps({
            "draft": {
                "files": [{
                    "path": "SKILL.md",
                    "content": "private workflow body",
                }]
            },
            "api_key": "sk-sse-secret-123456",
        }))

        self.assertIn("[redacted-content]", result)
        self.assertNotIn("private workflow body", result)
        self.assertNotIn("sk-sse-secret", result)
        self.assertIn("tool_output_limits", meta)

    def test_runtime_projection_events_redact_tool_json_string_result(self):
        from agent.protocol import RuntimeProjectionService

        projection = RuntimeProjectionService.project_request_events([
            {
                "event_id": 1,
                "created_at": 1,
                "request_id": "req-tool-event-redaction",
                "session_id": "sess-tool-event-redaction",
                "event_type": "tool.completed",
                "payload": {
                    "tool": "agent_capability",
                    "tool_call_id": "tool-call-redaction",
                    "result": json.dumps({
                        "draft": {
                            "files": [{
                                "path": "SKILL.md",
                                "content": "private workflow body",
                            }]
                        },
                        "token": "should-not-project",
                    }),
                },
            }
        ])
        serialized = json.dumps(projection, ensure_ascii=False)

        self.assertIn("[redacted-content]", serialized)
        self.assertNotIn("private workflow body", serialized)
        self.assertNotIn("should-not-project", serialized)
        self.assertIn("events", projection)
        self.assertEqual(
            projection["events"][0]["payload"]["result"]["draft"]["files"][0]["content"],
            "[redacted-content]",
        )

    def test_mcp_error_format_masks_remote_sensitive_details(self):
        from agent.tools.mcp.mcp_client import McpClient, _mask_sensitive

        formatted = McpClient._format_rpc_error({
            "code": -32000,
            "message": "authorization=Bearer should-not-log",
            "data": {"cookie": "session=should-not-log", "api_key": "sk-should-not-log-123456"},
        })

        self.assertNotIn("should-not-log", formatted)
        self.assertNotIn("sk-should-not-log", formatted)
        self.assertIn("***", formatted)
        self.assertNotIn("secret-token", _mask_sensitive("HTTP error token=secret-token"))

    def retired_legacy_external_connections_browser_smoke_harness_contract(self):
        smoke_path = Path("scripts/smoke-web-external-connections-browser.py")
        smoke_source = smoke_path.read_text(encoding="utf-8")
        probe_script = python_function_literal_return(smoke_path, "_probe_script")

        self.assertIn("from web_smoke_support import ROOT, static_site_server", smoke_source)
        self.assertIn("parser.add_argument(\"--app-root\", default=\"desktop/dist\"", smoke_source)
        self.assertIn("parser.add_argument(\"--artifact\"", smoke_source)
        self.assertIn(".settings-nav button", probe_script)
        self.assertIn(".external-connection-card", probe_script)
        self.assertIn(".connection-logo.is-feishu", probe_script)
        self.assertIn(".connection-logo.is-slack", probe_script)
        self.assertIn("input[type=\"password\"]", probe_script)
        self.assertIn("设为投递目标", probe_script)
        self.assertIn("homeChannelActionVisible", probe_script)
        self.assertIn("homeChannelActionUsable", probe_script)
        self.assertIn("home-channel action should be disabled when API only returns a hashed Feishu homeChannel", probe_script)
        self.assertIn("secretEchoed === false", probe_script)
        self.assertIn("Run Center", probe_script)

    def retired_legacy_external_connections_browser_smoke_artifact_contract(self):
        artifact_path = Path("docs/v0.2.3/artifacts/external-connections-browser-smoke.json")
        privacy_path = Path("docs/v0.2.3/artifacts/external-connections-privacy-scan.json")

        self.assertTrue(artifact_path.exists(), "external connections browser smoke artifact is missing")
        self.assertTrue(privacy_path.exists(), "external connections privacy scan artifact is missing")

        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        privacy = json.loads(privacy_path.read_text(encoding="utf-8"))
        metrics = artifact.get("metrics") or {}

        self.assertEqual(artifact.get("status"), "PASS")
        self.assertEqual(artifact.get("consoleErrorCount"), 0)
        self.assertEqual(metrics.get("connectionCards"), 2)
        self.assertTrue(metrics.get("hasFeishuLogo"))
        self.assertTrue(metrics.get("hasSlackLogo"))
        self.assertTrue(metrics.get("homeChannelActionVisible"))
        self.assertTrue(metrics.get("homeChannelActionUsable"))
        self.assertTrue(metrics.get("secretRedactedOnSave"))
        self.assertTrue(metrics.get("runCenterHidden"))
        self.assertEqual(privacy.get("status"), "success")
        self.assertEqual(privacy.get("filesScanned"), 1)
        self.assertEqual(privacy.get("findingCount"), 0)

    def retired_legacy_sidecar_default_injects_cdp_auto_launch_true(self):
        source = Path("desktop/electron/sidecar.ts").read_text(encoding="utf-8")
        self.assertIn("cdp_auto_launch: true", source)
        self.assertNotIn("cdp_auto_launch: false", source)

    def retired_legacy_v027_sidecar_prefers_bundled_playwright_browsers_on_macos(
        self,
    ):
        source = Path("desktop/electron/sidecar.ts").read_text(encoding="utf-8")

        self.assertIn('bundledPlaywrightBrowsersDir = path.join(this.repoRoot, "playwright-browsers")', source)
        self.assertIn("fs.existsSync(bundledPlaywrightBrowsersDir)", source)
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH: managedPlaywrightBrowsersDir", source)
        self.assertIn("ECOREX_PLAYWRIGHT_BROWSERS_DIR: managedPlaywrightBrowsersDir", source)
        self.assertIn('inheritedPlaywrightBrowsersDir || path.join(userDataDir, "capabilities", "playwright-browsers")', source)
        self.assertNotIn('PLAYWRIGHT_BROWSERS_PATH:\n            process.platform === "darwin"', source)

    def test_docs_skeleton_exists_for_v023_goal(self):
        root = Path("docs/v0.2.3")
        for name in [
            "goal.md",
            "development-log.md",
            "review-log.md",
            "acceptance-checklist.md",
            "evidence-ledger.md",
            "harness-matrix.json",
        ]:
            self.assertTrue((root / name).exists(), name)
        json.loads((root / "harness-matrix.json").read_text(encoding="utf-8"))

    def test_skill_learning_draft_is_ledger_backed_and_projected(self):
        from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests
        from agent.skills.learning_service import SkillLearningService

        with tempfile.TemporaryDirectory() as tmp:
            ledger = reset_run_event_ledger_for_tests(Path(tmp) / "events.db")
            result = SkillLearningService(ledger=ledger).create_draft(
                name="xhs-note-workflow",
                description="Use when a reviewed Xiaohongshu note workflow should be repeated.",
                goal="learn a successful Xiaohongshu note workflow",
                request_id="req-skill-learning",
                session_id="sess-skill-learning",
                sources=[{"type": "run", "title": "reviewed workflow"}],
                reviews=[
                    {"role": "Skill Author Reviewer", "status": "pass"},
                    {"role": "Security/Privacy Reviewer", "status": "pass"},
                    {"role": "Domain/Product Reviewer", "status": "pass"},
                ],
                files=[{
                    "path": "SKILL.md",
                    "content": (
                        "---\n"
                        "name: xhs-note-workflow\n"
                        "description: Use when a reviewed Xiaohongshu note workflow should be repeated.\n"
                        "category: learned\n"
                        "---\n\n"
                        "Use only after the workflow was reviewed and approved.\n"
                    ),
                }],
            )
            projection = RuntimeProjectionService(ledger).request_projection("req-skill-learning")

        draft = result["draft"]
        self.assertEqual(draft["validation"]["status"], "pass")
        self.assertEqual(draft["security"]["status"], "pass")
        self.assertEqual(draft["reviewState"]["status"], "pass")
        self.assertEqual(len(projection["skill_drafts"]), 1)
        self.assertEqual(projection["skill_drafts"][0]["name"], "xhs-note-workflow")
        self.assertEqual(projection["skill_drafts"][0]["status"], "draft")

    def test_skill_learning_requested_fallback_draft_id_is_stable(self):
        from agent.protocol import RuntimeProjectionService

        goal = "learn a successful Xiaohongshu note workflow"
        projection = RuntimeProjectionService.project_request_events([
            {
                "event_id": 1,
                "created_at": 1,
                "request_id": "req-learning-stable",
                "session_id": "sess-learning-stable",
                "event_type": "skill_learning.requested",
                "payload": {"goal": goal, "name": "xhs-note-workflow"},
            }
        ])
        expected = "learning-" + hashlib.sha256(goal.encode("utf-8")).hexdigest()[:12]

        self.assertEqual(projection["skill_drafts"][0]["draftId"], expected)
        self.assertNotIn("hash(goal)", Path("agent/protocol/runtime_projection.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
