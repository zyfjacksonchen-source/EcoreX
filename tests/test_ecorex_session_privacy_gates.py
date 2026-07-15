import json
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
AUDIT_SCRIPT = ROOT / "scripts" / "audit-ecorex-session-state.py"
SCAN_SCRIPT = ROOT / "scripts" / "scan-session-artifacts-privacy.py"
SECURITY_AUDIT_SCRIPT = ROOT / "scripts" / "audit-v023-security-permissions.py"
FINAL_GATE_SCRIPT = ROOT / "scripts" / "audit-v023-final-release-gate.py"
CHAT_BUBBLE_BROWSER_SMOKE = ROOT / "scripts" / "smoke-chat-attachment-bubble-browser.py"


def load_security_audit_module():
    spec = importlib.util.spec_from_file_location("audit_v023_security_permissions", SECURITY_AUDIT_SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("security audit module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_final_gate_module():
    spec = importlib.util.spec_from_file_location("audit_v023_final_release_gate", FINAL_GATE_SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("final gate module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EcoreXSessionPrivacyGateTests(unittest.TestCase):
    def _run(self, args, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, *map(str, args)],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return result

    def test_clean_audit_artifact_passes_privacy_scan(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as raw_workspace:
            workspace = Path(raw_workspace)
            db = workspace / "conversation.sqlite3"
            ui_state = workspace / ".ecorex" / "ui-state.json"
            output = workspace / "session-cross-talk-repair-dry-run.json"
            ui_state.parent.mkdir(parents=True)
            ConversationStore(db).append_messages("session-a", [{"role": "user", "content": "private prompt"}], channel_type="web")
            ui_state.write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "sessionProjects": {"session-missing": "project-private"},
                    "sessionTitles": {"session-missing": "private title"},
                    "pinnedSessions": {"session-missing": True},
                }),
                encoding="utf-8",
            )

            self._run([
                AUDIT_SCRIPT,
                "--dry-run",
                "--ui-state",
                ui_state,
                "--conversation-db",
                db,
                "--output",
                output,
                "--salt",
                "test-salt",
            ])
            scan = self._run([SCAN_SCRIPT, output, "--salt", "test-salt"])
            payload = json.loads(scan.stdout)

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["findingCount"], 0)

    def test_privacy_scan_fails_without_echoing_sensitive_values(self):
        with tempfile.TemporaryDirectory() as raw_workspace:
            workspace = Path(raw_workspace)
            bad = workspace / "session-cross-talk-bad.json"
            bad.write_text(
                json.dumps({
                    "session_id": "session-raw-123",
                    "sessionId": "session-camel-123",
                    "request_id": "request-raw-123",
                    "requestId": "request-camel-123",
                    "projectId": "project-camel-123",
                    "content": "raw prompt with C:\\Users\\person\\secret.png and sk-testtoken123456789",
                }),
                encoding="utf-8",
            )

            result = self._run([SCAN_SCRIPT, bad, "--salt", "test-salt"], check=False)
            payload = json.loads(result.stdout)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["status"], "failed")
        patterns = {item["pattern"] for item in payload["findings"]}
        self.assertIn("raw_session_id_key", patterns)
        self.assertIn("raw_session_id_camel_key", patterns)
        self.assertIn("raw_request_id_key", patterns)
        self.assertIn("raw_request_id_camel_key", patterns)
        self.assertIn("raw_project_id_camel_key", patterns)
        self.assertIn("raw_message_body_key", patterns)
        self.assertIn("windows_path", patterns)
        self.assertIn("api_token", patterns)
        self.assertTrue(all(item["artifactHash"].startswith("hmac:") for item in payload["findings"]))
        self.assertNotIn('"artifactHash": "session-cross-talk-bad', result.stdout)
        self.assertNotIn("session-raw-123", result.stdout)
        self.assertNotIn("session-camel-123", result.stdout)
        self.assertNotIn("request-raw-123", result.stdout)
        self.assertNotIn("request-camel-123", result.stdout)
        self.assertNotIn("project-camel-123", result.stdout)
        self.assertNotIn("sk-testtoken", result.stdout)
        self.assertNotIn("C:\\Users", result.stdout)

    def test_privacy_scan_fails_when_no_artifacts_are_scanned(self):
        with tempfile.TemporaryDirectory() as raw_workspace:
            missing = Path(raw_workspace) / "missing-*.json"
            output = Path(raw_workspace) / "scan.json"
            result = self._run([
                SCAN_SCRIPT,
                missing,
                "--json-output",
                output,
                "--salt",
                "test-salt",
            ], check=False)
            payload = json.loads(result.stdout)
            saved = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["filesScanned"], 0)
        self.assertEqual(payload["inputError"], "no_artifacts_scanned")
        self.assertEqual(saved["inputError"], "no_artifacts_scanned")

    def test_privacy_scan_does_not_treat_localhost_url_as_windows_path(self):
        with tempfile.TemporaryDirectory() as raw_workspace:
            workspace = Path(raw_workspace)
            clean = workspace / "localhost-url.json"
            dirty = workspace / "windows-path.json"
            clean.write_text(
                json.dumps({
                    "endpoint": "http://127.0.0.1:9222",
                    "browserUrl": "http://localhost:9222/json/version",
                }),
                encoding="utf-8",
            )
            dirty.write_text(
                json.dumps({"path": r"C:\Users\alice\private.png"}),
                encoding="utf-8",
            )

            clean_result = self._run([SCAN_SCRIPT, clean, "--salt", "test-salt"])
            dirty_result = self._run([SCAN_SCRIPT, dirty, "--salt", "test-salt"], check=False)
            clean_payload = json.loads(clean_result.stdout)
            dirty_payload = json.loads(dirty_result.stdout)

        self.assertEqual(clean_payload["status"], "success")
        self.assertEqual(clean_payload["findingCount"], 0)
        self.assertEqual(dirty_result.returncode, 1)
        patterns = {item["pattern"] for item in dirty_payload["findings"]}
        self.assertIn("windows_path", patterns)

    def test_privacy_scan_does_not_treat_low_risk_reason_as_api_token(self):
        with tempfile.TemporaryDirectory() as raw_workspace:
            clean = Path(raw_workspace) / "permission-matrix.json"
            clean.write_text(
                json.dumps({
                    "schemaVersion": "web-permission-matrix-v1",
                    "checks": [
                        {"reason": "default-low-risk-optional-ability-status"},
                        {"reason": "default-low-risk-agent-capability-status"},
                        {"reason": "default-low-risk-browser-snapshot"},
                    ],
                }),
                encoding="utf-8",
            )

            result = self._run([SCAN_SCRIPT, clean, "--salt", "test-salt"])
            payload = json.loads(result.stdout)

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["findingCount"], 0)

    def test_v023_security_permission_audit_passes_and_self_scan_is_clean(self):
        with tempfile.TemporaryDirectory() as raw_workspace:
            output = Path(raw_workspace) / "security-permission-audit.json"
            scan_output = Path(raw_workspace) / "security-permission-audit-scan.json"
            result = self._run([
                SECURITY_AUDIT_SCRIPT,
                "--output",
                output,
                "--salt",
                "test-security-audit",
            ])
            payload = json.loads(result.stdout)
            scan = self._run([
                SCAN_SCRIPT,
                output,
                "--json-output",
                scan_output,
                "--salt",
                "test-security-audit-scan",
            ])
            scan_payload = json.loads(scan.stdout)

        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["slice"], "R23-16")
        self.assertEqual(payload["metrics"]["failedCheckCount"], 0)
        self.assertEqual(payload["metrics"]["scanIssueCount"], 0)
        self.assertEqual(payload["metrics"]["findingBucketCount"], 0)
        self.assertGreaterEqual(payload["metrics"]["checkCount"], 12)
        self.assertEqual(scan_payload["status"], "success")
        self.assertEqual(scan_payload["findingCount"], 0)

    def test_v023_security_permission_audit_fails_closed_on_artifact_privacy_drift(self):
        module = load_security_audit_module()
        scanner = module._load_module("ecorex_artifact_privacy_scanner_test", SCAN_SCRIPT)
        with tempfile.TemporaryDirectory() as raw_workspace:
            workspace = Path(raw_workspace)
            (workspace / "bad.json").write_text(
                json.dumps({"session_id": "raw-session", "content": "private"}),
                encoding="utf-8",
            )
            (workspace / "bad-scan.json").write_text(
                json.dumps({"status": "success", "findingCount": 0}),
                encoding="utf-8",
            )
            original_root = module.ROOT
            original_required = module.REQUIRED_ARTIFACTS
            original_direct = module.DIRECT_SCAN_ARTIFACTS
            try:
                module.ROOT = workspace
                module.REQUIRED_ARTIFACTS = (("bad-artifact", "bad.json", "bad-scan.json"),)
                module.DIRECT_SCAN_ARTIFACTS = ()
                checks, scan_issues, finding_buckets, scanned_count = module._artifact_checks(
                    scanner,
                    module._salt("test-security-audit"),
                )
            finally:
                module.ROOT = original_root
                module.REQUIRED_ARTIFACTS = original_required
                module.DIRECT_SCAN_ARTIFACTS = original_direct

        self.assertEqual(scanned_count, 2)
        self.assertFalse(scan_issues)
        self.assertTrue(any(item["status"] == "pass" for item in checks))
        self.assertGreaterEqual(len(finding_buckets), 2)
        serialized = json.dumps(finding_buckets, ensure_ascii=False)
        self.assertNotIn("raw-session", serialized)
        self.assertNotIn("private", serialized)

    def _write_final_gate_fixture(self, module, root: Path) -> None:
        docs = root / "docs" / "v0.2.3"
        artifacts = docs / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        docs.mkdir(parents=True, exist_ok=True)
        docs.joinpath("acceptance-checklist.md").write_text(
            "\n".join([
                "| Slice | Requirement | Status | Evidence |",
                "| --- | --- | --- | --- |",
                "| R23-16P | performance final gate | PASS | fixture |",
                "| R23-17 | final release gate | PASS | fixture |",
                "| R23-20 | session final gate | PASS | fixture |",
                "| R23-21 | chat bubble final gate | PASS | fixture |",
                "",
            ]),
            encoding="utf-8",
        )

        def payload_for(item_id: str, expected: str) -> dict:
            payload = {"status": expected, "redacted": True}
            if item_id.endswith("scan"):
                payload.update({"findingCount": 0, "imageOcrScannedCount": 0, "imageOcrUnavailableCount": 0, "imageOcrErrorCount": 0})
            if item_id == "cross-talk-screenshot-scan":
                payload.update({"findingCount": 0, "imageOcrScannedCount": 2, "imageOcrUnavailableCount": 0, "imageOcrErrorCount": 0})
            if item_id == "performance":
                payload["metrics"] = {
                    "matrixScenarioCount": 8,
                    "scenarioPairCount": 7,
                    "requiredScenarioMissingCount": 0,
                    "matrixConfigIssueCount": 0,
                    "missingMainArtifactCount": 0,
                    "missingScanArtifactCount": 0,
                    "scanNotCleanCount": 0,
                    "findingBucketCount": 0,
                    "scannedArtifactCount": 14,
                }
            if item_id == "session-browser-smoke":
                payload.update({
                    "consoleErrorCount": 0,
                    "metrics": {
                        "sessionQueryIncludePinned": True,
                        "includedPinnedCount": 3,
                        "generalRows": 4,
                        "projectRows": 2,
                        "pinnedGroupBeforeUnpinned": True,
                        "projectPinnedGroupBeforeUnpinned": True,
                        "backendOwnerWonOverLocalStaleBinding": True,
                        "projectOwnerStayedInProjectBucket": True,
                        "renameDidNotPin": True,
                    },
                })
            if item_id == "session-refresh-smoke":
                payload.update({
                    "consoleErrorCount": 0,
                    "metrics": {
                        "race": {
                            "staleHistoryIgnored": True,
                            "activeSessionContentStable": True,
                            "mismatchDiagnosticObserved": True,
                            "streamExpectedSessionObserved": True,
                        },
                        "refresh": {
                            "refreshKeptCleanSession": True,
                            "backendHistoryFetched": True,
                            "refreshRejectedLateSession": True,
                        },
                    },
                })
            if item_id == "chat-browser-smoke":
                payload.update({
                    "consoleErrorCount": 0,
                    "redacted": True,
                    "metrics": {
                        "userMessageCount": 1,
                        "attachmentButtonCount": 2,
                        "imageAttachmentCount": 1,
                        "textIncludesCodex": True,
                        "runCenterHidden": True,
                    },
                })
            return payload

        for item_id, rel, expected in module.REQUIRED_CLEAN_ARTIFACTS:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload_for(item_id, expected), ensure_ascii=False, indent=2), encoding="utf-8")

    def test_v023_final_release_gate_passes_when_promoted_and_evidence_is_strong(self):
        module = load_final_gate_module()
        audit = module.build_audit(salt=module._salt("test-final-gate"))

        self.assertEqual(audit["status"], "pass")
        self.assertTrue(audit["complete"])
        self.assertEqual(audit["metrics"]["blockerCount"], 0)
        self.assertGreaterEqual(audit["metrics"]["artifactCount"], 12)
        self.assertTrue(all(item["clean"] for item in audit["artifactStates"]))

    def test_v023_final_release_gate_fails_closed_on_weak_performance_or_session_evidence(self):
        module = load_final_gate_module()
        with tempfile.TemporaryDirectory() as raw_workspace:
            root = Path(raw_workspace)
            self._write_final_gate_fixture(module, root)
            original_root = module.ROOT
            original_acceptance = module.ACCEPTANCE
            try:
                module.ROOT = root
                module.ACCEPTANCE = root / "docs" / "v0.2.3" / "acceptance-checklist.md"

                performance_path = root / "docs" / "v0.2.3" / "artifacts" / "perf-evidence-audit.json"
                performance = json.loads(performance_path.read_text(encoding="utf-8"))
                performance["metrics"]["missingMainArtifactCount"] = 1
                performance_path.write_text(json.dumps(performance), encoding="utf-8")
                audit = module.build_audit(salt=module._salt("test-final-gate"))
                blocker_ids = {item["id"] for item in audit["blockers"]}
                self.assertIn("artifact-not-clean-performance", blocker_ids)

                self._write_final_gate_fixture(module, root)
                session_path = root / "docs" / "v0.2.3" / "artifacts" / "session-cross-talk-browser-smoke.json"
                session = json.loads(session_path.read_text(encoding="utf-8"))
                session["metrics"]["projectPinnedGroupBeforeUnpinned"] = False
                session_path.write_text(json.dumps(session), encoding="utf-8")
                audit = module.build_audit(salt=module._salt("test-final-gate"))
                blocker_ids = {item["id"] for item in audit["blockers"]}
                self.assertIn("artifact-not-clean-session-browser-smoke", blocker_ids)
            finally:
                module.ROOT = original_root
                module.ACCEPTANCE = original_acceptance

    def test_chat_attachment_bubble_integrated_browser_smoke_contract(self):
        source = CHAT_BUBBLE_BROWSER_SMOKE.read_text(encoding="utf-8")
        self.assertIn("static_site_server", source)
        self.assertIn("desktop/dist", source)
        self.assertIn("/api/history", source)
        self.assertIn("extras", source)
        self.assertIn("attachments", source)
        self.assertIn("Chat Attachment Bubble Integrated Smoke", source)
        self.assertIn(".message.user.has-files", source)
        self.assertIn(".message-text-bubble", source)
        self.assertIn("Run Center", source)
        self.assertIn("artifact://docx-pptx-ref", source)

    def test_chat_attachment_bubble_integrated_browser_artifact_contract(self):
        artifact_path = ROOT / "docs" / "v0.2.3" / "artifacts" / "chat-attachment-bubble-browser-smoke.json"
        privacy_path = ROOT / "docs" / "v0.2.3" / "artifacts" / "chat-attachment-bubble-browser-privacy-scan.json"
        self.assertTrue(artifact_path.exists(), "integrated chat bubble browser smoke artifact is missing")
        self.assertTrue(privacy_path.exists(), "integrated chat bubble privacy scan artifact is missing")
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        privacy = json.loads(privacy_path.read_text(encoding="utf-8"))
        metrics = artifact.get("metrics") or {}

        self.assertEqual(artifact.get("status"), "PASS")
        self.assertEqual(artifact.get("consoleErrorCount"), 0)
        self.assertTrue(artifact.get("redacted"))
        self.assertEqual(metrics.get("userMessageCount"), 1)
        self.assertEqual(metrics.get("attachmentButtonCount"), 2)
        self.assertEqual(metrics.get("imageAttachmentCount"), 1)
        self.assertTrue(metrics.get("textIncludesCodex"))
        self.assertTrue(metrics.get("runCenterHidden"))
        self.assertEqual(privacy.get("status"), "success")
        self.assertEqual(privacy.get("findingCount"), 0)


if __name__ == "__main__":
    unittest.main()
