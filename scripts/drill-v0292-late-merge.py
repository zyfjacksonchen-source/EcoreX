#!/usr/bin/env python3
"""Exercise a real v0.2.9.2 -> v1 late merge without touching live data.

Only aggregate counts and integrity facts are emitted. Conversation content and
identifiers are deliberately never selected or printed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from ecorex.connectors import InMemoryCredentialVault
from ecorex.migration.migrator import TARGET_DATABASE_NAME
from ecorex.migration.product import (
    ProductLegacyMigrationCoordinator,
    write_product_migration_plan,
)

print("stage=imports", flush=True)


def _count(database: Path, sql: str, parameters: tuple[object, ...] = ()) -> int:
    with sqlite3.connect(database) as connection:
        row = connection.execute(sql, parameters).fetchone()
    return int(row[0]) if row else 0


def _snapshot_state(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    database = source / TARGET_DATABASE_NAME
    if not database.is_file():
        raise SystemExit("v1 runtime database is unavailable")
    for child in source.iterdir():
        if child.name in {
            TARGET_DATABASE_NAME,
            TARGET_DATABASE_NAME + "-wal",
            TARGET_DATABASE_NAME + "-shm",
        }:
            continue
        target = destination / child.name
        if child.is_dir():
            shutil.copytree(child, target, symlinks=False)
        elif child.is_file():
            shutil.copy2(child, target)
    database_family = (
        database,
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    )
    for attempt in range(5):
        before = {
            item.name: (item.stat().st_size, item.stat().st_mtime_ns)
            for item in database_family
            if item.exists()
        }
        for item in database_family:
            target = destination / item.name
            if item.exists():
                shutil.copy2(item, target)
            elif target.exists():
                target.unlink()
        after = {
            item.name: (item.stat().st_size, item.stat().st_mtime_ns)
            for item in database_family
            if item.exists()
        }
        if before == after:
            with sqlite3.connect(destination / TARGET_DATABASE_NAME) as snapshot:
                if snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",):
                    return
        if attempt < 4:
            time.sleep(0.2)
    raise SystemExit("v1 runtime database changed during snapshot")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--v1-state", type=Path, required=True)
    parser.add_argument("--work-root", type=Path)
    args = parser.parse_args()

    legacy_root = Path(os.path.abspath(args.legacy_root))
    v1_state = Path(os.path.abspath(args.v1_state))
    work_parent = Path(os.path.abspath(args.work_root)) if args.work_root else None
    if not legacy_root.is_dir():
        raise SystemExit("legacy root is unavailable")
    if not v1_state.is_dir():
        raise SystemExit("v1 state root is unavailable")
    if work_parent is not None:
        work_parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="ecorex-v0292-merge-", dir=work_parent, ignore_cleanup_errors=True
    ) as raw:
        print("stage=snapshot_v1", flush=True)
        install = Path(raw) / "install"
        state = install / "state"
        candidate = install / "slots" / "candidate-v1"
        candidate.mkdir(parents=True)
        _snapshot_state(v1_state, state)

        baseline_threads = _count(
            state / TARGET_DATABASE_NAME, "SELECT COUNT(*) FROM threads"
        )
        baseline_messages = _count(
            state / TARGET_DATABASE_NAME,
            "SELECT COUNT(*) FROM items WHERE kind='message'",
        )
        baseline_artifacts = _count(
            state / TARGET_DATABASE_NAME, "SELECT COUNT(*) FROM artifact_entities"
        )

        print("stage=plan_legacy", flush=True)
        write_product_migration_plan(install, legacy_root, source_version="0.2.9.2")
        migration = ProductLegacyMigrationCoordinator(
            install,
            state / TARGET_DATABASE_NAME,
            vault=InMemoryCredentialVault(),
        )
        print("stage=dry_run", flush=True)
        migration.dry_run(candidate, "real-v0292-late-merge-dry-run")
        print("stage=commit", flush=True)
        migration.commit(candidate, "real-v0292-late-merge-commit")
        print("stage=verify", flush=True)

        database = state / TARGET_DATABASE_NAME
        report = json.loads(
            (state / "migration-report.json").read_text(encoding="utf-8")
        )
        with sqlite3.connect(database) as connection:
            integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
        result = {
            "status": "passed",
            "integrity": integrity_row == ("ok",),
            "baseline": {
                "threads": baseline_threads,
                "messages": baseline_messages,
                "artifacts": baseline_artifacts,
            },
            "merged": {
                "threads": _count(database, "SELECT COUNT(*) FROM threads"),
                "messages": _count(
                    database, "SELECT COUNT(*) FROM items WHERE kind='message'"
                ),
                "artifacts": _count(database, "SELECT COUNT(*) FROM artifact_entities"),
                "legacy_sessions": _count(
                    database,
                    "SELECT COUNT(*) FROM legacy_id_map WHERE entity_kind='session'",
                ),
                "legacy_messages": _count(
                    database,
                    "SELECT COUNT(*) FROM legacy_id_map WHERE entity_kind='message'",
                ),
            },
            "preservation": {
                "baseline_merge": int(report["counts"].get("baseline_merge", 0)),
                "baseline_threads_preserved": int(
                    report["counts"].get("baseline_threads_preserved", 0)
                ),
                "baseline_items_preserved": int(
                    report["counts"].get("baseline_items_preserved", 0)
                ),
                "deleted_sessions_excluded": int(
                    report["counts"].get("deleted_session_cache_excluded", 0)
                ),
            },
        }
        if result["merged"]["threads"] < baseline_threads:
            raise SystemExit("late merge removed existing v1 threads")
        if result["merged"]["messages"] < baseline_messages:
            raise SystemExit("late merge removed existing v1 messages")
        if result["merged"]["artifacts"] < baseline_artifacts:
            raise SystemExit("late merge removed existing v1 artifacts")
        if result["preservation"]["baseline_merge"] != 1:
            raise SystemExit("late merge preservation mode was not active")
        if result["preservation"]["deleted_sessions_excluded"] <= 0:
            raise SystemExit("deleted legacy sessions were not explicitly excluded")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
