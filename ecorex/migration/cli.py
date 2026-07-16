"""Standalone CLI for released-data copy-on-write migration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .crypto import load_quarantine_key
from .inventory import DEFAULT_SOURCE_VERSION, SUPPORTED_SOURCE_VERSIONS, inventory_source
from .migrator import migrate_legacy_to_v1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ecorex.migration")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser(
        "inventory", help="read-only inventory and SHA-256 manifest"
    )
    inventory.add_argument("source", type=Path)
    inventory.add_argument(
        "--source-version",
        choices=sorted(SUPPORTED_SOURCE_VERSIONS),
        default=DEFAULT_SOURCE_VERSION,
    )

    migrate = subparsers.add_parser(
        "migrate", help="stage, verify, and atomically publish a new v1 target"
    )
    migrate.add_argument("source", type=Path)
    migrate.add_argument("target", type=Path)
    migrate.add_argument(
        "--source-version",
        choices=sorted(SUPPORTED_SOURCE_VERSIONS),
        default=DEFAULT_SOURCE_VERSION,
    )
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument(
        "--quarantine-key-file",
        type=Path,
        help="raw, hex, or base64 AES key supplied from a local credential vault",
    )
    migrate.add_argument("--conversation-database", type=Path)
    migrate.add_argument("--memory-database", type=Path)
    migrate.add_argument("--config-file", type=Path)
    migrate.add_argument("--mcp-file", type=Path)
    migrate.add_argument("--ui-state-file", type=Path)
    migrate.add_argument("--skills-config-file", type=Path)
    migrate.add_argument(
        "--permission-file",
        type=Path,
        help="legacy permissions.json outside the workspace (staged, never activated)",
    )
    migrate.add_argument(
        "--release-evidence-file",
        type=Path,
        help="release.json/runtime-manifest.json supplied by the old install",
    )
    migrate.add_argument("--sample-size", type=int, default=3)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "inventory":
        payload = inventory_source(
            args.source, source_version=args.source_version
        ).to_dict()
    else:
        key = (
            load_quarantine_key(args.quarantine_key_file)
            if args.quarantine_key_file
            else None
        )
        report = migrate_legacy_to_v1(
            args.source,
            args.target,
            source_version=args.source_version,
            dry_run=bool(args.dry_run),
            quarantine_key=key,
            conversation_database=args.conversation_database,
            memory_database=args.memory_database,
            config_file=args.config_file,
            mcp_file=args.mcp_file,
            ui_state_file=args.ui_state_file,
            skills_config_file=args.skills_config_file,
            permission_file=args.permission_file,
            release_evidence_file=args.release_evidence_file,
            sample_size=max(0, args.sample_size),
        )
        payload = report.to_dict()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
