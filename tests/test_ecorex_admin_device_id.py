import importlib.util
import base64
import hashlib
import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock
from urllib.parse import quote
from http.server import ThreadingHTTPServer


def load_admin_api():
    module_path = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "ecorex-admin-api" / "ecorex_admin_api.py"
    spec = importlib.util.spec_from_file_location("ecorex_admin_api", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


admin_api = load_admin_api()


def _write_release_fixture(root: pathlib.Path, name: str, version: str, payload: bytes = b"release") -> pathlib.Path:
    release = root / name
    downloads = release / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    artifact = downloads / f"{name}.zip"
    artifact.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest().upper()
    manifest = {
        "product": "EcoreX",
        "version": version,
        "updatedAt": "2026-07-03T00:00:00Z",
        "update": {
            "webui": {
                "mode": "online",
                "channel": "stable",
                "promotion": "admin-gated",
                "artifactIds": ["webui-windows-x64"],
            }
        },
        "artifacts": [
            {
                "id": "webui-windows-x64",
                "fileName": artifact.name,
                "href": f"downloads/{artifact.name}",
                "status": "ready",
                "size": len(payload),
                "sha256": digest,
            }
        ],
    }
    (release / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return release


class DeviceIdMatchesTest(unittest.TestCase):
    def test_matches_raw_and_url_encoded_device_ids(self):
        raw = "\u7535\u8111-\u5f20\u4e09-darwin"
        encoded = quote(raw, safe="")
        self.assertTrue(admin_api.device_id_matches(raw, raw))
        self.assertTrue(admin_api.device_id_matches(raw, encoded))
        self.assertTrue(admin_api.device_id_matches(encoded, raw))

    def test_empty_device_id_keeps_legacy_sessions_compatible(self):
        self.assertTrue(admin_api.device_id_matches("", "ecorex-device"))
        self.assertTrue(admin_api.device_id_matches("ecorex-device", ""))

    def test_different_devices_do_not_match(self):
        self.assertFalse(admin_api.device_id_matches("ecorex-device-a", "ecorex-device-b"))


class AdminBasicAuthTest(unittest.TestCase):
    def _authorized(self, username, password):
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        handler = object.__new__(admin_api.AdminHandler)
        handler.headers = {"Authorization": f"Basic {token}"}
        return handler._admin_authorized()

    def test_comma_separated_admin_usernames_share_admin_password(self):
        with mock.patch.dict(
            admin_api.os.environ,
            {
                "ECOREX_ADMIN_USERNAMES": "admin, root",
                "ECOREX_ADMIN_PASSWORD": "Password123",
            },
            clear=False,
        ):
            self.assertTrue(self._authorized("admin", "Password123"))
            self.assertTrue(self._authorized("root", "Password123"))
            self.assertFalse(self._authorized("operator", "Password123"))
            self.assertFalse(self._authorized("admin", "wrong"))


class AdminReleaseStateTest(unittest.TestCase):
    def _start_server(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp_dir.cleanup)
        previous_store = getattr(admin_api.AdminHandler, "store", None)
        self.addCleanup(lambda: setattr(admin_api.AdminHandler, "store", previous_store))
        admin_api.AdminHandler.store = admin_api.AdminStore(str(pathlib.Path(temp_dir.name) / "admin.sqlite3"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), admin_api.AdminHandler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        return server

    def _request_json(self, server, method, path, payload=None, client_key="unit-key"):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}{path}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-EcoreX-Client-Key": client_key,
            },
            method=method,
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_release_state_disables_older_staged_candidates(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp_dir.cleanup)
        root = pathlib.Path(temp_dir.name) / "releases"
        _write_release_fixture(root, "current", "0.2.7.1", b"current")
        _write_release_fixture(root, "staged-v0.2.7", "0.2.7", b"older")
        _write_release_fixture(root, "staged-v0.2.7.2", "0.2.7.2", b"newer")
        store = admin_api.AdminStore(str(pathlib.Path(temp_dir.name) / "admin.sqlite3"))

        with mock.patch.dict(admin_api.os.environ, {"ECOREX_RELEASE_ROOT": str(root)}, clear=False):
            state = store.release_state()

        staged = {item["version"]: item for item in state["staged"]}
        self.assertFalse(staged["0.2.7"]["canPromote"])
        self.assertIn("低于当前 stable", staged["0.2.7"]["promoteDisabledReason"])
        self.assertTrue(staged["0.2.7.2"]["canPromote"])

    def test_notify_release_broadcasts_current_stable_without_promote(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp_dir.cleanup)
        temp_path = pathlib.Path(temp_dir.name)
        root = temp_path / "releases"
        state_dir = temp_path / "state"
        _write_release_fixture(root, "current", "0.2.7.1", b"current")
        store = admin_api.AdminStore(str(temp_path / "admin.sqlite3"))

        with mock.patch.dict(
            admin_api.os.environ,
            {
                "ECOREX_RELEASE_ROOT": str(root),
                "ECOREX_WEBUI_STATE_DIR": str(state_dir),
            },
            clear=False,
        ):
            payload = store.notify_release({"version": "0.2.7.1", "actor": "unit"})

        manifest = json.loads((root / "current" / "manifest.json").read_text(encoding="utf-8"))
        notice = manifest["update"]["webui"]["notice"]
        update_state = json.loads((state_dir / "update-state.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["notifiedVersion"], "0.2.7.1")
        self.assertTrue(payload["noticeRevision"])
        self.assertTrue(payload["noticeFileWritten"])
        self.assertTrue(payload["manifestNoticeWritten"])
        self.assertEqual(notice["revision"], payload["noticeRevision"])
        self.assertEqual(update_state["noticeRevision"], payload["noticeRevision"])
        self.assertEqual(update_state["mode"], "manual")
        self.assertEqual(update_state["status"], "ready")
        self.assertEqual(update_state["browserAction"], "none")
        self.assertEqual(update_state["activationPolicy"], "manual-update-check")

    def test_notify_release_succeeds_when_manifest_is_not_writable(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp_dir.cleanup)
        temp_path = pathlib.Path(temp_dir.name)
        root = temp_path / "releases"
        state_dir = temp_path / "state"
        _write_release_fixture(root, "current", "0.2.7.1", b"current")
        store = admin_api.AdminStore(str(temp_path / "admin.sqlite3"))
        manifest_path = root / "current" / "manifest.json"
        original_write = store._write_release_json

        def write_or_deny(path, payload):
            if pathlib.Path(path) == manifest_path:
                raise PermissionError("manifest denied")
            return original_write(path, payload)

        with mock.patch.dict(
            admin_api.os.environ,
            {
                "ECOREX_RELEASE_ROOT": str(root),
                "ECOREX_WEBUI_STATE_DIR": str(state_dir),
            },
            clear=False,
        ), mock.patch.object(store, "_write_release_json", side_effect=write_or_deny):
            payload = store.notify_release({"version": "0.2.7.1", "actor": "unit"})

        notice_state = store.release_notice()["notice"]
        update_state = json.loads((state_dir / "update-state.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["notifiedVersion"], "0.2.7.1")
        self.assertTrue(payload["noticeFileWritten"])
        self.assertFalse(payload["manifestNoticeWritten"])
        self.assertEqual(payload["manifestNoticeError"], "PermissionError")
        self.assertNotIn(str(root), payload["manifestNoticeErrorHash"])
        self.assertEqual(notice_state["revision"], payload["noticeRevision"])
        self.assertEqual(update_state["noticeRevision"], payload["noticeRevision"])

    def test_client_release_notice_endpoint_returns_admin_data_notice(self):
        server = self._start_server()
        store = admin_api.AdminHandler.store
        notice = {
            "revision": "0.2.7.1-unit",
            "version": "0.2.7.1",
            "message": "EcoreX 0.2.7.1 已发布，已安装用户可在本机检查更新。",
            "publishedAt": "2026-07-03T00:00:00Z",
            "reason": "admin-release-notify",
            "redacted": True,
        }
        store._write_release_notice_file(notice)

        payload = self._request_json(server, "GET", "/client/release-notice", client_key=admin_api.DEFAULT_CLIENT_EVENT_KEY)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["notice"]["revision"], "0.2.7.1-unit")
        self.assertTrue(payload["redacted"])


class TongxinAuthEndpointTest(unittest.TestCase):
    def _start_server(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp_dir.cleanup)
        previous_store = getattr(admin_api.AdminHandler, "store", None)
        self.addCleanup(lambda: setattr(admin_api.AdminHandler, "store", previous_store))
        admin_api.AdminHandler.store = admin_api.AdminStore(str(pathlib.Path(temp_dir.name) / "admin.sqlite3"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), admin_api.AdminHandler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        return server

    def _request_json(self, server, method, path, payload=None, client_key="unit-key"):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}{path}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-EcoreX-Client-Key": client_key,
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, json.loads(raw)

    def test_tongxin_auth_endpoint_uses_client_key_and_returns_configured_manifest(self):
        server = self._start_server()
        with mock.patch.dict(
            admin_api.os.environ,
            {
                "ECOREX_CLIENT_EVENT_KEYS": "unit-key",
                "ECOREX_TONGXIN_BOOTSTRAP_MANIFEST_URL": "https://example.invalid/tongxin/manifest.json",
                "ECOREX_TONGXIN_BOOTSTRAP_TOKEN": "server-bootstrap-token",
            },
            clear=False,
        ):
            status_code, status = self._request_json(server, "GET", "/client/tongxin/auth")
            self.assertEqual(status_code, 200)
            self.assertTrue(status["configured"])
            self.assertTrue(status["readOnly"])
            self.assertNotIn("server-bootstrap-token", json.dumps(status, ensure_ascii=False))

            auth_code, auth = self._request_json(
                server,
                "POST",
                "/client/tongxin/auth",
                {"username": "xin-user@example.test", "password": "xin-password-secret", "readOnly": True},
            )
            self.assertEqual(auth_code, 200)
            self.assertTrue(auth["ok"])
            self.assertEqual(auth["manifestUrl"], "https://example.invalid/tongxin/manifest.json")
            self.assertTrue(auth["permission"]["readOnly"])
            rendered = json.dumps(auth, ensure_ascii=False)
            self.assertNotIn("xin-password-secret", rendered)
            self.assertNotIn("xin-user@example.test", rendered)

            denied_code, denied = self._request_json(server, "GET", "/client/tongxin/auth", client_key="bad-key")
            self.assertEqual(denied_code, 403)
            self.assertFalse(denied["ok"])


class Phase1SyncIngestTest(unittest.TestCase):
    def _store_with_session(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp_dir.cleanup)
        store = admin_api.AdminStore(str(pathlib.Path(temp_dir.name) / "admin.sqlite3"))
        store.create_user({
            "name": "Sync User",
            "email": "sync@example.com",
            "role": "member",
            "initialPassword": "Password123",
        })
        session = store.login({
            "email": "sync@example.com",
            "password": "Password123",
            "deviceId": "device-1",
        })
        return store, session

    def test_phase1_sync_is_idempotent_and_omits_bodies_and_paths(self):
        store, session = self._store_with_session()
        payload = {
            "events": [
                {
                    "idempotencyKey": "event:req-1:completed",
                    "eventType": "run.completed",
                    "status": "completed",
                    "sessionId": "sess-1",
                    "requestId": "req-1",
                    "detail": {
                        "safe": "kept",
                        "content": "chat body must not be stored",
                        "message": "assistant body must not be stored",
                    },
                }
            ],
            "artifacts": [
                {
                    "idempotencyKey": "artifact:req-1:one",
                    "id": "req-1:C:/Users/Alice/secret-output.txt",
                    "path": "C:/Users/Alice/secret-output.txt",
                    "previewUrl": "/api/file?path=C%3A%2FUsers%2FAlice%2Fsecret-output.txt",
                    "title": "secret-output.txt",
                    "kind": "file",
                    "status": "ready",
                    "sizeBytes": 42,
                    "content": "file body must not be stored",
                }
            ],
        }

        first = store.ingest_sync_events(payload, token=session["token"], device_id="device-1")
        second = store.ingest_sync_events(payload, token=session["token"], device_id="device-1")

        self.assertEqual(first["eventsAccepted"], 1)
        self.assertEqual(first["artifactsAccepted"], 1)
        self.assertEqual(second["eventsAccepted"], 1)
        self.assertEqual(second["artifactsAccepted"], 1)
        with store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sync_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sync_artifacts").fetchone()[0], 1)
            event = conn.execute("SELECT * FROM sync_events").fetchone()
            artifact = conn.execute("SELECT * FROM sync_artifacts").fetchone()
            detail = json.loads(event["detail"])
            metadata = json.loads(artifact["metadata"])
            serialized = json.dumps({"detail": detail, "metadata": metadata}, ensure_ascii=False)

        self.assertEqual(detail["safe"], "kept")
        self.assertEqual(detail["content"], "[omitted]")
        self.assertEqual(detail["message"], "[omitted]")
        self.assertTrue(artifact["artifact_id"].startswith("artifact:"))
        self.assertTrue(artifact["path_hash"])
        self.assertEqual(artifact["path_ext"], ".txt")
        self.assertNotIn("chat body must not be stored", serialized)
        self.assertNotIn("assistant body must not be stored", serialized)
        self.assertNotIn("file body must not be stored", serialized)
        self.assertNotIn("C:/Users/Alice", serialized)
        self.assertEqual(store.state()["syncSummary"]["events"], 1)
        self.assertEqual(store.state()["syncSummary"]["artifacts"], 1)

    def test_runtime_audit_projection_summarizes_sync_events_without_raw_leaks(self):
        store, session = self._store_with_session()
        raw_session = "session-runtime-audit-private"
        raw_request = "request-runtime-audit-private"
        raw_path = r"C:\Users\Alice\workspace\private prompt draft.md"
        raw_unknown_event_type = r"C:\Users\Alice\workspace\private prompt event"
        payload = {
            "events": [
                {
                    "idempotencyKey": "event:audit:failed",
                    "eventType": "run.failed",
                    "status": raw_path,
                    "source": raw_path,
                    "sessionId": raw_session,
                    "requestId": raw_request,
                    "createdAt": raw_path,
                    "detail": {
                        "error_type": raw_path,
                        "prompt": "do not leak admin audit prompt",
                        "policy_mode": "disabled",
                    },
                },
                {
                    "idempotencyKey": "event:audit:policy-blocked",
                    "eventType": "capability.policy_blocked",
                    "status": "blocked",
                    "source": "runtime",
                    "sessionId": raw_session,
                    "requestId": raw_request,
                    "detail": {
                        "policy_mode": "disabled",
                        "pack_id": "office-pdf-ghp_abcd",
                        "message": "blocked prompt body must not leak",
                    },
                },
                {
                    "idempotencyKey": "event:audit:unknown",
                    "eventType": raw_unknown_event_type,
                    "status": "completed",
                    "source": "client",
                    "sessionId": raw_session,
                    "requestId": raw_request,
                    "detail": {"safe": "shape only"},
                },
            ],
            "artifacts": [
                {
                    "idempotencyKey": "artifact:audit:one",
                    "id": f"{raw_request}:{raw_path}",
                    "path": raw_path,
                    "title": "private prompt draft.md",
                    "sessionId": raw_session,
                    "requestId": raw_request,
                    "kind": "file",
                    "status": "ready",
                }
            ],
        }

        store.ingest_sync_events(payload, token=session["token"], device_id="device-1")
        audit = store.state()["runtimeAudit"]
        rendered = json.dumps(audit, ensure_ascii=False)

        for raw in (
            raw_session,
            raw_request,
            raw_path,
            raw_unknown_event_type,
            "private prompt",
            "private_prompt",
            "do not leak admin audit prompt",
            "blocked prompt body must not leak",
            "office-pdf-ghp_abcd",
            "sync@example.com",
            "device-1",
        ):
            self.assertNotIn(raw, rendered)
        self.assertEqual(audit["sourceOfTruth"], "admin-sync-runtime-events")
        self.assertEqual(audit["summary"]["events"], 3)
        self.assertEqual(audit["summary"]["artifacts"], 1)
        self.assertEqual(audit["summary"]["requests"], 1)
        self.assertEqual(audit["summary"]["terminalEvents"], 1)
        self.assertEqual(audit["summary"]["capabilityPolicyBlocked"], 1)
        self.assertEqual(audit["summary"]["unknownEventTypes"], 1)
        self.assertEqual(audit["eventTypeCounts"]["run.failed"], 1)
        self.assertEqual(audit["eventTypeCounts"]["capability.policy_blocked"], 1)
        self.assertEqual(audit["eventTypeCounts"]["unknown"], 1)
        self.assertFalse(audit["privacy"]["includesRawRuntimePayloads"])
        self.assertFalse(audit["privacy"]["includesRawRequestSessionIds"])
        self.assertFalse(audit["privacy"]["includesArtifactPaths"])
        unknown = [item for item in audit["recentEvents"] if item["eventType"] == "unknown"][0]
        self.assertTrue(unknown["eventTypeHash"])
        self.assertTrue(unknown["eventTypeRedacted"])
        failed = [item for item in audit["recentEvents"] if item["eventType"] == "run.failed"][0]
        self.assertEqual(failed["status"], "unknown")
        self.assertTrue(failed["statusHash"])
        self.assertTrue(failed["sourceHash"])
        self.assertTrue(failed["createdAtHash"])
        self.assertTrue(audit["requests"][0]["requestHash"])
        self.assertEqual(audit["requests"][0]["artifactCount"], 1)

    def test_artifact_feedback_defaults_valid_and_thumbs_down_marks_invalid(self):
        store, session = self._store_with_session()
        base_artifact = {
            "idempotencyKey": "artifact:feedback:one",
            "safeArtifactId": "artifact:safe-one",
            "sessionId": "sess-feedback",
            "requestId": "req-feedback",
            "title": "final-cover.png",
            "kind": "image",
            "status": "ready",
        }

        store.ingest_sync_events(
            {"artifacts": [base_artifact]},
            token=session["token"],
            device_id="device-1",
        )
        self.assertEqual(store.state()["syncSummary"]["validArtifacts"], 1)
        self.assertEqual(store.state()["syncSummary"]["defaultValidArtifacts"], 1)
        self.assertEqual(store.state()["syncSummary"]["invalidArtifacts"], 0)

        store.ingest_sync_events(
            {
                "events": [
                    {
                        "idempotencyKey": "event:feedback:one:down",
                        "eventType": "artifact.feedback",
                        "status": "invalid",
                        "sessionId": "sess-feedback",
                        "requestId": "req-feedback",
                        "detail": {
                            "artifact_hash": "safe-hash",
                            "artifact_validity": "invalid",
                            "artifact_feedback_signal": "thumbs_down",
                        },
                    }
                ],
                "artifacts": [
                    {
                        **base_artifact,
                        "artifactValidity": "invalid",
                        "artifactFeedbackSignal": "thumbs_down",
                    }
                ],
            },
            token=session["token"],
            device_id="device-1",
        )
        summary = store.state()["syncSummary"]

        self.assertEqual(summary["artifacts"], 1)
        self.assertEqual(summary["validArtifacts"], 0)
        self.assertEqual(summary["defaultValidArtifacts"], 0)
        self.assertEqual(summary["invalidArtifacts"], 1)
        self.assertEqual(summary["thumbsDownArtifacts"], 1)
        self.assertEqual(store.state()["runtimeAudit"]["eventTypeCounts"]["artifact.feedback"], 1)

    def test_runtime_audit_projects_user_actions_effective_artifacts_and_feedback_traces(self):
        store, session = self._store_with_session()
        store.ingest_sync_events(
            {
                "events": [
                    {
                        "idempotencyKey": "event:v029:image-started",
                        "eventType": "image_job.started",
                        "status": "running",
                        "source": "image_job",
                        "sessionId": "sess-v029",
                        "requestId": "req-image",
                        "detail": {"job_id": "job-safe"},
                    },
                    {
                        "idempotencyKey": "event:v029:imagegen-tool",
                        "eventType": "tool.started",
                        "status": "running",
                        "source": "tool",
                        "sessionId": "sess-v029",
                        "requestId": "req-image",
                        "detail": {"tool": "imagegen"},
                    },
                    {
                        "idempotencyKey": "event:v029:feedback-down",
                        "eventType": "artifact.feedback",
                        "status": "invalid",
                        "source": "WebUI",
                        "sessionId": "sess-v029",
                        "requestId": "req-bad",
                        "detail": {
                            "artifact_hash": "bad-safe",
                            "artifact_validity": "invalid",
                            "artifact_feedback_signal": "thumbs_down",
                            "feedback_share_id": "sh_unittrace01",
                            "feedback_share_url": "https://mvdcm.ecoremedia.net/ecorex-agent/client/session-shares/sh_unittrace01",
                        },
                    },
                ],
                "artifacts": [
                    {
                        "idempotencyKey": "artifact:v029:default",
                        "safeArtifactId": "artifact:v029-default",
                        "sessionId": "sess-v029",
                        "requestId": "req-default",
                        "title": "final-cover.png",
                        "kind": "image",
                        "status": "ready",
                        "pathExt": ".png",
                    },
                    {
                        "idempotencyKey": "artifact:v029:up",
                        "safeArtifactId": "artifact:v029-up",
                        "sessionId": "sess-v029",
                        "requestId": "req-up",
                        "title": "approved-report.pdf",
                        "kind": "file",
                        "status": "ready",
                        "pathExt": ".pdf",
                        "artifactFeedbackSignal": "thumbs_up",
                    },
                    {
                        "idempotencyKey": "artifact:v029:down",
                        "safeArtifactId": "artifact:v029-down",
                        "sessionId": "sess-v029",
                        "requestId": "req-bad",
                        "title": "bad-output.png",
                        "kind": "image",
                        "status": "ready",
                        "pathExt": ".png",
                        "artifactValidity": "invalid",
                        "artifactFeedbackSignal": "thumbs_down",
                        "artifactFeedbackAt": "2026-07-04T21:20:00Z",
                        "feedbackShareId": "sh_unittrace01",
                        "feedbackShareUrl": "https://mvdcm.ecoremedia.net/ecorex-agent/client/session-shares/sh_unittrace01",
                    },
                ],
            },
            token=session["token"],
            device_id="device-1",
        )

        audit = store.state()["runtimeAudit"]
        rendered = json.dumps(audit, ensure_ascii=False)

        self.assertEqual(audit["summary"]["effectiveArtifacts"], 2)
        self.assertEqual(audit["summary"]["thumbsDownArtifacts"], 1)
        self.assertEqual(audit["actionTypeCounts"]["image_processing"], 2)
        self.assertEqual(audit["actionTypeCounts"]["artifact_feedback"], 1)
        self.assertGreaterEqual(len(audit["userActions"]), 3)
        self.assertEqual(len(audit["effectiveArtifacts"]), 2)
        self.assertEqual({item["artifactFeedbackSignal"] for item in audit["effectiveArtifacts"]}, {"default", "thumbs_up"})
        self.assertEqual(len(audit["feedbackTraces"]), 1)
        trace = audit["feedbackTraces"][0]
        self.assertEqual(trace["userName"], "Sync User")
        self.assertEqual(trace["userEmail"], "sync@example.com")
        self.assertEqual(trace["feedbackShareId"], "sh_unittrace01")
        self.assertIn("/ecorex-agent/client/session-shares/sh_unittrace01", trace["feedbackShareUrl"])
        self.assertNotIn("request_id", rendered)
        self.assertNotIn("sess-v029", rendered)
        self.assertNotIn("req-bad", rendered)

    def test_run_paused_is_not_counted_as_failed_terminal_event(self):
        store, session = self._store_with_session()
        store.ingest_sync_events(
            {
                "events": [
                    {
                        "idempotencyKey": "event:paused:one",
                        "eventType": "run.paused",
                        "status": "paused",
                        "source": "runtime",
                        "sessionId": "sess-paused",
                        "requestId": "req-paused",
                    }
                ]
            },
            token=session["token"],
            device_id="device-1",
        )
        audit = store.state()["runtimeAudit"]

        self.assertEqual(audit["eventTypeCounts"]["run.paused"], 1)
        self.assertEqual(audit["statusCounts"]["paused"], 1)
        self.assertEqual(audit["summary"]["terminalEvents"], 0)
        self.assertNotIn("run.failed", audit["eventTypeCounts"])

    def test_session_share_redacts_local_paths_and_renders_public_html(self):
        store, session = self._store_with_session()
        result = store.create_session_share(
            {
                "title": r"Review C:\Users\Alice\secret-plan.md",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": r"请看 C:\Users\Alice\secret-plan.md token=abc123"}],
                    },
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "已生成封面。"}],
                        "artifacts": [
                            {
                                "title": r"C:\Users\Alice\output\cover.png",
                                "kind": "image",
                                "fileName": "cover.png",
                                "mimeType": "image/png",
                                "sizeBytes": 2048,
                                "mediaUrl": "https://mvdcm.ecoremedia.net/ecorex-agent/client/artifacts/cover.png",
                                "artifactValidity": "valid",
                                "artifactFeedbackSignal": "default",
                            },
                            {
                                "title": "brief.pdf",
                                "kind": "file",
                                "fileName": "brief.pdf",
                                "mimeType": "application/pdf",
                                "sizeBytes": 40960,
                                "url": "https://mvdcm.ecoremedia.net/ecorex-agent/client/artifacts/brief.pdf",
                            }
                        ],
                    },
                ],
            },
            token=session["token"],
            device_id="device-1",
        )
        share = store.get_session_share(result["shareId"])
        handler = object.__new__(admin_api.AdminHandler)
        rendered = handler._share_html(result["shareId"], share)

        self.assertIn("[local-path]", json.dumps(share, ensure_ascii=False))
        self.assertIn("请看 [local-path] token=[redacted]", rendered)
        self.assertIn("已生成封面", rendered)
        self.assertIn("<b>User</b>", rendered)
        self.assertIn("<b>Agent</b>", rendered)
        self.assertTrue(share["privacy"]["includesArtifactFiles"])
        self.assertIn("https://mvdcm.ecoremedia.net/ecorex-agent/client/artifacts/cover.png", json.dumps(share, ensure_ascii=False))
        self.assertIn('<img class="artifact-preview"', rendered)
        self.assertIn('class="artifact-preview-button"', rendered)
        self.assertIn('id="image-lightbox"', rendered)
        self.assertIn("保存图片", rendered)
        self.assertIn("cover.png", rendered)
        self.assertIn("https://mvdcm.ecoremedia.net/ecorex-agent/client/artifacts/brief.pdf", rendered)
        self.assertIn('class="artifact-file-link"', rendered)
        self.assertIn("打开产物", rendered)
        self.assertIn("Shared from EcoreX", rendered)
        self.assertNotIn("C:\\Users\\Alice", json.dumps(share, ensure_ascii=False))
        self.assertNotIn("C:\\Users\\Alice", rendered)
        self.assertNotIn("abc123", json.dumps(share, ensure_ascii=False))
        self.assertNotIn("abc123", rendered)

    def test_session_share_url_uses_public_ecorex_agent_client_prefix(self):
        handler = object.__new__(admin_api.AdminHandler)
        handler.path = "/client/session-shares"
        handler.headers = {"Host": "mvdcm.ecoremedia.net", "X-Forwarded-Proto": "https"}

        with mock.patch.dict(admin_api.os.environ, {"ECOREX_PUBLIC_BASE_URL": "https://mvdcm.ecoremedia.net/ecorex-agent"}, clear=False):
            self.assertEqual(
                handler._share_url("sh_unit"),
                "https://mvdcm.ecoremedia.net/ecorex-agent/client/session-shares/sh_unit",
            )

    def test_session_share_url_infers_public_prefix_for_forwarded_production_host(self):
        handler = object.__new__(admin_api.AdminHandler)
        handler.path = "/client/session-shares"
        handler.headers = {"Host": "mvdcm.ecoremedia.net", "X-Forwarded-Proto": "https"}

        with mock.patch.dict(
            admin_api.os.environ,
            {
                "ECOREX_PUBLIC_CLIENT_BASE_URL": "",
                "ECOREX_PUBLIC_BASE_URL": "",
                "ECOREX_AGENT_PUBLIC_BASE_URL": "",
                "PUBLIC_BASE_URL": "",
            },
            clear=False,
        ):
            self.assertEqual(
                handler._share_url("sh_unit"),
                "https://mvdcm.ecoremedia.net/ecorex-agent/client/session-shares/sh_unit",
            )

    def test_session_share_url_keeps_localhost_client_prefix(self):
        handler = object.__new__(admin_api.AdminHandler)
        handler.path = "/client/session-shares"
        handler.headers = {"Host": "127.0.0.1:18084", "X-Forwarded-Proto": "http"}

        with mock.patch.dict(
            admin_api.os.environ,
            {
                "ECOREX_PUBLIC_CLIENT_BASE_URL": "",
                "ECOREX_PUBLIC_BASE_URL": "",
                "ECOREX_AGENT_PUBLIC_BASE_URL": "",
                "PUBLIC_BASE_URL": "",
            },
            clear=False,
        ):
            self.assertEqual(
                handler._share_url("sh_unit"),
                "http://127.0.0.1:18084/client/session-shares/sh_unit",
            )

    def test_runtime_audit_request_counts_are_scoped_by_identity_for_shared_request_id(self):
        store, session_a = self._store_with_session()
        store.create_user({
            "name": "Other Sync User",
            "email": "other@example.com",
            "role": "member",
            "initialPassword": "Password123",
        })
        session_b = store.login({
            "email": "other@example.com",
            "password": "Password123",
            "deviceId": "device-2",
        })
        shared_request = "shared-runtime-request"

        store.ingest_sync_events(
            {
                "events": [
                    {
                        "idempotencyKey": "event:shared:a",
                        "eventType": "run.completed",
                        "status": "completed",
                        "sessionId": "sess-a",
                        "requestId": shared_request,
                    }
                ]
            },
            token=session_a["token"],
            device_id="device-1",
        )
        store.ingest_sync_events(
            {
                "events": [
                    {
                        "idempotencyKey": "event:shared:b",
                        "eventType": "run.completed",
                        "status": "completed",
                        "sessionId": "sess-b",
                        "requestId": shared_request,
                    }
                ],
                "artifacts": [
                    {
                        "idempotencyKey": "artifact:shared:b",
                        "sessionId": "sess-b",
                        "requestId": shared_request,
                        "id": "artifact-b",
                        "title": "output.txt",
                        "kind": "file",
                        "status": "ready",
                    }
                ],
            },
            token=session_b["token"],
            device_id="device-2",
        )
        with mock.patch.dict(
            admin_api.os.environ,
            {
                "ECOREX_SYNC_PHASE2_MESSAGES_ENABLED": "1",
                "ECOREX_SYNC_MESSAGE_MAX_CONTENT_BYTES": "4096",
            },
        ):
            store.ingest_sync_messages(
                {
                    "sessionId": "sess-b",
                    "requestId": shared_request,
                    "messages": [
                        {
                            "idempotencyKey": "message:shared:b",
                            "messageId": "message-b",
                            "seq": 1,
                            "role": "assistant",
                            "content": "body is stored only for user b",
                        }
                    ],
                },
                token=session_b["token"],
                device_id="device-2",
            )

        with store.connect() as conn:
            audit_a = store.runtime_audit(conn, {"userEmail": "sync@example.com"})
            audit_b = store.runtime_audit(conn, {"userEmail": "other@example.com"})
            audit_all = store.runtime_audit(conn, {})

        self.assertEqual(audit_a["summary"]["events"], 1)
        self.assertEqual(audit_a["summary"]["artifacts"], 0)
        self.assertEqual(audit_a["summary"]["messages"], 0)
        self.assertEqual(len(audit_a["requests"]), 1)
        self.assertEqual(audit_a["requests"][0]["artifactCount"], 0)
        self.assertEqual(audit_a["requests"][0]["messageCount"], 0)
        self.assertEqual(audit_b["summary"]["events"], 1)
        self.assertEqual(audit_b["summary"]["artifacts"], 1)
        self.assertEqual(audit_b["summary"]["messages"], 1)
        self.assertEqual(len(audit_b["requests"]), 1)
        self.assertEqual(audit_b["requests"][0]["artifactCount"], 1)
        self.assertEqual(audit_b["requests"][0]["messageCount"], 1)
        self.assertEqual(audit_all["summary"]["requests"], 2)
        self.assertEqual(audit_all["summary"]["artifactRequests"], 1)
        self.assertEqual(audit_all["summary"]["messageRequests"], 1)
        self.assertEqual(len(audit_all["requests"]), 2)
        self.assertEqual(sorted(row["artifactCount"] for row in audit_all["requests"]), [0, 1])
        self.assertEqual(sorted(row["messageCount"] for row in audit_all["requests"]), [0, 1])

    def test_runtime_audit_filters_by_user_and_created_range(self):
        store, session = self._store_with_session()
        store.ingest_sync_events(
            {
                "events": [
                    {
                        "idempotencyKey": "event:range:old",
                        "eventType": "run.completed",
                        "status": "completed",
                        "sessionId": "sess-range-old",
                        "requestId": "req-range-old",
                        "createdAt": "2026-07-01T12:00:00+08:00",
                    },
                    {
                        "idempotencyKey": "event:range:inside",
                        "eventType": "run.completed",
                        "status": "completed",
                        "sessionId": "sess-range-inside",
                        "requestId": "req-range-inside",
                        "createdAt": "2026-07-02T12:00:00+08:00",
                    },
                ],
                "artifacts": [
                    {
                        "idempotencyKey": "artifact:range:inside",
                        "sessionId": "sess-range-inside",
                        "requestId": "req-range-inside",
                        "id": "artifact-range-inside",
                        "title": "inside.txt",
                        "kind": "file",
                        "status": "ready",
                        "createdAt": "2026-07-02T12:01:00+08:00",
                    }
                ],
            },
            token=session["token"],
            device_id="device-1",
        )

        with store.connect() as conn:
            audit = store.runtime_audit(
                conn,
                {
                    "userEmail": "sync@example.com",
                    "start": "2026-07-02T00:00:00+08:00",
                    "end": "2026-07-03T00:00:00+08:00",
                },
            )

        self.assertEqual(audit["summary"]["events"], 1)
        self.assertEqual(audit["summary"]["artifacts"], 1)
        self.assertEqual(audit["summary"]["requests"], 1)
        self.assertEqual(len(audit["recentEvents"]), 1)
        self.assertEqual(len(audit["effectiveArtifacts"]), 1)
        self.assertEqual(audit["recentEvents"][0]["eventType"], "run.completed")

    def test_phase1_sync_requires_valid_user_session_when_requested(self):
        store, _session = self._store_with_session()
        with self.assertRaises(PermissionError):
            store.ingest_sync_events(
                {"events": [{"eventType": "run.completed", "sessionId": "s", "requestId": "r"}]},
                token="",
                device_id="device-1",
                require_user=True,
            )

    def test_sync_policy_keeps_phase2_and_phase3_closed_by_default(self):
        store, session = self._store_with_session()
        with mock.patch.dict(
            admin_api.os.environ,
            {
                "ECOREX_SYNC_PHASE2_MESSAGES_ENABLED": "0",
                "ECOREX_SYNC_PHASE3_ARTIFACT_FILES_ENABLED": "0",
            },
        ):
            status = store.sync_status()
            policy = status["syncPolicy"]

            self.assertTrue(policy["phase1"]["eventsEnabled"])
            self.assertTrue(policy["phase1"]["artifactMetadataEnabled"])
            self.assertFalse(policy["phase1"]["storesChatBodies"])
            self.assertFalse(policy["phase1"]["storesArtifactFiles"])
            self.assertFalse(policy["phase2"]["chatBodiesEnabled"])
            self.assertFalse(policy["phase3"]["artifactFilesEnabled"])
            self.assertTrue(policy["phase3"]["implemented"])
            self.assertTrue(policy["phase3"]["killSwitch"])
            self.assertGreater(policy["phase3"]["chunkBytes"], 0)
            self.assertIn("syncSummary", status)

            with self.assertRaises(admin_api.ForbiddenError):
                store.ingest_sync_messages(
                    {
                        "messages": [
                            {
                                "idempotencyKey": "message:req-1:1",
                                "sessionId": "sess-1",
                                "requestId": "req-1",
                                "role": "user",
                                "content": "phase 2 body must stay closed",
                            }
                        ]
                    },
                    token=session["token"],
                    device_id="device-1",
                    require_user=True,
                )

            with self.assertRaises(admin_api.ForbiddenError):
                store.ingest_sync_artifact_file(
                    {
                        "artifactId": "artifact-1",
                        "sha256": "0" * 64,
                        "chunkIndex": 0,
                        "contentBase64": "ZmlsZSBib2R5IG11c3Qgc3RheSBjbG9zZWQ=",
                    },
                    token=session["token"],
                    device_id="device-1",
                    require_user=True,
                )

            with store.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sync_events").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sync_artifacts").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sync_messages").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sync_artifact_files").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sync_artifact_file_chunks").fetchone()[0], 0)

    def test_admin_runtime_audit_panel_consumes_backend_projection(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        admin_html = (root / "deploy" / "ecorex-site" / "admin" / "index.html").read_text(encoding="utf-8")
        admin_js = (root / "deploy" / "ecorex-site" / "admin" / "admin.js").read_text(encoding="utf-8")
        admin_api_source = (root / "deploy" / "ecorex-admin-api" / "ecorex_admin_api.py").read_text(encoding="utf-8")
        web_channel_source = (root / "channel" / "web" / "web_channel.py").read_text(encoding="utf-8")
        caddy_source = (root / "deploy" / "ecorex-site" / "caddy" / "ecorex-agent.routes.caddy").read_text(encoding="utf-8")
        nginx_source = (root / "deploy" / "ecorex-site" / "nginx" / "ecorex-agent.conf.example").read_text(encoding="utf-8")

        self.assertIn('data-panel="runtime-audit"', admin_html)
        self.assertIn("data-runtime-audit-summary", admin_html)
        self.assertIn("data-runtime-audit-actions", admin_html)
        self.assertIn("data-runtime-audit-effective-artifacts", admin_html)
        self.assertIn("data-runtime-audit-feedback-traces", admin_html)
        self.assertIn("data-runtime-audit-events", admin_html)
        self.assertIn("runtimeAudit", admin_js)
        self.assertIn("function renderRuntimeAudit()", admin_js)
        self.assertIn("actionTypeCounts", admin_js)
        self.assertIn("effectiveArtifacts", admin_js)
        self.assertIn("feedbackTraces", admin_js)
        self.assertIn("eventTypeCounts", admin_js)
        self.assertIn("/ecorex-agent/usage-panel/api", admin_api_source)
        self.assertIn("feedbackShareUrl", web_channel_source)
        self.assertIn("/ecorex-agent/usage-panel/", caddy_source)
        self.assertIn("/ecorex-agent/usage-panel/", nginx_source)
        self.assertNotIn("sync_events", admin_js)
        self.assertNotIn("request_id", admin_js)

    def test_phase3_sync_artifact_files_stores_chunks_and_dedupes_when_enabled(self):
        store, session = self._store_with_session()
        data = b"hello phase three artifact file body"
        content_sha256 = hashlib.sha256(data).hexdigest()
        chunks = [data[:11], data[11:]]

        def payload_for(artifact_id, index):
            chunk = chunks[index]
            return {
                "sessionId": "sess-3",
                "requestId": "req-3",
                "artifactId": artifact_id,
                "fileSyncKey": f"artifact-file:req-3:{artifact_id}:{content_sha256}",
                "artifact": {
                    "safeArtifactId": artifact_id,
                    "title": "phase3-output.txt",
                    "pathHash": "safe-path-hash",
                    "pathExt": ".txt",
                    "mimeType": "text/plain",
                    "content": "must not land in metadata",
                },
                "title": "phase3-output.txt",
                "mimeType": "text/plain",
                "totalSizeBytes": len(data),
                "contentSha256": content_sha256,
                "chunkIndex": index,
                "chunkCount": len(chunks),
                "chunkSha256": hashlib.sha256(chunk).hexdigest(),
                "contentBase64": base64.b64encode(chunk).decode("ascii"),
            }

        with mock.patch.dict(
            admin_api.os.environ,
            {
                "ECOREX_SYNC_PHASE3_ARTIFACT_FILES_ENABLED": "1",
                "ECOREX_SYNC_ARTIFACT_MAX_AUTO_BYTES": "4096",
                "ECOREX_SYNC_ARTIFACT_CHUNK_BYTES": "1024",
                "ECOREX_SYNC_ARTIFACT_BYTES_PER_SECOND": "0",
            },
        ):
            first = store.ingest_sync_artifact_file(payload_for("artifact-a", 0), token=session["token"], device_id="device-1")
            second = store.ingest_sync_artifact_file(payload_for("artifact-a", 1), token=session["token"], device_id="device-1")
            repeat = store.ingest_sync_artifact_file(payload_for("artifact-a", 1), token=session["token"], device_id="device-1")
            reused = store.ingest_sync_artifact_file(payload_for("artifact-b", 0), token=session["token"], device_id="device-1")
            status = store.sync_status()

        self.assertFalse(first["complete"])
        self.assertTrue(second["complete"])
        self.assertTrue(repeat["deduped"])
        self.assertTrue(reused["deduped"])
        self.assertTrue(reused["complete"])
        self.assertEqual(status["syncSummary"]["artifactFiles"], 2)
        self.assertEqual(status["syncSummary"]["artifactFilesComplete"], 2)
        self.assertEqual(status["syncSummary"]["artifactFileChunks"], 2)
        self.assertEqual(status["syncSummary"]["artifactFileStoredBytes"], len(data))
        with store.connect() as conn:
            files = conn.execute("SELECT * FROM sync_artifact_files ORDER BY artifact_id").fetchall()
            chunk_rows = conn.execute("SELECT * FROM sync_artifact_file_chunks ORDER BY chunk_index").fetchall()
        self.assertEqual([row["artifact_id"] for row in files], ["artifact-a", "artifact-b"])
        self.assertEqual(len(chunk_rows), 2)
        self.assertEqual(b"".join(bytes(row["data"]) for row in chunk_rows), data)
        metadata = json.loads(files[0]["metadata"])
        serialized_metadata = json.dumps(metadata, ensure_ascii=False)
        self.assertNotIn("must not land in metadata", serialized_metadata)

    def test_phase3_sync_artifact_files_enforces_size_and_rate_limits(self):
        store, session = self._store_with_session()
        data = b"abcdef"
        content_sha256 = hashlib.sha256(data).hexdigest()
        payload = {
            "sessionId": "sess-3-limit",
            "requestId": "req-3-limit",
            "artifactId": "artifact-limit",
            "totalSizeBytes": len(data),
            "contentSha256": content_sha256,
            "chunkIndex": 0,
            "chunkCount": 1,
            "chunkSha256": content_sha256,
            "contentBase64": base64.b64encode(data).decode("ascii"),
        }

        with mock.patch.dict(
            admin_api.os.environ,
            {
                "ECOREX_SYNC_PHASE3_ARTIFACT_FILES_ENABLED": "1",
                "ECOREX_SYNC_ARTIFACT_MAX_AUTO_BYTES": "3",
                "ECOREX_SYNC_ARTIFACT_CHUNK_BYTES": "1024",
                "ECOREX_SYNC_ARTIFACT_BYTES_PER_SECOND": "0",
            },
        ):
            with self.assertRaises(ValueError):
                store.ingest_sync_artifact_file(payload, token=session["token"], device_id="device-1")

        with mock.patch.dict(
            admin_api.os.environ,
            {
                "ECOREX_SYNC_PHASE3_ARTIFACT_FILES_ENABLED": "1",
                "ECOREX_SYNC_ARTIFACT_MAX_AUTO_BYTES": "4096",
                "ECOREX_SYNC_ARTIFACT_CHUNK_BYTES": "1024",
                "ECOREX_SYNC_ARTIFACT_BYTES_PER_SECOND": "3",
            },
        ):
            first_chunk = {
                **payload,
                "artifactId": "artifact-rate",
                "fileSyncKey": "artifact-file:req-3-limit:rate",
                "chunkCount": 2,
                "chunkSha256": hashlib.sha256(data[:3]).hexdigest(),
                "contentBase64": base64.b64encode(data[:3]).decode("ascii"),
            }
            second_chunk = {
                **payload,
                "artifactId": "artifact-rate",
                "fileSyncKey": "artifact-file:req-3-limit:rate",
                "chunkIndex": 1,
                "chunkCount": 2,
                "chunkSha256": hashlib.sha256(data[3:]).hexdigest(),
                "contentBase64": base64.b64encode(data[3:]).decode("ascii"),
            }
            store.ingest_sync_artifact_file(first_chunk, token=session["token"], device_id="device-1")
            with self.assertRaises(admin_api.RateLimitError):
                store.ingest_sync_artifact_file(second_chunk, token=session["token"], device_id="device-1")

    def test_phase2_sync_messages_stores_bodies_when_enabled_and_is_idempotent(self):
        store, session = self._store_with_session()
        payload = {
            "sessionId": "sess-2",
            "requestId": "req-2",
            "messages": [
                {
                    "idempotencyKey": "message:req-2:1",
                    "messageId": "local-user-1",
                    "seq": 1,
                    "role": "user",
                    "content": "hello phase 2 body",
                    "extras": {
                        "safe": "kept",
                        "path": "C:/Users/Alice/secret.txt",
                        "file_content": "must not store file body",
                    },
                },
                {
                    "idempotencyKey": "message:req-2:2",
                    "messageId": "local-assistant-2",
                    "seq": 2,
                    "role": "assistant",
                    "content": [{"type": "text", "text": "assistant phase 2 body"}],
                },
            ],
        }

        with mock.patch.dict(
            admin_api.os.environ,
            {
                "ECOREX_SYNC_PHASE2_MESSAGES_ENABLED": "1",
                "ECOREX_SYNC_MESSAGE_MAX_BATCH": "1000",
                "ECOREX_SYNC_MESSAGE_MAX_CONTENT_BYTES": "4096",
            },
        ):
            first = store.ingest_sync_messages(payload, token=session["token"], device_id="device-1")
            second = store.ingest_sync_messages(payload, token=session["token"], device_id="device-1")
            status = store.sync_status()

        self.assertEqual(first["messagesAccepted"], 2)
        self.assertEqual(second["messagesAccepted"], 2)
        self.assertEqual(status["syncSummary"]["messages"], 2)
        self.assertEqual(status["syncSummary"]["messageSessions"], 1)
        self.assertEqual(status["syncSummary"]["messageRequests"], 1)
        with store.connect() as conn:
            rows = conn.execute("SELECT * FROM sync_messages ORDER BY seq").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(json.loads(rows[0]["content"]), "hello phase 2 body")
        self.assertEqual(json.loads(rows[1]["content"])[0]["text"], "assistant phase 2 body")
        self.assertEqual(rows[0]["role"], "user")
        self.assertTrue(rows[0]["content_sha256"])
        self.assertGreater(rows[0]["content_size_bytes"], 0)
        extras = json.loads(rows[0]["extras"])
        serialized_extras = json.dumps(extras, ensure_ascii=False)
        self.assertEqual(extras["safe"], "kept")
        self.assertEqual(extras["path"], "[omitted]")
        self.assertEqual(extras["file_content"], "[omitted]")
        self.assertNotIn("C:/Users/Alice", serialized_extras)
        self.assertNotIn("must not store file body", serialized_extras)

    def test_phase2_sync_messages_enforces_content_size_limit(self):
        store, session = self._store_with_session()
        with mock.patch.dict(
            admin_api.os.environ,
            {
                "ECOREX_SYNC_PHASE2_MESSAGES_ENABLED": "1",
                "ECOREX_SYNC_MESSAGE_MAX_CONTENT_BYTES": "1024",
            },
        ):
            with self.assertRaises(ValueError):
                store.ingest_sync_messages(
                    {
                        "sessionId": "sess-size",
                        "requestId": "req-size",
                        "messages": [
                            {
                                "idempotencyKey": "message:req-size:1",
                                "role": "user",
                                "content": "x" * 2048,
                            }
                        ],
                    },
                    token=session["token"],
                    device_id="device-1",
                    require_user=True,
                )


if __name__ == "__main__":
    unittest.main()
