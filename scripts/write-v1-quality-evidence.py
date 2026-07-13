#!/usr/bin/env python3
"""Validate machine-readable test reports and seal one typed quality receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release.evidence_io import (  # noqa: E402
    read_stable_regular_file,
    strict_json_loads,
    write_new_json_file,
)


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GATES = ("lint", "typecheck", "unit", "contract", "integration", "migration-dry-run")
_MAX_REPORT_BYTES = 64 * 1024 * 1024
_FULL_SUITE_MINIMUM = 1000
_FULL_SENTINELS = (
    "tests.v1.test_agent_turn_worker",
    "tests.v1.test_runtime_event_store",
    "tests.v1.test_update_coordinator",
    "tests.v1.test_product_legacy_migration",
    "tests.v1.test_candidate_release_pipeline",
)
_MIGRATION_CORPUS = (
    "tests.v1.test_migration_copy_on_write",
    "tests.v1.test_product_legacy_migration",
    "tests.v1.test_migration_quarantine",
    "tests.v1.test_v030_release_schema_archive",
    "tests.v1.test_candidate_storage_migrations",
    "tests.v1.test_update_activation_health",
)
_BROWSER_E2E_COUNT = 36
_BROWSER_SENTINELS = (
    "1440x900 light GA report passes with zero axe violations",
    "390x844 dark GA report passes with zero axe violations",
    "320x568 dark GA report passes with zero axe violations",
    "Composer is centered only while choosing a new conversation and otherwise stays at the workspace bottom",
    "reasoning stays visible until replacement and terminal facts clear the first-turn indicator",
    "image artifact opens fitted, keeps zoom controls, and restores keyboard focus",
    "administrator gates are a signed read-only projection and publication stays server-authoritative",
    "administrator gate table remains non-mutating and page-safe at 390px",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--byte-contract", required=True, type=Path)
    parser.add_argument("--supply-chain", required=True, type=Path)
    parser.add_argument("--baseline-evidence", required=True, type=Path)
    parser.add_argument("--full-pytest-junit", required=True, type=Path)
    parser.add_argument("--migration-pytest-junit", required=True, type=Path)
    parser.add_argument("--playwright-json", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _bytes(path: Path, *, label: str) -> bytes:
    return read_stable_regular_file(path, maximum_bytes=_MAX_REPORT_BYTES, code=label)


def _digest(path: Path) -> str:
    return hashlib.sha256(_bytes(path, label="quality_dependency_evidence_invalid")).hexdigest()


def _integer(value: str | None, *, label: str) -> int:
    if value is None or re.fullmatch(r"0|[1-9][0-9]*", value) is None:
        raise ValueError(label)
    return int(value)


def _junit(path: Path, *, migration: bool) -> dict[str, Any]:
    payload = _bytes(path, label="pytest_junit_invalid")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        raise ValueError("pytest_junit_invalid") from None
    suites = [root] if root.tag == "testsuite" else list(root.findall("./testsuite"))
    if root.tag not in {"testsuite", "testsuites"} or not suites:
        raise ValueError("pytest_junit_invalid")
    testcases = list(root.iter("testcase"))
    declared = sum(_integer(item.get("tests"), label="pytest_junit_invalid") for item in suites)
    failures = sum(_integer(item.get("failures"), label="pytest_junit_invalid") for item in suites)
    errors = sum(_integer(item.get("errors"), label="pytest_junit_invalid") for item in suites)
    skipped = sum(_integer(item.get("skipped"), label="pytest_junit_invalid") for item in suites)
    if declared != len(testcases) or declared < 1 or failures or errors:
        raise ValueError("pytest_junit_not_clean")
    if any(item.find("failure") is not None or item.find("error") is not None for item in testcases):
        raise ValueError("pytest_junit_not_clean")
    identities = {
        f"{item.get('classname', '')}::{item.get('name', '')}"
        for item in testcases
        if item.find("skipped") is None
    }
    required = _MIGRATION_CORPUS if migration else _FULL_SENTINELS
    missing = [token for token in required if not any(token in identity for identity in identities)]
    if (
        missing
        or (migration and skipped != 0)
        or (not migration and declared - skipped < _FULL_SUITE_MINIMUM)
    ):
        raise ValueError("pytest_required_corpus_missing")
    return {
        "report_sha256": hashlib.sha256(payload).hexdigest(),
        "tests": declared,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "required_corpus": list(required),
    }


def _playwright(path: Path) -> dict[str, Any]:
    payload = _bytes(path, label="playwright_report_invalid")
    value = strict_json_loads(payload, code="playwright_report_invalid")
    if not isinstance(value, dict) or not isinstance(value.get("suites"), list):
        raise ValueError("playwright_report_invalid")
    stats = value.get("stats")
    errors = value.get("errors")
    if not isinstance(stats, dict) or not isinstance(errors, list) or errors:
        raise ValueError("playwright_report_invalid")
    expected = stats.get("expected")
    unexpected = stats.get("unexpected")
    flaky = stats.get("flaky")
    skipped = stats.get("skipped")
    duration = stats.get("duration")
    if (
        isinstance(expected, bool)
        or not isinstance(expected, int)
        or expected != _BROWSER_E2E_COUNT
        or unexpected != 0
        or flaky != 0
        or skipped != 0
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or duration <= 0
    ):
        raise ValueError("playwright_report_not_clean")

    specs: list[dict[str, Any]] = []
    pending: list[Any] = list(value["suites"])
    while pending:
        item = pending.pop()
        if not isinstance(item, dict):
            raise ValueError("playwright_report_invalid")
        children = item.get("suites", [])
        raw_specs = item.get("specs", [])
        if not isinstance(children, list) or not isinstance(raw_specs, list):
            raise ValueError("playwright_report_invalid")
        pending.extend(children)
        specs.extend(raw_specs)
    executed = 0
    titles: set[str] = set()
    for spec in specs:
        if (
            not isinstance(spec, dict)
            or spec.get("ok") is not True
            or not isinstance(spec.get("title"), str)
            or not spec["title"]
        ):
            raise ValueError("playwright_report_not_clean")
        titles.add(spec["title"])
        tests = spec.get("tests")
        if not isinstance(tests, list) or len(tests) != 1:
            raise ValueError("playwright_report_invalid")
        test = tests[0]
        results = test.get("results") if isinstance(test, dict) else None
        if (
            not isinstance(results, list)
            or len(results) != 1
            or not isinstance(results[0], dict)
            or results[0].get("status") != "passed"
        ):
            raise ValueError("playwright_report_not_clean")
        executed += 1
    if executed != _BROWSER_E2E_COUNT:
        raise ValueError("playwright_report_execution_count_invalid")
    missing = [title for title in _BROWSER_SENTINELS if title not in titles]
    if missing:
        raise ValueError("playwright_required_corpus_missing")
    return {
        "report_sha256": hashlib.sha256(payload).hexdigest(),
        "tests": executed,
        "passed": executed,
        "failed": 0,
        "skipped": 0,
        "duration_milliseconds": duration,
        "required_corpus": list(_BROWSER_SENTINELS),
    }


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if (
            _COMMIT.fullmatch(args.commit_sha) is None
            or isinstance(args.workflow_run_id, bool)
            or args.workflow_run_id < 1
        ):
            raise ValueError("quality_evidence_identity_invalid")
        full = _junit(args.full_pytest_junit, migration=False)
        migration = _junit(args.migration_pytest_junit, migration=True)
        browser = _playwright(args.playwright_json)
        value = {
            "schema_version": 3,
            "evidence_type": "ecorex-source-quality-execution",
            "status": "passed",
            "commit_sha": args.commit_sha,
            "workflow_run_id": args.workflow_run_id,
            "gates": {gate: "passed" for gate in _GATES},
            "dependencies": {
                "byte-contract": _digest(args.byte_contract),
                "supply-chain": _digest(args.supply_chain),
                "v030-baseline": _digest(args.baseline_evidence),
            },
            "executions": {
                "full-pytest": full,
                "migration-pytest": migration,
                "browser-e2e": browser,
            },
        }
        output = args.output.resolve()
        if os.path.lexists(output):
            raise ValueError("quality_evidence_exists")
        write_new_json_file(value, output, code="quality_evidence_exists")
        print(json.dumps({"ok": True, "gates": list(_GATES)}, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
