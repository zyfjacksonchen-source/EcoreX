#!/usr/bin/env python3
"""Fail closed unless a fixed historical release commit is fully available."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED_PATHS = (
    "agent/memory/conversation_store.py",
    "agent/protocol/run_ledger.py",
    "agent/protocol/run_event_ledger.py",
)


def _git(*arguments: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ("git", *arguments),
        cwd=ROOT,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("release_baseline_object_incomplete")
    return result.stdout


def verify(commit: str) -> dict[str, object]:
    if _COMMIT.fullmatch(commit) is None:
        raise ValueError("release_baseline_commit_invalid")
    resolved = _git("rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    if resolved != commit:
        raise ValueError("release_baseline_commit_mismatch")
    objects = []
    for line in _git("rev-list", "--objects", commit).splitlines():
        object_id = line.split(b" ", 1)[0]
        if not re.fullmatch(rb"[0-9a-f]{40,64}", object_id):
            raise ValueError("release_baseline_object_inventory_invalid")
        objects.append(object_id)
    if not objects:
        raise ValueError("release_baseline_object_inventory_invalid")
    checked = _git(
        "cat-file",
        "--batch-check=%(objectname) %(objecttype)",
        input_bytes=b"\n".join(objects) + b"\n",
    ).splitlines()
    if len(checked) != len(objects) or any(line.endswith(b" missing") for line in checked):
        raise ValueError("release_baseline_object_incomplete")
    path_digests: dict[str, str] = {}
    for path in _REQUIRED_PATHS:
        payload = _git("show", f"{commit}:{path}")
        if not payload:
            raise ValueError("release_baseline_schema_source_missing")
        path_digests[path] = hashlib.sha256(payload).hexdigest()
    tree = _git("rev-parse", f"{commit}^{{tree}}").decode().strip()
    return {
        "schema_version": 1,
        "evidence_type": "ecorex-v030-complete-git-object-set",
        "status": "passed",
        "commit_sha": commit,
        "tree_sha": tree,
        "reachable_object_count": len(objects),
        "required_path_sha256": path_digests,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        value = verify(args.commit)
        output = args.output.resolve()
        if os.path.lexists(output):
            raise ValueError("release_baseline_evidence_exists")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(json.dumps({"ok": True, "object_count": value["reachable_object_count"]}))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
