from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "check-v1-runtime-schema-authority.py"


def test_runtime_schema_authority_gate_passes() -> None:
    completed = subprocess.run(
        [sys.executable, str(GATE)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result == {
        "fragment_count": 20,
        "schema_version": 2,
        "status": "passed",
        "violations": [],
    }


def test_gate_scans_every_local_runtime_python_string() -> None:
    source = GATE.read_text(encoding="utf-8")
    assert 'ECOREX.rglob("*.py")' in source
    assert "ast.JoinedStr" in source
    assert "runtime/schema_fragments/" in source
    assert "control_plane/" in source
    assert '"ecorex/image_orchestrator/sqlite_schema.py"' in source
    assert '"ecorex/image_orchestrator/sqlite_store.py"' not in source
