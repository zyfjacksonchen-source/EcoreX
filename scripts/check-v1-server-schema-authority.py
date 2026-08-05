#!/usr/bin/env python3
"""Reject server-side DDL outside explicit deployment schema authorities.

The Control Plane, managed Model Gateway, and image orchestration packages are
independent server boundaries.  Their repositories/apps/stores may read and
write business rows, but physical schema changes belong only to the fixed
operator-invoked migration modules listed below.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOTS = (
    ROOT / "ecorex" / "control_plane",
    ROOT / "ecorex" / "gateway",
    ROOT / "ecorex" / "image_orchestrator",
)
SCHEMA_AUTHORITIES = frozenset(
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
DDL = re.compile(
    r"\b(?:"
    r"CREATE\s+(?:(?:OR\s+REPLACE|UNIQUE)\s+)*"
    r"(?:TABLE|INDEX|TRIGGER|MATERIALIZED\s+VIEW|VIEW|FUNCTION|PROCEDURE|"
    r"SEQUENCE|SCHEMA|TYPE|DOMAIN|EXTENSION|POLICY|RULE|AGGREGATE|OPERATOR|COLLATION)"
    r"|ALTER\s+(?:TABLE|INDEX|TRIGGER|MATERIALIZED\s+VIEW|VIEW|FUNCTION|PROCEDURE|"
    r"SEQUENCE|SCHEMA|TYPE|DOMAIN|EXTENSION|POLICY|RULE|AGGREGATE|OPERATOR|COLLATION)"
    r"|DROP\s+(?:TABLE|INDEX|TRIGGER|MATERIALIZED\s+VIEW|VIEW|FUNCTION|PROCEDURE|"
    r"SEQUENCE|SCHEMA|TYPE|DOMAIN|EXTENSION|POLICY|RULE|AGGREGATE|OPERATOR|COLLATION)"
    r"|PRAGMA\s+journal_mode"
    r")\b",
    re.IGNORECASE,
)


def _string_nodes(source: str, *, filename: str) -> tuple[tuple[int, str], ...]:
    tree = ast.parse(source, filename=filename)
    values: list[tuple[int, str]] = []

    class StringVisitor(ast.NodeVisitor):
        def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
            if isinstance(node.value, str):
                values.append((node.lineno, node.value))

        def visit_JoinedStr(self, node: ast.JoinedStr) -> None:  # noqa: N802
            # The literal portions are sufficient to catch dynamic statements
            # such as f"DROP TRIGGER {name}" without executing the source.
            literal = "".join(
                value.value
                for value in node.values
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            )
            values.append((node.lineno, literal))
            # Do not visit the literal Constant children again.  Expressions
            # inside formatted values can still contain independent strings.
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    self.visit(value.value)

    StringVisitor().visit(tree)
    return tuple(values)


def scan_python_source(display_path: str, source: str) -> list[str]:
    """Return deterministic DDL violations for one non-authority source."""

    found: list[str] = []
    try:
        values = _string_nodes(source, filename=display_path)
    except SyntaxError as error:
        return [f"{display_path}:unreadable:{type(error).__name__}"]
    for line, value in values:
        match = DDL.search(value)
        if match is not None:
            found.append(f"{display_path}:{line}:{match.group(0).upper()}")
    return found


def violations() -> list[str]:
    found: list[str] = []
    for authority in sorted(SCHEMA_AUTHORITIES):
        if not ROOT.joinpath(*authority.split("/")).is_file():
            found.append(f"{authority}:missing-authority")

    for server_root in SERVER_ROOTS:
        if not server_root.is_dir():
            found.append(
                f"{server_root.relative_to(ROOT).as_posix()}:missing-server-root"
            )
            continue
        for path in sorted(server_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(ROOT).as_posix()
            if relative in SCHEMA_AUTHORITIES:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                found.append(f"{relative}:unreadable:{type(error).__name__}")
                continue
            found.extend(scan_python_source(relative, source))
    return found


def main() -> int:
    found = violations()
    result = {
        "authority_count": len(SCHEMA_AUTHORITIES),
        "server_root_count": len(SERVER_ROOTS),
        "status": "failed" if found else "passed",
        "violations": found,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
