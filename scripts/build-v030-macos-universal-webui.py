#!/usr/bin/env python3
"""Produce the e-Mate macOS universal WebUI ZIP.

The artifact remains a browser WebUI distribution: React assets, a universal
Bootstrap/Runtime launcher, and the exact protected arm64/x64 stage payloads.
It is not an Electron, SwiftUI, or native product-UI bundle.  Apple does not
support stapling a ticket to a ZIP, so the accepted notarization submission is
recorded and validated without inventing a staple receipt.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex import __version__  # noqa: E402
from ecorex.pack_catalog import REQUIRED_CAPABILITY_PACK_IDS  # noqa: E402
from ecorex.release.legacy_webui_manifest import (  # noqa: E402
    RECEIPT_SCHEMA as FINAL_RECEIPT_SCHEMA,
)
from ecorex.release.windows_webui import (  # noqa: E402
    WINDOWS_RECEIPT_SCHEMA,
    _verify_candidate_receipt,
    _verify_webui_contract,
    verify_windows_webui_package,
)
from ecorex.release.web_bundle import (  # noqa: E402
    WebBundleBuildInput,
    scan_web_bundle,
)
from ecorex.update import Ed25519SignatureVerifier, ReleaseManifest  # noqa: E402


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIST_SCHEMA = "emate.macos-distribution-receipt.v1"


class MacWebUIBuildError(RuntimeError):
    pass


def _fail(code: str) -> None:
    raise MacWebUIBuildError(code)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(*command: str, stderr_output: bool = False) -> str:
    try:
        result = subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30 * 60,
        )
    except (OSError, subprocess.SubprocessError):
        _fail("macos_distribution_command_failed")
    return (result.stderr if stderr_output else result.stdout).strip()


def _regular(path: Path, code: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        _fail(code)
    if not resolved.is_file() or resolved.is_symlink():
        _fail(code)
    return resolved


def _directory(path: Path, code: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        _fail(code)
    if not resolved.is_dir() or resolved.is_symlink():
        _fail(code)
    return resolved


def _require_tools(*, terminal_distribution: bool) -> None:
    if platform.system() != "Darwin":
        _fail("macos_host_required")
    tools = [
        "/usr/bin/codesign",
        "/usr/bin/ditto",
        "/usr/bin/lipo",
    ]
    if not terminal_distribution:
        tools.extend(("/usr/sbin/spctl", "/usr/bin/xcrun"))
    for tool in tools:
        if not Path(tool).is_file():
            _fail("macos_distribution_toolchain_missing")


def _verify_signed_slice(
    path: Path, architecture: str, identity: str
) -> dict[str, str]:
    _run("/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(path))
    if architecture not in set(_run("/usr/bin/lipo", "-archs", str(path)).split()):
        _fail("macos_stage_slice_missing")
    diagnostic = _run(
        "/usr/bin/codesign", "-d", "--verbose=4", "-r-", str(path), stderr_output=True
    )
    values: dict[str, str] = {}
    for line in diagnostic.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"Authority", "TeamIdentifier", "Identifier"}:
            values[key.casefold()] = value.strip()
        elif line.strip().startswith("designated =>"):
            values["requirement"] = line.strip()
    if (
        not values.get("authority", "").startswith("Developer ID Application:")
        or identity not in values["authority"]
        or not values.get("teamidentifier")
        or not values.get("identifier")
        or not values.get("requirement")
        or "Runtime Version=" not in diagnostic
    ):
        _fail("macos_developer_id_readback_invalid")
    return values


def _verify_terminal_slice(path: Path, architecture: str) -> dict[str, str]:
    _run("/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(path))
    if architecture not in set(_run("/usr/bin/lipo", "-archs", str(path)).split()):
        _fail("macos_stage_slice_missing")
    diagnostic = _run(
        "/usr/bin/codesign", "-d", "--verbose=4", str(path), stderr_output=True
    )
    identifier = next(
        (
            line.partition("=")[2].strip()
            for line in diagnostic.splitlines()
            if line.startswith("Identifier=")
        ),
        "",
    )
    if "Signature=adhoc" not in diagnostic or not identifier:
        _fail("macos_terminal_signature_invalid")
    return {
        "architecture": architecture,
        "identifier": identifier,
        "signature": "adhoc",
    }


def _write_legacy_receipt(
    output: Path, *, version: str, windows: Path, macos: Path, generated_at: str
) -> Path:
    if (
        windows.name != f"EcoreX_{version}-webui-windows-x64.zip"
        or macos.name != f"EcoreX_{version}-webui-macos-universal.zip"
    ):
        _fail("webui_package_name_invalid")
    artifacts = []
    for artifact_id, path in (
        ("webui-windows-x64", _regular(windows, "windows_webui_package_missing")),
        ("webui-macos-universal", _regular(macos, "macos_webui_package_missing")),
    ):
        artifacts.append(
            {
                "id": artifact_id,
                "file_name": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    receipt = output / "webui-build-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": FINAL_RECEIPT_SCHEMA,
                "version": version,
                "status": "verified",
                "generated_at": generated_at,
                "artifacts": artifacts,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return receipt


def _write_distribution_receipt(path: Path, value: dict[str, Any]) -> None:
    mode = value.get("distribution_mode", "developer-id-notarized")
    notarization = value.get("notarization", {})
    stapling = value.get("stapling")
    mode_valid = (
        mode == "developer-id-notarized"
        and notarization.get("status") == "Accepted"
        and notarization.get("submission_id")
        and stapling
        == {"applicable": False, "reason": "zip-ticket-cannot-be-stapled"}
    ) or (
        mode == "terminal-command"
        and notarization
        == {
            "status": "not-applicable",
            "reason": "terminal-command-distribution",
        }
        and stapling == {"applicable": False, "reason": "no-app-bundle"}
    )
    if (
        value.get("schema") != _DIST_SCHEMA
        or value.get("status") != "verified"
        or not mode_valid
    ):
        _fail("macos_distribution_receipt_invalid")
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _verify_windows_partial_receipt(
    receipt_path: Path,
    windows: Path,
    manifest: ReleaseManifest,
    manifest_path: Path,
    candidate_receipt: Path,
) -> dict[str, Any]:
    try:
        win = json.loads(receipt_path.read_text(encoding="utf-8"))
        win_item = win["artifacts"][0]
        provenance = win_item["provenance"]
    except Exception:
        _fail("windows_webui_receipt_invalid")
    if (
        win.get("schema") != WINDOWS_RECEIPT_SCHEMA
        or win.get("status") != "partial"
        or win.get("production_eligible") is not True
        or win_item.get("id") != "webui-windows-x64"
        or win_item.get("file_name") != windows.name
        or win_item.get("size_bytes") != windows.stat().st_size
        or win_item.get("sha256") != _sha256(windows)
        or provenance.get("release_id") != manifest.release_id
        or provenance.get("build_digest") != manifest.build_digest
        or provenance.get("manifest_sha256") != _sha256(manifest_path)
        or provenance.get("candidate_receipt_sha256") != _sha256(candidate_receipt)
        or provenance.get("signing_key_id") != manifest.signature.key_id
        or provenance.get("core_artifact_id") != "core-windows-x64"
        or provenance.get("core_sha256") != manifest.artifact("core-windows-x64").sha256
        or provenance.get("web_manifest_sha256")
        != manifest.artifact("web-manifest").sha256
        or not isinstance(provenance.get("included_artifact_ids"), list)
        or "core-windows-x64" not in provenance["included_artifact_ids"]
        or "web-manifest" not in provenance["included_artifact_ids"]
        or provenance.get("mode") != "production"
    ):
        _fail("windows_webui_receipt_invalid")
    return win


def build(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    terminal_distribution = bool(getattr(args, "terminal_distribution", False))
    _require_tools(terminal_distribution=terminal_distribution)
    if (
        args.version != __version__
        or not _COMMIT.fullmatch(args.commit_sha)
        or (
            not terminal_distribution
            and (
                not args.identity.strip()
                or not args.notary_profile.strip()
                or not args.notary_keychain.strip()
            )
        )
    ):
        _fail("macos_distribution_authority_missing")
    candidate = _directory(args.candidate_root, "signed_candidate_missing")
    release = _directory(
        candidate / "output" / "release", "signed_candidate_release_missing"
    )
    candidate_receipt = _regular(
        candidate / "output" / "candidate-build-receipt.json",
        "signed_candidate_receipt_missing",
    )
    manifest_path = _regular(
        release / "release-manifest.json", "signed_candidate_manifest_missing"
    )
    signature_report = candidate / "macos-webui-signature-report.json"
    _run(
        sys.executable,
        str(ROOT / "scripts" / "verify-v1-release-signatures.py"),
        "--release-dir",
        str(release),
        "--report",
        str(signature_report),
    )
    try:
        manifest = ReleaseManifest.from_json(manifest_path.read_bytes())
        candidate_payload = candidate_receipt.read_bytes()
        candidate_value = json.loads(candidate_payload)
    except Exception:
        _fail("signed_candidate_identity_invalid")
    if (
        manifest.version != args.version
        or candidate_value.get("commit_sha") != args.commit_sha
        or candidate_value.get("status") != "passed"
    ):
        _fail("signed_candidate_identity_invalid")
    try:
        key_id = os.environ["ECOREX_RELEASE_SIGNER_KEY_ID"]
        public = base64.b64decode(
            os.environ["ECOREX_RELEASE_SIGNER_PUBLIC_KEY"], validate=True
        )
        if len(public) != 32:
            raise ValueError
        verifier = Ed25519SignatureVerifier({key_id: public})
        _verify_candidate_receipt(
            candidate_value,
            candidate_payload=candidate_payload,
            manifest=manifest,
            manifest_payload=manifest_path.read_bytes(),
            verifier=verifier,
        )
    except Exception:
        _fail("signed_candidate_receipt_invalid")
    selected = tuple(
        item
        for item in manifest.artifacts
        if (item.platform == "macos" and item.architecture in {"arm64", "x64"})
        or item.artifact_id == "web-manifest"
    )
    required = {
        "core-macos-arm64",
        "core-macos-x64",
        "bootstrap-macos-arm64",
        "bootstrap-macos-x64",
        "web-manifest",
        *(
            f"capability-pack-{kind}-macos-{arch}"
            for kind in REQUIRED_CAPABILITY_PACK_IDS
            for arch in ("arm64", "x64")
        ),
        *(
            f"capability-pack-{kind}-macos-{arch}-manifest"
            for kind in REQUIRED_CAPABILITY_PACK_IDS
            for arch in ("arm64", "x64")
        ),
    }
    if {item.artifact_id for item in selected} != required:
        _fail("signed_candidate_macos_artifacts_incomplete")
    web = _directory(args.web_dist, "web_dist_missing")
    scanned_web = scan_web_bundle(WebBundleBuildInput(web))
    web_contracts = tuple(
        _verify_webui_contract(
            release / manifest.artifact(f"core-macos-{architecture}").file_name,
            release / manifest.artifact("web-manifest").file_name,
            manifest=manifest,
            verifier=verifier,
            expected_platform="macos",
            expected_architecture=architecture,
        )
        for architecture in ("arm64", "x64")
    )
    if {
        scanned_web.bundle_sha256,
        *(contract["bundle_sha256"] for contract in web_contracts),
    } != {scanned_web.bundle_sha256}:
        _fail("web_bundle_candidate_mismatch")
    windows = _regular(args.windows_package, "windows_webui_package_missing")
    windows_receipt = _regular(args.windows_receipt, "windows_webui_receipt_missing")
    try:
        verified_windows = verify_windows_webui_package(
            windows,
            trusted_public_keys={key_id: public},
            production=True,
            production_key_ids=frozenset({key_id}),
        )
    except Exception:
        _fail("windows_webui_package_invalid")
    if (
        verified_windows.get("release_id") != manifest.release_id
        or verified_windows.get("build_digest") != manifest.build_digest
        or verified_windows.get("manifest_sha256") != _sha256(manifest_path)
        or verified_windows.get("candidate_receipt_sha256")
        != _sha256(candidate_receipt)
        or verified_windows.get("signing_key_id") != manifest.signature.key_id
    ):
        _fail("windows_webui_package_invalid")
    _verify_windows_partial_receipt(
        windows_receipt,
        windows,
        manifest,
        manifest_path,
        candidate_receipt,
    )
    output = args.output.resolve()
    if output.exists():
        _fail("macos_webui_output_exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(
        tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent)
    )
    staging = temporary_root / "verified-output"
    staging.mkdir()
    try:
        package_root = temporary_root / "e-Mate WebUI"
        package_root.mkdir()
        signed = package_root / "signed"
        signed.mkdir()
        shutil.copy2(manifest_path, signed / manifest_path.name)
        evidence = package_root / "evidence"
        evidence.mkdir()
        shutil.copy2(candidate_receipt, evidence / candidate_receipt.name)
        shutil.copy2(signature_report, evidence / signature_report.name)
        signed_binaries: dict[str, dict[str, str]] = {}
        inspection = temporary_root / "inspection"
        for artifact in selected:
            shutil.copy2(release / artifact.file_name, signed / artifact.file_name)
        for arch, slice_name in (("arm64", "arm64"), ("x64", "x86_64")):
            for kind, binary in (("bootstrap", "ecorex-bootstrap"), ("core", "ecorex")):
                artifact = manifest.artifact(f"{kind}-macos-{arch}")
                target = inspection / f"{kind}-{arch}"
                target.mkdir(parents=True)
                _run(
                    "/usr/bin/ditto",
                    "-x",
                    "-k",
                    str(release / artifact.file_name),
                    str(target),
                )
                signed_binaries[f"{arch}/{binary}"] = (
                    _verify_terminal_slice(target / "bin" / binary, slice_name)
                    if terminal_distribution
                    else _verify_signed_slice(
                        target / "bin" / binary, slice_name, args.identity
                    )
                )
        installer = package_root / "Install e-Mate WebUI.command"
        installer.write_text(
            '#!/bin/sh\nset -eu\nBASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\ncase "$(uname -m)" in arm64) TARGET=arm64 ;; x86_64) TARGET=x64 ;; *) echo "e-Mate 不支持当前架构" >&2; exit 78 ;; esac\nDEST="${TMPDIR:-/tmp}/emate-bootstrap-$TARGET-$$"\nmkdir -m 700 "$DEST"\ntrap \'rm -rf "$DEST"\' EXIT HUP INT TERM\nARCHIVE=$(find "$BASE_DIR/signed" -maxdepth 1 -type f -name "ecorex-bootstrap-macos-$TARGET-*.zip" -print)\ntest -n "$ARCHIVE" && test "$(printf "%s\\n" "$ARCHIVE" | wc -l | tr -d " ")" = 1\n/usr/bin/ditto -x -k "$ARCHIVE" "$DEST"\nexec "$DEST/bin/ecorex-bootstrap" --local-release "$BASE_DIR/signed" "$@"\n',
            encoding="utf-8",
        )
        installer.chmod(0o755)
        package = staging / f"EcoreX_{args.version}-webui-macos-universal.zip"
        _run(
            "/usr/bin/ditto",
            "-c",
            "-k",
            "--keepParent",
            str(package_root),
            str(package),
        )
        if terminal_distribution:
            distribution_mode = "terminal-command"
            developer_id = {
                "hardened_runtime": False,
                "signature_mode": "candidate-ad-hoc",
                "signed_candidate_binaries": signed_binaries,
            }
            notarization = {
                "status": "not-applicable",
                "reason": "terminal-command-distribution",
            }
            stapling = {"applicable": False, "reason": "no-app-bundle"}
            assessments: dict[str, Any] = {
                "app_bundle": {
                    "status": "not-applicable",
                    "reason": "no-app-bundle",
                }
            }
        else:
            notary_raw = _run(
                "/usr/bin/xcrun",
                "notarytool",
                "submit",
                str(package),
                "--keychain-profile",
                args.notary_profile,
                "--keychain",
                args.notary_keychain,
                "--wait",
                "--output-format",
                "json",
            )
            try:
                notary = json.loads(notary_raw)
                submission_id = str(uuid.UUID(str(notary.get("id"))))
            except (ValueError, TypeError, json.JSONDecodeError):
                _fail("macos_notarization_receipt_invalid")
            if notary.get("status") != "Accepted":
                _fail("macos_notarization_rejected")
            distribution_mode = "developer-id-notarized"
            developer_id = {
                "hardened_runtime": True,
                "signed_candidate_binaries": signed_binaries,
            }
            notarization = {"status": "Accepted", "submission_id": submission_id}
            stapling = {
                "applicable": False,
                "reason": "zip-ticket-cannot-be-stapled",
            }
            assessments = {}
            for key in signed_binaries:
                architecture, binary = key.split("/", 1)
                kind = "bootstrap" if binary == "ecorex-bootstrap" else "core"
                result = _run(
                    "/usr/sbin/spctl",
                    "--assess",
                    "--type",
                    "execute",
                    "--verbose=4",
                    str(inspection / f"{kind}-{architecture}" / "bin" / binary),
                    stderr_output=True,
                )
                assessments[key] = {
                    "status": "passed",
                    "readback_sha256": hashlib.sha256(result.encode()).hexdigest(),
                }
        shutil.copy2(windows, staging / windows.name)
        generated_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        distribution = staging / "macos-distribution-receipt.json"
        _write_distribution_receipt(
            distribution,
            {
                "schema": _DIST_SCHEMA,
                "version": args.version,
                "status": "verified",
                "distribution_mode": distribution_mode,
                "generated_at": generated_at,
                "source_commit": args.commit_sha,
                "web_bundle": {"sha256": scanned_web.bundle_sha256},
                "candidate": {
                    "release_id": manifest.release_id,
                    "build_digest": manifest.build_digest,
                    "manifest_sha256": _sha256(manifest_path),
                    "candidate_receipt_sha256": _sha256(candidate_receipt),
                    "artifacts": [
                        {
                            "artifact_id": item.artifact_id,
                            "sha256": item.sha256,
                            "size_bytes": item.size_bytes,
                        }
                        for item in selected
                    ],
                },
                "developer_id": developer_id,
                "notarization": notarization,
                "stapling": stapling,
                "gatekeeper": assessments,
                "package": {
                    "file_name": package.name,
                    "sha256": _sha256(package),
                    "size_bytes": package.stat().st_size,
                },
            },
        )
        receipt = _write_legacy_receipt(
            staging,
            version=args.version,
            windows=staging / windows.name,
            macos=package,
            generated_at=generated_at,
        )
        os.replace(staging, output)
        return output / package.name, output / receipt.name, output / distribution.name
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-root", required=True, type=Path)
    parser.add_argument("--web-dist", required=True, type=Path)
    parser.add_argument("--windows-package", required=True, type=Path)
    parser.add_argument("--windows-receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", default=__version__)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--terminal-distribution", action="store_true")
    parser.add_argument(
        "--identity", default=os.environ.get("ECOREX_APPLE_DEVELOPER_ID", "")
    )
    parser.add_argument(
        "--notary-profile", default=os.environ.get("ECOREX_APPLE_NOTARY_PROFILE", "")
    )
    parser.add_argument(
        "--notary-keychain", default=os.environ.get("ECOREX_APPLE_NOTARY_KEYCHAIN", "")
    )
    return parser


def main() -> int:
    try:
        package, receipt, distribution = build(_parser().parse_args())
    except MacWebUIBuildError as error:
        print(json.dumps({"ok": False, "code": str(error)}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "package": str(package),
                "receipt": str(receipt),
                "distribution_receipt": str(distribution),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
