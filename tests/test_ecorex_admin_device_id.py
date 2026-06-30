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

        self.assertIn('data-panel="runtime-audit"', admin_html)
        self.assertIn("data-runtime-audit-summary", admin_html)
        self.assertIn("data-runtime-audit-events", admin_html)
        self.assertIn("runtimeAudit", admin_js)
        self.assertIn("function renderRuntimeAudit()", admin_js)
        self.assertIn("eventTypeCounts", admin_js)
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
