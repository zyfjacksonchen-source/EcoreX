#!/usr/bin/env python3
"""Reject local Runtime DDL outside the compiled schema/migration authorities."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
ECOREX = ROOT / "ecorex"
DDL = re.compile(
    r"\b(?:CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER)"
    r"|ALTER\s+TABLE|DROP\s+(?:TABLE|INDEX|TRIGGER)"
    r"|PRAGMA\s+journal_mode)\b",
    re.IGNORECASE,
)
EXPECTED_FRAGMENTS = frozenset(
    {
        "artifacts",
        "audit_outbox",
        "capability-snapshots",
        "connector-agent-runtime",
        "connectors-v6",
        "device_authorization",
        "extensions",
        "integration",
        "legacy-import-v3",
        "local-memory",
        "managed_session",
        "output",
        "runtime-permissions",
        "runtime-snapshots",
        "sharing",
        "system_observability",
        "tool-executions",
        "turn-execution-inputs",
        "trace_outbox",
        "update",
    }
)


def _allowed(path: Path) -> bool:
    relative = path.relative_to(ROOT).as_posix()
    return (
        relative in {
            "ecorex/runtime/database.py",
            "ecorex/runtime/storage_migrations.py",
            "ecorex/migration/schema.py",
            "ecorex/gateway/schema.py",
            "ecorex/image_orchestrator/postgres_schema.py",
            "ecorex/image_orchestrator/sqlite_schema.py",
        }
        or relative.startswith("ecorex/runtime/schema_fragments/")
        or relative.startswith("ecorex/control_plane/")
    )


def _string_nodes(path: Path) -> tuple[tuple[int, str], ...]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    values: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            literal = "".join(
                value.value
                for value in node.values
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            )
            values.append((node.lineno, literal))
    return tuple(values)


def violations() -> list[str]:
    found: list[str] = []
    for path in sorted(ECOREX.rglob("*.py")):
        if _allowed(path) or "__pycache__" in path.parts:
            continue
        try:
            values = _string_nodes(path)
        except (OSError, UnicodeError, SyntaxError) as error:
            found.append(
                f"{path.relative_to(ROOT).as_posix()}:unreadable:{type(error).__name__}"
            )
            continue
        for line, value in values:
            match = DDL.search(value)
            if match is not None:
                found.append(
                    f"{path.relative_to(ROOT).as_posix()}:{line}:{match.group(0).upper()}"
                )

    root_text = str(ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from ecorex.runtime.schema_catalog import product_schema_inventory

    inventory = product_schema_inventory()
    observed = {fragment_id for fragment_id, _objects in inventory}
    if observed != EXPECTED_FRAGMENTS:
        missing = sorted(EXPECTED_FRAGMENTS - observed)
        extra = sorted(observed - EXPECTED_FRAGMENTS)
        found.append(f"schema fragment registry drift: missing={missing}, extra={extra}")
    return found


def main() -> int:
    found = violations()
    result = {
        "fragment_count": len(EXPECTED_FRAGMENTS),
        "schema_version": 1,
        "status": "failed" if found else "passed",
        "violations": found,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
