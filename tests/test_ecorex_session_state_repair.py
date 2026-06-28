import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
AUDIT_SCRIPT = ROOT / "scripts" / "audit-ecorex-session-state.py"


class EcoreXSessionStateRepairTests(unittest.TestCase):
    def _project(self, project_id: str, root: Path) -> dict:
        return {
            "projectId": project_id,
            "projectName": f"Project {project_id}",
            "projectPath": str(root / project_id),
            "memoryPath": str(root / project_id / ".ecorex" / "project-memory.md"),
            "dreamsPath": str(root / project_id / ".ecorex" / "dreams"),
        }

    def _run_audit(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(AUDIT_SCRIPT), *args],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return result

    def test_dry_run_reports_repairable_ui_metadata_without_raw_ids_or_paths(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as raw_workspace:
            workspace = Path(raw_workspace)
            db = workspace / "conversation.sqlite3"
            ui_state = workspace / ".ecorex" / "ui-state.json"
            output = workspace / "dry-run.json"
            ui_state.parent.mkdir(parents=True)
            store = ConversationStore(db)
            store.append_messages("session-general", [{"role": "user", "content": "hello general"}], channel_type="web")
            store.append_messages(
                "session-project",
                [{"role": "user", "content": "hello project"}],
                channel_type="web",
                project_context=self._project("project-a", workspace),
            )
            before = {
                "schemaVersion": 1,
                "projects": [{"id": "project-a", "name": "Project A"}],
                "sessionProjects": {
                    "session-general": "project-a",
                    "session-project": "project-b",
                    "session-missing": "project-a",
                    "runtime-0": "project-a",
                },
                "sessionProjectBindings": {
                    "session-general": {"projectId": "project-a", "projectPath": str(workspace / "project-a")},
                    "session-project": {"projectId": "project-b"},
                    "session-missing": {"projectId": "project-a"},
                },
                "sessionTitles": {"session-missing": "raw title should not leak", "session-general": "General"},
                "pinnedSessions": {"session-missing": True, "session-general": True},
                "sessionUiState": {
                    "session-missing": {"messages": [{"role": "user", "content": "raw prompt should not leak"}]},
                    "runtime-0": {},
                },
            }
            ui_state.write_text(json.dumps(before, ensure_ascii=False), encoding="utf-8")

            result = self._run_audit(
                "--dry-run",
                "--ui-state",
                str(ui_state),
                "--conversation-db",
                str(db),
                "--output",
                str(output),
                "--salt",
                "test-salt",
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            persisted = json.loads(ui_state.read_text(encoding="utf-8"))
            output_text = output.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(persisted, before)
        self.assertGreater(report["summary"]["repairActionCount"], 0)
        self.assertIn("stale_local_project_for_backend_general", report["summary"]["issueTypes"])
        self.assertIn("local_project_mismatch_backend", report["summary"]["issueTypes"])
        self.assertIn("orphan_sessionProjects", report["summary"]["issueTypes"])
        self.assertIn("runtime_fallback_id", report["summary"]["issueTypes"])
        for raw in ("session-general", "session-project", "session-missing", "project-a", "project-b", str(workspace), "raw prompt"):
            self.assertNotIn(raw, output_text)
        self.assertIn("hmac:", output_text)
        self.assertFalse(report["privacy"]["rawIdentifiersIncluded"])
        self.assertFalse(report["privacy"]["rawPathsIncluded"])

    def test_apply_repairs_ui_metadata_and_rollback_restores_original(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as raw_workspace:
            workspace = Path(raw_workspace)
            db = workspace / "conversation.sqlite3"
            ui_state = workspace / ".ecorex" / "ui-state.json"
            output = workspace / "apply.json"
            backup_dir = workspace / "backup"
            active = workspace / "active.json"
            ui_state.parent.mkdir(parents=True)
            active.write_text(json.dumps({"activeRequests": []}), encoding="utf-8")
            store = ConversationStore(db)
            store.append_messages("session-general", [{"role": "user", "content": "keep message"}], channel_type="web")
            store.append_messages(
                "session-project",
                [{"role": "user", "content": "keep project message"}],
                channel_type="web",
                project_context=self._project("project-a", workspace),
            )
            original = {
                "schemaVersion": 1,
                "projects": [{"id": "project-a"}],
                "sessionProjects": {"session-general": "project-a", "session-project": "project-b", "session-missing": "project-a"},
                "sessionProjectBindings": {
                    "session-general": {"projectId": "project-a"},
                    "session-project": {"projectId": "project-b"},
                    "session-missing": {"projectId": "project-a"},
                },
                "sessionTitles": {"session-missing": "orphan title", "session-general": "General"},
                "pinnedSessions": {"session-missing": True, "session-general": True},
                "sessionUiState": {
                    "session-missing": {"messages": [{"role": "user", "content": "local draft remains"}]},
                    "runtime-0": {},
                },
            }
            ui_state.write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")

            self._run_audit(
                "--apply",
                "--ui-state",
                str(ui_state),
                "--conversation-db",
                str(db),
                "--backup-dir",
                str(backup_dir),
                "--active-requests-json",
                str(active),
                "--output",
                str(output),
                "--salt",
                "test-salt",
            )
            repaired = json.loads(ui_state.read_text(encoding="utf-8"))
            apply_report = json.loads(output.read_text(encoding="utf-8"))
            messages_after_apply = store.load_history_page("session-general", page=1, page_size=20)["messages"]

            self._run_audit(
                "--rollback",
                str(backup_dir / "manifest.json"),
                "--ui-state",
                str(ui_state),
                "--conversation-db",
                str(db),
                "--salt",
                "test-salt",
            )
            restored = json.loads(ui_state.read_text(encoding="utf-8"))

        self.assertNotIn("session-general", repaired["sessionProjects"])
        self.assertNotIn("session-project", repaired["sessionProjects"])
        self.assertNotIn("session-missing", repaired["sessionProjects"])
        self.assertNotIn("session-missing", repaired["sessionTitles"])
        self.assertNotIn("session-missing", repaired["pinnedSessions"])
        self.assertIn("session-missing", repaired["sessionUiState"])
        self.assertNotIn("runtime-0", repaired["sessionUiState"])
        self.assertEqual(messages_after_apply[0]["content"], "keep message")
        self.assertEqual(restored["sessionProjects"], original["sessionProjects"])
        self.assertEqual(restored["pinnedSessions"], original["pinnedSessions"])
        self.assertEqual(apply_report["backup"]["fileCount"], 2)
        self.assertTrue(apply_report["backup"]["manifestIdHash"].startswith("hmac:"))
        self.assertNotIn("Sha256", json.dumps(apply_report))
        self.assertNotIn("manifest.json", json.dumps(apply_report))
        self.assertEqual(apply_report["postRepairSummary"]["issueTypes"].get("orphan_sessionProjects", 0), 0)

    def test_apply_requires_active_request_snapshot(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as raw_workspace:
            workspace = Path(raw_workspace)
            db = workspace / "conversation.sqlite3"
            ui_state = workspace / ".ecorex" / "ui-state.json"
            output = workspace / "blocked.json"
            ui_state.parent.mkdir(parents=True)
            ConversationStore(db).append_messages("session-general", [{"role": "user", "content": "hello"}], channel_type="web")
            before = {"schemaVersion": 1, "sessionProjects": {"session-general": "project-a"}}
            ui_state.write_text(json.dumps(before), encoding="utf-8")

            result = self._run_audit(
                "--apply",
                "--ui-state",
                str(ui_state),
                "--conversation-db",
                str(db),
                "--output",
                str(output),
                "--salt",
                "test-salt",
                check=False,
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            persisted_after = json.loads(ui_state.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["code"], "ACTIVE_REQUEST_SNAPSHOT_REQUIRED")
        self.assertEqual(report["activeRequests"]["valid"], False)
        self.assertEqual(persisted_after, before)

    def test_apply_rejects_active_snapshot_without_known_request_collection(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as raw_workspace:
            workspace = Path(raw_workspace)
            db = workspace / "conversation.sqlite3"
            ui_state = workspace / ".ecorex" / "ui-state.json"
            active = workspace / "wrong-active.json"
            output = workspace / "blocked.json"
            ui_state.parent.mkdir(parents=True)
            ConversationStore(db).append_messages("session-general", [{"role": "user", "content": "hello"}], channel_type="web")
            before = {"schemaVersion": 1, "sessionProjects": {"session-general": "project-a"}}
            ui_state.write_text(json.dumps(before), encoding="utf-8")
            active.write_text(json.dumps({}), encoding="utf-8")

            result = self._run_audit(
                "--apply",
                "--ui-state",
                str(ui_state),
                "--conversation-db",
                str(db),
                "--active-requests-json",
                str(active),
                "--output",
                str(output),
                "--salt",
                "test-salt",
                check=False,
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            persisted_after = json.loads(ui_state.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["code"], "ACTIVE_REQUEST_SNAPSHOT_REQUIRED")
        self.assertEqual(report["activeRequests"]["reason"], "snapshot_missing_request_collection")
        self.assertEqual(persisted_after, before)

    def test_apply_refuses_missing_conversation_db(self):
        with tempfile.TemporaryDirectory() as raw_workspace:
            workspace = Path(raw_workspace)
            missing_db = workspace / "missing.sqlite3"
            ui_state = workspace / ".ecorex" / "ui-state.json"
            active = workspace / "active.json"
            output = workspace / "blocked.json"
            ui_state.parent.mkdir(parents=True)
            active.write_text(json.dumps({"activeRequests": []}), encoding="utf-8")
            before = {"schemaVersion": 1, "sessionProjects": {"session-real": "project-a"}}
            ui_state.write_text(json.dumps(before), encoding="utf-8")

            result = self._run_audit(
                "--apply",
                "--ui-state",
                str(ui_state),
                "--conversation-db",
                str(missing_db),
                "--active-requests-json",
                str(active),
                "--output",
                str(output),
                "--salt",
                "test-salt",
                check=False,
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            persisted_after = json.loads(ui_state.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["code"], "CONVERSATION_DB_REQUIRED")
        self.assertEqual(persisted_after, before)

    def test_apply_refuses_backend_row_limit_exceeded(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as raw_workspace:
            workspace = Path(raw_workspace)
            db = workspace / "conversation.sqlite3"
            ui_state = workspace / ".ecorex" / "ui-state.json"
            active = workspace / "active.json"
            output = workspace / "blocked.json"
            ui_state.parent.mkdir(parents=True)
            active.write_text(json.dumps({"activeRequests": []}), encoding="utf-8")
            store = ConversationStore(db)
            store.append_messages("session-one", [{"role": "user", "content": "one"}], channel_type="web")
            store.append_messages("session-two", [{"role": "user", "content": "two"}], channel_type="web")
            before = {"schemaVersion": 1, "sessionProjects": {"session-one": "project-a"}}
            ui_state.write_text(json.dumps(before), encoding="utf-8")

            result = self._run_audit(
                "--apply",
                "--ui-state",
                str(ui_state),
                "--conversation-db",
                str(db),
                "--active-requests-json",
                str(active),
                "--max-backend-sessions",
                "1",
                "--output",
                str(output),
                "--salt",
                "test-salt",
                check=False,
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            persisted_after = json.loads(ui_state.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["code"], "BACKEND_SESSION_ROW_LIMIT_EXCEEDED")
        self.assertEqual(report["rowCount"], 2)
        self.assertEqual(persisted_after, before)

    def test_apply_refuses_when_active_requests_are_present(self):
        from agent.memory.conversation_store import ConversationStore

        with tempfile.TemporaryDirectory() as raw_workspace:
            workspace = Path(raw_workspace)
            db = workspace / "conversation.sqlite3"
            ui_state = workspace / ".ecorex" / "ui-state.json"
            active = workspace / "active.json"
            output = workspace / "blocked.json"
            ui_state.parent.mkdir(parents=True)
            ConversationStore(db).append_messages("session-general", [{"role": "user", "content": "hello"}], channel_type="web")
            before = {"schemaVersion": 1, "sessionProjects": {"session-general": "project-a"}}
            ui_state.write_text(json.dumps(before), encoding="utf-8")
            active.write_text(json.dumps({"activeRequests": [{"request_id": "request-raw", "status": "running"}]}), encoding="utf-8")

            result = self._run_audit(
                "--apply",
                "--ui-state",
                str(ui_state),
                "--conversation-db",
                str(db),
                "--active-requests-json",
                str(active),
                "--output",
                str(output),
                "--salt",
                "test-salt",
                check=False,
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            output_text = output.read_text(encoding="utf-8")
            persisted_after = json.loads(ui_state.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["code"], "ACTIVE_REQUESTS_PRESENT")
        self.assertNotIn("request-raw", output_text)
        self.assertEqual(persisted_after, before)


if __name__ == "__main__":
    unittest.main()
