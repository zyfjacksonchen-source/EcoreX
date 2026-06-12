# encoding:utf-8
import json
import os
import sys
import tempfile
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


class TestEcoreXWorkspaceState(unittest.TestCase):
    def test_installation_manifest_and_ui_state_share_workspace(self):
        from common.ecorex_workspace import (
            installation_manifest_path,
            load_installation_manifest,
            load_ui_state,
            register_installation,
            save_ui_state,
            ui_state_path,
        )

        with tempfile.TemporaryDirectory() as workspace:
            manifest = register_installation(workspace, "webui", {"port": 9899})
            self.assertEqual(manifest["workspacePath"], os.path.abspath(workspace))
            self.assertIn("webui", manifest["surfaces"])
            self.assertTrue(installation_manifest_path(workspace).is_file())

            state = save_ui_state(workspace, {
                "activeSessionId": "s1",
                "sessionUiState": {"s1": {"composerText": "draft"}},
                "unknownKey": "ignored",
            })
            self.assertEqual(state["activeSessionId"], "s1")
            self.assertEqual(state["sessionUiState"]["s1"]["composerText"], "draft")
            self.assertNotIn("unknownKey", state)
            self.assertTrue(ui_state_path(workspace).is_file())

            self.assertEqual(load_installation_manifest(workspace)["surfaces"]["webui"]["port"], 9899)
            self.assertEqual(load_ui_state(workspace)["activeSessionId"], "s1")

    def test_session_lock_blocks_same_session_until_released(self):
        from common.ecorex_workspace import SessionBusyError, SessionLock

        with tempfile.TemporaryDirectory() as workspace:
            first = SessionLock(workspace, "session-1").acquire()
            with self.assertRaises(SessionBusyError):
                SessionLock(workspace, "session-1").acquire()
            first.release()
            second = SessionLock(workspace, "session-1").acquire()
            second.release()


class TestWebParallelHandlers(unittest.TestCase):
    def test_ui_state_handler_put_and_get(self):
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as workspace:
            handler = web_channel.UiStateHandler()
            payload = {"state": {"theme": "dark", "activeSessionId": "abc"}}
            with patch.object(web_channel, "_require_auth", return_value=None):
                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    with patch.object(web_channel.web, "data", return_value=json.dumps(payload).encode("utf-8")):
                        put_result = json.loads(handler.PUT())
                    get_result = json.loads(handler.GET())

            self.assertEqual(put_result["status"], "success")
            self.assertEqual(get_result["state"]["theme"], "dark")
            self.assertEqual(get_result["state"]["activeSessionId"], "abc")

    def test_installations_handler_registers_surface(self):
        from channel.web import web_channel

        with tempfile.TemporaryDirectory() as workspace:
            handler = web_channel.InstallationsHandler()
            payload = {"surface": "desktop", "metadata": {"version": "test"}}
            with patch.object(web_channel, "_require_auth", return_value=None):
                with patch.object(web_channel, "_get_workspace_root", return_value=workspace):
                    with patch.object(web_channel.web, "data", return_value=json.dumps(payload).encode("utf-8")):
                        post_result = json.loads(handler.POST())
                    get_result = json.loads(handler.GET())

            self.assertEqual(post_result["status"], "success")
            self.assertEqual(get_result["manifest"]["surfaces"]["desktop"]["version"], "test")

    def test_default_app_shell_is_independent_of_desktop_dist(self):
        from channel.web.web_channel import _default_web_app_html

        html = _default_web_app_html()
        self.assertIn("EcoreX Web App", html)
        self.assertIn("window.ecorexDesktop", html)
        self.assertNotIn("desktop/dist", html)


if __name__ == "__main__":
    unittest.main()
