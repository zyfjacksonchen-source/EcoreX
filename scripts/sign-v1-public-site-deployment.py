#!/usr/bin/env python3
"""Build and externally sign one exact public-site deployment authorization."""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.deployment.public_site import (  # noqa: E402
    DeploymentPaths,
    PublicSiteDeployError,
    _validate_unsigned_staged_site,
    build_admin_deployment_identity,
    sign_public_site_authorization,
    verify_public_site_authorization,
)
from ecorex.release import DigestPinnedExternalSigner  # noqa: E402
from ecorex.release.evidence_io import write_new_json_file  # noqa: E402


SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sign-v1-public-site-deployment",
        description=(
            "rescan one direct-checker-passed staging site and sign its exact "
            "deployment identity with the digest-pinned release signer"
        ),
    )
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--staging-release-dir", required=True, type=Path)
    parser.add_argument("--cloud-artifact-root", required=True, type=Path)
    return parser


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("site_authorization_signer_configuration_missing")
    return value


def _signer() -> DigestPinnedExternalSigner:
    try:
        public = base64.b64decode(
            _required("ECOREX_RELEASE_SIGNER_PUBLIC_KEY"), validate=True
        )
        executable_sha256 = _required("ECOREX_RELEASE_SIGNER_EXECUTABLE_SHA256")
        if len(public) != 32 or SHA256.fullmatch(executable_sha256) is None:
            raise ValueError
        return DigestPinnedExternalSigner(
            key_id=_required("ECOREX_RELEASE_SIGNER_KEY_ID"),
            public_key=public,
            executable_path=_required("ECOREX_RELEASE_SIGNER_EXECUTABLE"),
            executable_sha256=executable_sha256,
            adapter_path=os.environ.get("ECOREX_RELEASE_SIGNER_ADAPTER") or None,
            adapter_sha256=(
                os.environ.get("ECOREX_RELEASE_SIGNER_ADAPTER_SHA256") or None
            ),
            environment=os.environ,
        )
    except (TypeError, ValueError):
        raise ValueError("site_authorization_signer_configuration_invalid") from None


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        release_stage = args.staging_release_dir.expanduser().resolve(strict=True)
        if release_stage.name != args.release_id:
            raise ValueError("site_authorization_staging_identity_mismatch")
        paths = DeploymentPaths.for_offline_staging(release_stage)
        site = _validate_unsigned_staged_site(
            args.release_id,
            paths=paths,
            expected_owner_uid=None,
            authorization_expected=False,
        )
        signer = _signer()
        admin_identity = build_admin_deployment_identity(
            args.cloud_artifact_root,
            public_keys={signer.key_id: signer.public_key_bytes},
        )
        if admin_identity["cloud_version"] != site.version:
            raise ValueError("admin_cloud_version_mismatch")
        site = dataclasses.replace(site, admin_identity=admin_identity)
        authorization = sign_public_site_authorization(site, signer=signer)
        verify_public_site_authorization(
            site,
            authorization,
            public_keys={signer.key_id: signer.public_key_bytes},
        )
        output = release_stage / "deployment-authorization.json"
        write_new_json_file(
            authorization,
            output,
            code="site_authorization_already_exists",
        )
        payload = output.read_bytes()
        signing_receipt = signer.receipts[-1]
        print(
            json.dumps(
                {
                    "ok": True,
                    "release_id": site.release_id,
                    "version": site.version,
                    "site_tree_sha256": site.tree_sha256,
                    "direct_receipt_sha256": site.direct_receipt_sha256,
                    "authorization_sha256": hashlib.sha256(payload).hexdigest(),
                    "key_id": signer.key_id,
                    "signing_payload_sha256": signing_receipt.payload_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except Exception as error:
        code = error.code if isinstance(error, PublicSiteDeployError) else str(error)
        if re.fullmatch(r"[a-z][a-z0-9_]{2,127}", code) is None:
            code = "site_authorization_signing_failed"
        print(
            json.dumps({"ok": False, "code": code}, sort_keys=True),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
