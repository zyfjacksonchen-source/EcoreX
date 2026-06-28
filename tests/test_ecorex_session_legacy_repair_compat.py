import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = ROOT / "scripts" / "audit-ecorex-session-state.py"


class EcoreXSessionLegacyRepairCompatTests(unittest.TestCase):
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

    def _legacy_db(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(
                """
                CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY,
                    channel_type TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    last_active INTEGER NOT NULL,
                    msg_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO sessions (session_id, channel_type, created_at, last_active, msg_count) VALUES (?, ?, ?, ?, ?)",
                ("legacy-session", "", 10, 20, 1),
            )
            conn.execute(
                "INSERT INTO messages (session_id, seq, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                ("legacy-session", 0, "user", json.dumps("legacy message"), 10),
            )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_empty_channel_is_quarantined_not_deleted(self):
        with tempfile.TemporaryDirectory() as raw_workspace:
            workspace = Path(raw_workspace)
            db = workspace / "legacy.sqlite3"
            ui_state = workspace / ".ecorex" / "ui-state.json"
            active = workspace / "active.json"
            output = workspace / "legacy-dry-run.json"
            ui_state.parent.mkdir(parents=True)
            active.write_text(json.dumps({"activeRequests": []}), encoding="utf-8")
            self._legacy_db(db)
            ui_state.write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "projects": [{"id": "known-project"}],
                    "sessionProjects": {"legacy-session": "unknown-project"},
                    "sessionProjectBindings": {"legacy-session": {"projectId": "unknown-project"}},
                    "pinnedSessions": {"legacy-session": True},
                }),
                encoding="utf-8",
            )

            self._run_audit(
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

            self._run_audit(
                "--apply",
                "--ui-state",
                str(ui_state),
                "--conversation-db",
                str(db),
                "--active-requests-json",
                str(active),
                "--backup-dir",
                str(workspace / "backup"),
                "--salt",
                "test-salt",
            )
            repaired = json.loads(ui_state.read_text(encoding="utf-8"))
            conn = sqlite3.connect(str(db))
            try:
                session_count = conn.execute("SELECT COUNT(*) FROM sessions WHERE session_id = ?", ("legacy-session",)).fetchone()[0]
                message_count = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", ("legacy-session",)).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(report["summary"]["legacyEmptyChannelCount"], 1)
        self.assertIn("dangling_project_binding", report["summary"]["issueTypes"])
        self.assertIn("stale_local_project_for_backend_general", report["summary"]["issueTypes"])
        self.assertEqual(session_count, 1)
        self.assertEqual(message_count, 1)
        self.assertNotIn("legacy-session", repaired["sessionProjects"])
        self.assertNotIn("legacy-session", repaired["sessionProjectBindings"])
        self.assertTrue(repaired["pinnedSessions"]["legacy-session"])


if __name__ == "__main__":
    unittest.main()
