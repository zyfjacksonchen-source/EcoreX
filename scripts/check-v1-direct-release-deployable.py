#!/usr/bin/env python3
"""Fail closed before a direct-waiver release site can be deployed.

The checked-in public site deliberately remains unpublished.  An operator must
copy it to a staging directory, publish the immutable release to all three
origins, generate a signed ``published`` Bootstrap index from that exact
publication receipt, and pass this checker.  Therefore neither an unsigned
Candidate nor the repository's unpublished placeholder can reach the site.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.control_plane.cli import (  # noqa: E402
    _expected_public_bootstrap_discovery,
)
from ecorex.release import (  # noqa: E402
    DirectReleaseWaiverError,
    parse_external_public_key_description,
    validate_direct_release_waiver,
)
from ecorex.release.evidence_io import (  # noqa: E402
    read_stable_regular_file,
    strict_json_loads,
)
from ecorex.release.process_boundary import (  # noqa: E402
    BoundedProcessError,
    run_bounded_process,
)
from ecorex.update import ReleaseManifest, SignatureEnvelope  # noqa: E402


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSET = re.compile(r"^(?:site|styles)\.([0-9a-f]{12})\.(?:js|css)$")
_ADAPTER = ROOT / "scripts" / "ecorex-v1-dpapi-ed25519-signer.py"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--candidate-receipt", required=True, type=Path)
    parser.add_argument("--waiver", required=True, type=Path)
    parser.add_argument("--publication-receipt", required=True, type=Path)
    parser.add_argument("--site-root", required=True, type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--operator-instruction-sha256", required=True)
    parser.add_argument("--publication-key-description", required=True, type=Path)
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _keys(publication_description: Path) -> tuple[tuple[str, bytes], tuple[str, bytes]]:
    executable = Path(sys.executable).resolve(strict=True)
    adapter = _ADAPTER.resolve(strict=True)
    executable_sha = _sha256(executable)
    adapter_sha = _sha256(adapter)
    try:
        result = run_bounded_process(
            (str(executable), str(adapter), "describe"),
            payload=None,
            cwd=ROOT,
            environment=os.environ,
            timeout_seconds=15,
            max_stdout_bytes=64 * 1024,
            max_stderr_bytes=1024,
            hide_window=os.name == "nt",
        )
    except (OSError, BoundedProcessError):
        raise ValueError("direct_release_key_store_unavailable") from None
    if result.returncode != 0 or not result.stdout:
        raise ValueError("direct_release_key_store_unavailable")
    value = strict_json_loads(
        result.stdout, code="direct_release_key_description_invalid"
    )
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("status") != "ready"
        or _sha256(executable) != executable_sha
        or _sha256(adapter) != adapter_sha
    ):
        raise ValueError("direct_release_key_description_invalid")
    entry = value.get("release")
    if not isinstance(entry, dict):
        raise ValueError("direct_release_key_description_invalid")
    try:
        release_public = base64.b64decode(
            entry.get("public_key_base64"), validate=True
        )
    except (TypeError, ValueError):
        raise ValueError("direct_release_key_description_invalid") from None
    release_id = entry.get("key_id")
    if (
        not isinstance(release_id, str)
        or len(release_public) != 32
        or hashlib.sha256(release_public).hexdigest()
        != entry.get("public_key_sha256")
        or not release_id.startswith("ecorex-direct-release-")
    ):
        raise ValueError("direct_release_key_description_invalid")
    payload = read_stable_regular_file(
        publication_description,
        maximum_bytes=64 * 1024,
        code="direct_release_public_key_description_invalid",
    )
    publication_value = strict_json_loads(
        payload, code="direct_release_public_key_description_invalid"
    )
    if not isinstance(publication_value, dict):
        raise ValueError("direct_release_public_key_description_invalid")
    try:
        publication_id, publication_public = parse_external_public_key_description(
            publication_value, expected_role="publication"
        )
    except DirectReleaseWaiverError:
        raise ValueError("direct_release_public_key_description_invalid") from None
    if release_id == publication_id or release_public == publication_public:
        raise ValueError("direct_release_keys_not_independent")
    return (release_id, release_public), (publication_id, publication_public)


def _site(root_value: Path) -> tuple[dict[str, object], bytes]:
    root = root_value.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("direct_release_site_invalid")
    for forbidden in (
        "site.js",
        "styles.css",
        "manifest.json",
        "release-index.json",
        "admin/index.html",
        "admin/admin.js",
        "admin/admin.css",
    ):
        if (root / forbidden).exists():
            raise ValueError("direct_release_site_contains_legacy_authority")
    scripts = sorted(root.glob("site.*.js"))
    styles = sorted(root.glob("styles.*.css"))
    if len(scripts) != 1 or len(styles) != 1:
        raise ValueError("direct_release_site_assets_invalid")
    for path in (*scripts, *styles):
        match = _ASSET.fullmatch(path.name)
        if match is None or not _sha256(path).startswith(match.group(1)):
            raise ValueError("direct_release_site_assets_invalid")
    html = read_stable_regular_file(
        root / "index.html",
        maximum_bytes=256 * 1024,
        code="direct_release_site_html_invalid",
    )
    try:
        html_text = html.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("direct_release_site_html_invalid") from None
    if (
        f'./{scripts[0].name}' not in html_text
        or f'./{styles[0].name}' not in html_text
        or 'href="/ecorex-agent/admin/"' not in html_text
        or 'href="/admin/"' in html_text
        or "__HASH__" in html_text
    ):
        raise ValueError("direct_release_site_html_invalid")
    pointer_bytes = read_stable_regular_file(
        root / "public-bootstrap-index.json",
        maximum_bytes=256 * 1024,
        code="direct_release_public_index_invalid",
    )
    pointer = strict_json_loads(
        pointer_bytes, code="direct_release_public_index_invalid"
    )
    if not isinstance(pointer, dict) or pointer.get("status") != "published":
        raise ValueError("direct_release_unpublished_site_forbidden")
    return pointer, pointer_bytes


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if _SHA256.fullmatch(args.operator_instruction_sha256) is None:
            raise ValueError("direct_release_instruction_hash_invalid")
        (release_id, release_public), (publication_id, publication_public) = _keys(
            args.publication_key_description
        )
        pointer, pointer_bytes = _site(args.site_root)
        authority = pointer.get("authority")
        freshness = pointer.get("freshness")
        if not isinstance(authority, dict) or not isinstance(freshness, dict):
            raise ValueError("direct_release_public_index_invalid")
        authority_signature = SignatureEnvelope.from_dict(authority.get("signature"))
        freshness_signature = SignatureEnvelope.from_dict(freshness.get("signature"))
        verified, publication_receipt_sha256, expected_pointer = (
            _expected_public_bootstrap_discovery(
                release_dir=args.release_dir,
                publication_receipt=args.publication_receipt,
                trusted_keys=[
                    f"{release_id}={base64.b64encode(release_public).decode('ascii')}"
                ],
                trusted_freshness_keys=[
                    f"{publication_id}={base64.b64encode(publication_public).decode('ascii')}"
                ],
                authority_signature=authority_signature,
                freshness_signature=freshness_signature,
                freshness_issued_at=freshness.get("issued_at"),
                freshness_expires_at=freshness.get("expires_at"),
            )
        )
        if expected_pointer != pointer:
            raise ValueError("direct_release_public_index_mismatch")
        manifest: ReleaseManifest = verified.manifest
        manifest_sha256 = verified.expected_sha256["release-manifest.json"]
        receipt_bytes = read_stable_regular_file(
            args.candidate_receipt,
            maximum_bytes=4 * 1024 * 1024,
            code="direct_release_candidate_receipt_invalid",
        )
        waiver_bytes = read_stable_regular_file(
            args.waiver,
            maximum_bytes=2 * 1024 * 1024,
            code="direct_release_waiver_invalid",
        )
        waiver = strict_json_loads(waiver_bytes, code="direct_release_waiver_invalid")
        if not isinstance(waiver, dict):
            raise ValueError("direct_release_waiver_invalid")
        validate_direct_release_waiver(
            waiver,
            expected_manifest=manifest,
            expected_manifest_sha256=manifest_sha256,
            expected_candidate_receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
            expected_commit_sha=args.expected_commit,
            expected_operator_instruction_sha256=args.operator_instruction_sha256,
            release_public_key=release_public,
            publication_key_id=publication_id,
            publication_public_key=publication_public,
        )
        if pointer["release"]["publication_receipt_sha256"] != (
            publication_receipt_sha256
        ):
            raise ValueError("direct_release_publication_receipt_mismatch")
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": "deployable-with-explicit-operator-waiver",
                    "protected_pipeline_passed": False,
                    "release_id": manifest.release_id,
                    "version": manifest.version,
                    "manifest_sha256": manifest_sha256,
                    "waiver_sha256": hashlib.sha256(waiver_bytes).hexdigest(),
                    "publication_receipt_sha256": publication_receipt_sha256,
                    "public_index_sha256": hashlib.sha256(pointer_bytes).hexdigest(),
                    "public_index_status": "published",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except Exception as exc:
        if isinstance(exc, DirectReleaseWaiverError):
            code = str(exc)
        elif isinstance(exc, ValueError) and re.fullmatch(
            r"[a-z][a-z0-9_]{2,127}", str(exc)
        ):
            code = str(exc)
        else:
            code = "direct_release_not_deployable"
        print(json.dumps({"ok": False, "code": code}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
