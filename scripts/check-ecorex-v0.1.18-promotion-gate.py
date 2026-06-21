#!/usr/bin/env python3
"""Aggregate v0.1.18 production-agent runtime gates into one JSON report."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any


REQUIRED_ROWS: dict[str, list[str]] = {
    "run-ledger": ["R18-01-A", "R18-01-B", "R18-01-C"],
    "sse-contract": ["R18-02-A", "R18-02-B", "R18-02-C"],
    "cancellation-concurrency": ["R18-03-A", "R18-03-B", "R18-03-C", "R18-03-D"],
    "model-calls": ["R18-04-A", "R18-04-B", "R18-04-C", "R18-04-D"],
    "context-budget": ["R18-05-A", "R18-05-B"],
    "run-center": ["R18-06-A"],
    "evidence-gates": ["R18-07-A"],
}

EVIDENCE_REQUIREMENTS: dict[str, list[str]] = {
    "run-ledger evidence": [
        "r18-run-ledger",
        "terminal-once",
        "confirmed dead-owner",
        "registry fallback suppression",
    ],
    "sse contract evidence": [
        "r18-sse-contract",
        "run.failed",
        "stream.replay_gap",
        "request-scoped history recovery",
    ],
    "cancellation and concurrency evidence": [
        "r18-cancel-concurrency",
        "backpressure",
        "subagent",
        "busy fallback",
    ],
    "model-call evidence": [
        "r18-model-gateway",
        "model-call telemetry",
        "retry policy",
        "responses api",
    ],
    "context and tool budget evidence": [
        "r18-context-budget",
        "tool_schema_budget",
        "context_budget",
        "current-turn preservation",
    ],
    "run center evidence": [
        "r18-run-center",
        "run center",
        "first-class navigation",
        "retry/recover policy",
        "/api/active-requests",
        "diagnostics",
    ],
}

REVIEW_MARKERS: dict[str, list[str]] = {
    "run ledger multi-agent review": ["multi-agent cross-review", "sidecar interruption", "consensus: submit"],
    "sse recovery multi-agent review": ["multi-agent cross-review", "sse replay-gap", "consensus: submit"],
    "cancellation/concurrency multi-agent review": ["multi-agent cross-review", "typed busy/retry", "consensus: submit"],
    "model gateway multi-agent review": ["multi-agent cross-review", "model telemetry", "consensus: submit"],
    "context budget multi-agent review": ["multi-agent cross-review", "context overflow recovery", "consensus: submit"],
    "run center multi-agent review": ["multi-agent cross-review", "run center first-class navigation", "retry/recover policy", "consensus: submit"],
    "promotion gate multi-agent review": ["multi-agent cross-review", "promotion-gate hardening", "consensus: submit"],
}

TOKEN_RE = re.compile(rb"(?:(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})")
IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "dist-electron",
    "node_modules",
    "out",
    "release",
    "release-artifacts",
    "vendor",
}


def read_text(path: pathlib.Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{path} has a UTF-8 BOM")
    return raw.decode("utf-8")


def add_check(checks: list[dict[str, Any]], name: str, status: str, evidence: str, severity: str = "info") -> None:
    checks.append({
        "name": name,
        "status": status,
        "severity": severity,
        "evidence": evidence,
    })


def display_path(root: pathlib.Path, path: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def parse_acceptance_rows(markdown: str) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("| R18-"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 5:
            continue
        rows[cells[0]] = {
            "area": cells[1],
            "standard": cells[2],
            "status": cells[3].upper(),
            "evidence": cells[4],
        }
    return rows


def check_required_acceptance_rows(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    path = root / "docs" / f"v{version}" / "acceptance-checklist.md"
    shown_path = display_path(root, path)
    try:
        rows = parse_acceptance_rows(read_text(path))
    except Exception as exc:
        add_check(checks, "Acceptance checklist", "fail", f"{shown_path}: {exc}", "blocker")
        return

    for group, row_ids in REQUIRED_ROWS.items():
        missing = [row_id for row_id in row_ids if row_id not in rows]
        incomplete = [
            f"{row_id}={rows[row_id]['status']}"
            for row_id in row_ids
            if row_id in rows and rows[row_id]["status"] != "PASS"
        ]
        if missing or incomplete:
            add_check(
                checks,
                f"Acceptance rows: {group}",
                "fail",
                f"{shown_path}: missing={missing} incomplete={incomplete}",
                "blocker",
            )
        else:
            add_check(checks, f"Acceptance rows: {group}", "pass", f"{len(row_ids)} rows PASS")


def text_has_all(text: str, markers: list[str]) -> bool:
    normalized = text.lower()
    return all(marker.lower() in normalized for marker in markers)


def any_line_has_all(text: str, markers: list[str]) -> bool:
    lowered_markers = [marker.lower() for marker in markers]
    for line in text.splitlines():
        normalized = line.lower()
        if all(marker in normalized for marker in lowered_markers):
            return True
    return False


def check_evidence_ledger(root: pathlib.Path, version: str, checks: list[dict[str, Any]]) -> None:
    path = root / "docs" / f"v{version}" / "evidence-ledger.md"
    shown_path = display_path(root, path)
    try:
        evidence = read_text(path)
    except Exception as exc:
        add_check(checks, "Evidence ledger", "fail", f"{shown_path}: {exc}", "blocker")
        return

    for name, markers in EVIDENCE_REQUIREMENTS.items():
        if text_has_all(evidence, markers):
            add_check(checks, name, "pass", f"{shown_path}: markers={markers}")
        else:
            missing = [marker for marker in markers if marker.lower() not in evidence.lower()]
            add_check(checks, name, "fail", f"{shown_path}: missing markers={missing}", "blocker")

    for name, markers in REVIEW_MARKERS.items():
        if any_line_has_all(evidence, markers):
            add_check(checks, name, "pass", f"{shown_path}: review consensus row present")
        else:
            add_check(checks, name, "fail", f"{shown_path}: no single review row contains markers={markers}", "blocker")


def iter_scannable_files(root: pathlib.Path):
    try:
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout
        for raw in listing.split(b"\0"):
            if not raw:
                continue
            rel = pathlib.Path(raw.decode("utf-8", errors="ignore"))
            if set(rel.parts) & IGNORED_DIRS:
                continue
            path = root / rel
            if path.is_file():
                yield path
        return
    except Exception:
        pass

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & IGNORED_DIRS:
            continue
        yield path


def check_token_scan(root: pathlib.Path, checks: list[dict[str, Any]]) -> None:
    hits: list[str] = []
    for path in iter_scannable_files(root):
        try:
            data = path.read_bytes()
        except Exception:
            continue
        if TOKEN_RE.search(data):
            hits.append(str(path.relative_to(root)))
            if len(hits) >= 10:
                break
    if hits:
        add_check(checks, "GitHub token pattern scan", "fail", f"matches={hits}", "blocker")
    else:
        add_check(checks, "GitHub token pattern scan", "pass", "No GitHub token pattern found outside ignored artifacts")


def build_report(root: pathlib.Path, version: str, *, scan_tokens: bool = True) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    check_required_acceptance_rows(root, version, checks)
    check_evidence_ledger(root, version, checks)
    if scan_tokens:
        check_token_scan(root, checks)
    else:
        add_check(
            checks,
            "GitHub token pattern scan",
            "fail",
            "Token scan was skipped; promotion gate cannot report GO without credential-leakage evidence",
            "blocker",
        )

    blockers = [item for item in checks if item["status"] == "fail" and item.get("severity") == "blocker"]
    warnings = [item for item in checks if item.get("severity") == "warn" or item["status"] in {"skipped", "missing"}]
    return {
        "product": "EcoreX",
        "version": version,
        "gate": "v0.1.18-production-agent",
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": "go" if not blockers else "no-go",
        "summary": {
            "total": len(checks),
            "blockers": len(blockers),
            "warnings": len(warnings),
        },
        "checks": checks,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--version", default="0.1.18")
    parser.add_argument("--output", default="")
    parser.add_argument("--allow-no-go", action="store_true", help="Write the report and return 0 even when blockers remain")
    parser.add_argument("--skip-token-scan", action="store_true", help="Skip the local GitHub token pattern scan")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    report = build_report(root, args.version, scan_tokens=not args.skip_token_scan)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = pathlib.Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    if report["status"] == "no-go" and not args.allow_no_go:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
