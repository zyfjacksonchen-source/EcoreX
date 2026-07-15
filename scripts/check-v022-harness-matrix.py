#!/usr/bin/env python3
"""Validate the v0.2.2 harness evidence matrix.

This checker intentionally verifies structure and referenced evidence only. It
does not run every smoke command; browser smokes stay explicit commands in the
matrix so reviewers can choose the right gate for their slice.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "docs" / "v0.2.2" / "harness-matrix.json"
REQUIRED_SURFACES = {
    "replay",
    "refresh",
    "disconnect",
    "restart",
    "permissions",
    "artifacts",
    "channels",
    "feishu",
    "image-jobs",
    "image-fallback",
    "scheduler",
    "project-sessions",
    "markdown",
    "ui-polish",
    "status-motion",
    "run-center",
}
PASS_STATUSES = {"pass", "contract-pass", "local-pass"}
ROW_STATUSES = PASS_STATUSES | {"pending", "blocked"}


class MatrixError(RuntimeError):
    """Raised when the matrix violates the contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MatrixError(message)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MatrixError(f"{path} is not valid JSON: {exc}") from exc
    _require(isinstance(payload, dict), "matrix root must be a JSON object")
    return payload


def _relative_path(value: str) -> Path:
    candidate = Path(value)
    _require(not candidate.is_absolute(), f"path must be repository-relative: {value}")
    return ROOT / candidate


def _validate_paths(row_id: str, key: str, values: Any) -> None:
    if values is None:
        return
    _require(isinstance(values, list), f"{row_id}.{key} must be a list")
    for value in values:
        _require(isinstance(value, str) and value, f"{row_id}.{key} has an empty path")
        _require(_relative_path(value).exists(), f"{row_id}.{key} missing path: {value}")


def validate_matrix(path: Path = DEFAULT_MATRIX) -> dict[str, Any]:
    _require(path.exists(), f"matrix file does not exist: {path}")
    matrix = _load_json(path)

    _require(matrix.get("schemaVersion") == 1, "schemaVersion must be 1")
    _require(matrix.get("slice") == "R22-07", "slice must be R22-07")
    _require(isinstance(matrix.get("status"), str) and matrix["status"], "status is required")
    _require(matrix.get("commandShell") == "PowerShell", "commandShell must be PowerShell")

    required_surfaces = set(matrix.get("requiredSurfaces") or [])
    _require(required_surfaces == REQUIRED_SURFACES, "requiredSurfaces must match the R22-07 contract")

    rows = matrix.get("rows")
    _require(isinstance(rows, list) and rows, "rows must be a non-empty list")

    ids: set[str] = set()
    covered: dict[str, list[str]] = {surface: [] for surface in REQUIRED_SURFACES}
    commands = 0

    for row in rows:
        _require(isinstance(row, dict), "each row must be an object")
        row_id = row.get("id")
        _require(isinstance(row_id, str) and row_id, "row.id is required")
        _require(row_id not in ids, f"duplicate row id: {row_id}")
        ids.add(row_id)

        status = row.get("status")
        _require(status in ROW_STATUSES, f"{row_id}.status is invalid: {status}")

        surfaces = row.get("surfaces")
        _require(isinstance(surfaces, list) and surfaces, f"{row_id}.surfaces must be non-empty")
        for surface in surfaces:
            _require(surface in REQUIRED_SURFACES, f"{row_id} references unknown surface: {surface}")
            if status in PASS_STATUSES:
                covered[surface].append(row_id)

        row_commands = row.get("commands")
        _require(isinstance(row_commands, list) and row_commands, f"{row_id}.commands must be non-empty")
        for command in row_commands:
            _require(isinstance(command, str) and command.strip(), f"{row_id}.commands has an empty command")
            _require(
                not command.lstrip().startswith("PYTEST_DISABLE_PLUGIN_AUTOLOAD="),
                f"{row_id}.commands must use PowerShell-compatible env assignment",
            )
            commands += 1

        assertions = row.get("assertions")
        _require(isinstance(assertions, list) and assertions, f"{row_id}.assertions must be non-empty")
        for assertion in assertions:
            _require(isinstance(assertion, str) and assertion.strip(), f"{row_id}.assertions has empty text")

        for key in ("files", "artifacts", "docs"):
            _validate_paths(row_id, key, row.get(key))

    missing = sorted(surface for surface, row_ids in covered.items() if not row_ids)
    _require(not missing, f"required surfaces missing pass/contract-pass coverage: {', '.join(missing)}")

    blockers = matrix.get("externalBlockers") or []
    _require(isinstance(blockers, list), "externalBlockers must be a list")
    feishu_blockers = [
        blocker for blocker in blockers
        if isinstance(blocker, dict)
        and blocker.get("surface") == "feishu"
        and blocker.get("status") == "BLOCKER-PENDING-CREDENTIALS"
    ]
    _require(feishu_blockers, "Feishu real-smoke credential blocker must be explicit")

    return {
        "matrix": str(path.relative_to(ROOT)),
        "rows": len(rows),
        "commands": commands,
        "required_surfaces": sorted(REQUIRED_SURFACES),
        "coverage": {surface: covered[surface] for surface in sorted(covered)},
        "external_blockers": len(blockers),
        "command_shell": matrix["commandShell"],
        "status": matrix["status"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--json", action="store_true", help="Print a machine-readable summary.")
    args = parser.parse_args(argv)

    try:
        summary = validate_matrix(args.matrix.resolve())
    except MatrixError as exc:
        print(f"R22-07 harness matrix check failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(
            f"R22-07 harness matrix OK: {summary['rows']} rows, "
            f"{summary['commands']} commands, status={summary['status']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
