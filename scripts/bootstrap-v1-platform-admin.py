#!/usr/bin/env python3
"""Idempotently create the deployment-owned platform administrator account."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.control_plane.management import (  # noqa: E402
    AdminManagementNotFound,
    AdminManagementRepository,
)
from ecorex.control_plane.management_models import CreateAdminUserRequest  # noqa: E402
from ecorex.control_plane.models import ControlPrincipal  # noqa: E402


_ACTOR = ControlPrincipal(
    subject="system.platform-admin-bootstrap",
    client_id="ecorex-production-bootstrap",
    account_id="system.deployment",
    organization_id=None,
    roles=frozenset({"platform_admin"}),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", default="ecorex-platform-admin")
    parser.add_argument("--display-name", default="EcoreX 管理员")
    args = parser.parse_args()
    try:
        database = Path(os.environ["ECOREX_CP_DATABASE_PATH"])
        key = base64.b64decode(
            os.environ["ECOREX_CP_MODEL_CONFIG_ENCRYPTION_KEY_B64"], validate=True
        )
        if not database.is_absolute() or len(key) != 32:
            raise ValueError
        repository = AdminManagementRepository(database, encryption_key=key)
        try:
            user = repository.get_user(args.account_id)
            if user.status != "active" or user.display_name != args.display_name:
                raise ValueError
            created = False
        except AdminManagementNotFound:
            user = repository.create_user(
                CreateAdminUserRequest(
                    account_id=args.account_id,
                    display_name=args.display_name,
                    email=None,
                    organization_id="ecorex-production",
                    token_limit=0,
                    image_limit=0,
                    client_request_id=(
                        "bootstrap-platform-admin-"
                        + hashlib.sha256(args.account_id.encode()).hexdigest()
                    ),
                ),
                actor=_ACTOR,
            )
            created = True
        print(
            json.dumps(
                {"ok": True, "account_id": user.account_id, "created": created},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except Exception:
        print('{"ok":false,"code":"platform_admin_bootstrap_failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
