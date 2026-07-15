#!/usr/bin/env python3
"""Audit or idempotently bootstrap EcoreX v1 GitHub release governance."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release.repository_admin import (  # noqa: E402
    GitHubRepositoryAdminClient,
)
from ecorex.release.repository_readiness import (  # noqa: E402
    EnvironmentGitHubAdminCredential,
    RepositoryReadinessError,
    default_release_repository_contract,
    evaluate_release_repository,
)


_REPOSITORY = re.compile(
    r"^(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]{0,99})/"
    r"(?P<repository>[A-Za-z0-9][A-Za-z0-9._-]{0,99})$"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit release repository state or apply only branch/environment "
            "governance. Secret values and runner registration remain external."
        )
    )
    parser.add_argument("mode", choices=("audit", "bootstrap"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--token-environment", default="ECOREX_GITHUB_ADMIN_TOKEN"
    )
    parser.add_argument("--expected-head")
    parser.add_argument("--reviewer-login")
    parser.add_argument("--confirm-repository")
    return parser


def _write_json(path: Path, value: Any) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _identity(raw: str) -> tuple[str, str]:
    match = _REPOSITORY.fullmatch(raw)
    if match is None:
        raise ValueError("repository must be the exact owner/name identity")
    return match.group("owner"), match.group("repository")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    owner, repository = _identity(args.repository)
    contract = default_release_repository_contract()
    credentials = EnvironmentGitHubAdminCredential(variable=args.token_environment)
    report: dict[str, Any] = {
        "action": "none",
        "mode": args.mode,
        "repository": args.repository,
        "schema_version": 1,
    }
    try:
        with GitHubRepositoryAdminClient(
            owner=owner,
            repository=repository,
            credentials=credentials,
        ) as client:
            before = client.snapshot(contract)
            report["before"] = {
                "readiness": evaluate_release_repository(before, contract),
                "snapshot": before,
            }
            if args.mode == "bootstrap":
                if args.confirm_repository != args.repository:
                    raise ValueError("bootstrap repository confirmation mismatch")
                if not isinstance(args.expected_head, str) or _SHA.fullmatch(
                    args.expected_head
                ) is None:
                    raise ValueError("bootstrap requires an exact 40-character head")
                if not isinstance(args.reviewer_login, str) or not args.reviewer_login:
                    raise ValueError("bootstrap requires a reviewer login")
                reviewer_id = client.resolve_user_id(args.reviewer_login)
                client.apply_governance(
                    expected_head=args.expected_head,
                    reviewer_id=reviewer_id,
                    contract=contract,
                )
                report["action"] = "governance_applied"
                after = client.snapshot(contract)
                report["after"] = {
                    "readiness": evaluate_release_repository(after, contract),
                    "snapshot": after,
                }
    except (RepositoryReadinessError, ValueError, TypeError) as error:
        code = error.code if isinstance(error, RepositoryReadinessError) else str(error)
        report["error"] = code
        report["compensated"] = bool(
            isinstance(error, RepositoryReadinessError) and error.compensated
        )
        report["status"] = "failed"
        _write_json(args.output, report)
        return 1

    final = report.get("after", report["before"])
    readiness = final["readiness"]
    report["status"] = "ready" if readiness["ready"] else "blocked"
    _write_json(args.output, report)
    return 0 if readiness["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
