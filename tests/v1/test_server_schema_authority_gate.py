from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "check-v1-server-schema-authority.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("server_schema_authority_gate", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_server_schema_authority_gate_passes() -> None:
    completed = subprocess.run(
        [sys.executable, str(GATE)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == {
        "authority_count": 12,
        "server_root_count": 3,
        "status": "passed",
        "violations": [],
    }


def test_gate_detects_literal_dynamic_ddl_and_journal_changes() -> None:
    gate = _load_gate()
    source = '''
def mutate(connection, name):
    connection.execute("CREATE TABLE escaped(id INTEGER)")
    connection.execute(f"DROP TRIGGER {name}")
    connection.execute("CREATE SEQUENCE hidden_ids")
    connection.execute("PRAGMA journal_mode=WAL")
'''
    violations = gate.scan_python_source("ecorex/gateway/repository.py", source)

    assert len(violations) == 4
    assert any("CREATE TABLE" in violation for violation in violations)
    assert any("DROP TRIGGER" in violation for violation in violations)
    assert any("CREATE SEQUENCE" in violation for violation in violations)
    assert any("PRAGMA JOURNAL_MODE" in violation for violation in violations)


def test_gate_has_an_exact_small_deployment_authority_allowlist() -> None:
    gate = _load_gate()
    assert gate.SCHEMA_AUTHORITIES == frozenset(
        {
            "ecorex/control_plane/audit_schema.py",
            "ecorex/control_plane/bootstrap_index_schema.py",
            "ecorex/control_plane/device_identity_schema.py",
            "ecorex/control_plane/direct_admission_schema.py",
            "ecorex/control_plane/management_schema.py",
            "ecorex/control_plane/schema.py",
            "ecorex/control_plane/share_media_migration.py",
            "ecorex/control_plane/share_schema.py",
            "ecorex/control_plane/skill_hub.py",
            "ecorex/gateway/schema.py",
            "ecorex/image_orchestrator/postgres_schema.py",
            "ecorex/image_orchestrator/sqlite_schema.py",
        }
    )
    assert tuple(path.name for path in gate.SERVER_ROOTS) == (
        "control_plane",
        "gateway",
        "image_orchestrator",
    )
