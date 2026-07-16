from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

from ecorex.migration.legacy_identity_export import (
    LegacyIdentityExportError,
    export_v0292_legacy_identities,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only v0.2.9.2 Admin identity NDJSON exporter"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--as-of", help="UTC/offset ISO-8601 cutoff")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        cutoff = (
            datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
            if args.as_of
            else datetime.now(UTC)
        )
        if cutoff.tzinfo is None:
            raise ValueError
        records, report = export_v0292_legacy_identities(args.database, as_of=cutoff)
        if args.dry_run:
            print(
                json.dumps(
                    report.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            for record in records:
                print(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            print(
                json.dumps(
                    {
                        "status": "complete",
                        "eligible_sessions": report.eligible_sessions,
                        "records_sha256": report.records_sha256,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                file=sys.stderr,
            )
        return 0
    except (LegacyIdentityExportError, OSError, ValueError) as error:
        print(
            json.dumps(
                {"status": "failed", "error": type(error).__name__},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
