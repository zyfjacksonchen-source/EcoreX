#!/usr/bin/env python3
"""Smoke a local v0.2.2 web-linux-service deploy and rollback.

This is intentionally a local filesystem smoke. It validates the release
tarball, then simulates the installer's current-release pointer swap and
rollback without requiring root, systemd, network, or production hosts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE = ROOT / "release-artifacts" / "EcoreX_0.2.2-web-linux-service.tar.gz"
DEFAULT_SHA256_FILE = DEFAULT_PACKAGE.with_suffix(DEFAULT_PACKAGE.suffix + ".sha256")

REQUIRED_BUNDLE_PATHS = (
    "README.txt",
    "runtime/app.py",
    "runtime/requirements.txt",
    "runtime/channel/web/chat.html",
    "runtime/channel/web/static/app/index.html",
    "scripts/install-ecorex-web.sh",
    "scripts/check-ecorex-web-release.sh",
    "service/caddy/ecorex-agent.routes.caddy",
    "service/nginx/ecorex-agent.conf.example",
    "service/systemd/ecorex-web.service.example",
    "release.json",
    "checksums.json",
    "SHA256SUMS.txt",
)


class SmokeError(RuntimeError):
    """Raised when the deploy/rollback smoke contract fails."""


def _repo_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # pragma: no cover - error path is surfaced by CLI
        raise SmokeError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SmokeError(f"JSON root must be an object: {path}")
    return payload


def _safe_member_name(member: tarfile.TarInfo) -> str:
    name = member.name.replace("\\", "/")
    if name.startswith("/") or name.startswith("../") or "/../" in name:
        raise SmokeError(f"unsafe tar member path: {member.name}")
    if member.issym() or member.islnk():
        link = (member.linkname or "").replace("\\", "/")
        if link.startswith("/") or link.startswith("../") or "/../" in link:
            raise SmokeError(f"unsafe tar link target: {member.name} -> {member.linkname}")
    return name


def _safe_extract(package: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(package, "r:gz") as archive:
        for member in archive.getmembers():
            name = _safe_member_name(member)
            destination = (target / name).resolve()
            if target.resolve() not in (destination, *destination.parents):
                raise SmokeError(f"tar member escapes extraction root: {member.name}")
        archive.extractall(target)


def _find_bundle_root(extract_dir: Path) -> Path:
    if (extract_dir / "runtime" / "app.py").is_file():
        return extract_dir
    candidates = sorted(path for path in extract_dir.iterdir() if path.is_dir())
    for candidate in candidates:
        if (candidate / "runtime" / "app.py").is_file():
            return candidate
    raise SmokeError("release tarball does not contain runtime/app.py")


def _verify_external_sha256(package: Path) -> dict[str, Any]:
    sha_path = package.with_suffix(package.suffix + ".sha256")
    actual = _sha256_file(package)
    if not sha_path.is_file():
        raise SmokeError(f"missing package sha256 file: {sha_path}")
    raw = sha_path.read_text(encoding="ascii").strip()
    parts = raw.split()
    if len(parts) < 2:
        raise SmokeError(f"invalid package sha256 file: {sha_path}")
    expected_hash, expected_name = parts[0].upper(), parts[-1]
    if expected_hash != actual:
        raise SmokeError(f"package sha256 mismatch: expected {expected_hash}, got {actual}")
    if expected_name != package.name:
        raise SmokeError(f"package sha256 filename mismatch: expected {package.name}, got {expected_name}")
    return {
        "path": _repo_relative(sha_path),
        "sha256": expected_hash,
        "filename": expected_name,
        "matches": True,
    }


def _verify_required_paths(bundle_root: Path) -> list[str]:
    missing = [relative for relative in REQUIRED_BUNDLE_PATHS if not (bundle_root / relative).exists()]
    if missing:
        raise SmokeError(f"release bundle missing required paths: {', '.join(missing)}")
    return list(REQUIRED_BUNDLE_PATHS)


def _verify_release_json(bundle_root: Path, expected_version: str) -> dict[str, Any]:
    payload = _load_json(bundle_root / "release.json")
    expected = {
        "product": "EcoreX",
        "version": expected_version,
        "artifactId": "web-linux-service",
        "artifactFile": f"EcoreX_{expected_version}-web-linux-service.tar.gz",
        "includesDesktopArtifacts": False,
    }
    mismatches = {
        key: {"expected": value, "actual": payload.get(key)}
        for key, value in expected.items()
        if payload.get(key) != value
    }
    if mismatches:
        raise SmokeError(f"release.json mismatch: {mismatches}")
    return {
        "product": payload.get("product"),
        "version": payload.get("version"),
        "artifactId": payload.get("artifactId"),
        "artifactFile": payload.get("artifactFile"),
        "webBuild": payload.get("webBuild"),
        "includesDesktopArtifacts": payload.get("includesDesktopArtifacts"),
        "serviceName": payload.get("serviceName"),
    }


def _assert_safe_relative(value: str) -> Path:
    path = Path(value)
    normalized = value.replace("\\", "/")
    if path.is_absolute() or normalized.startswith("../") or "/../" in normalized:
        raise SmokeError(f"unsafe checksum path: {value}")
    return path


def _verify_checksums(bundle_root: Path, expected_version: str) -> dict[str, Any]:
    checksums = _load_json(bundle_root / "checksums.json")
    if checksums.get("product") != "EcoreX":
        raise SmokeError("checksums.json product mismatch")
    if checksums.get("version") != expected_version:
        raise SmokeError("checksums.json version mismatch")
    if checksums.get("artifactId") != "web-linux-service":
        raise SmokeError("checksums.json artifactId mismatch")
    files = checksums.get("files")
    if not isinstance(files, list) or not files:
        raise SmokeError("checksums.json files must be a non-empty list")

    verified_paths: set[str] = set()
    for row in files:
        if not isinstance(row, dict):
            raise SmokeError("checksum row must be an object")
        relative = str(row.get("path") or "")
        if not relative:
            raise SmokeError("checksum row missing path")
        file_path = bundle_root / _assert_safe_relative(relative)
        if not file_path.is_file():
            raise SmokeError(f"checksum file missing: {relative}")
        expected_size = int(row.get("size"))
        expected_hash = str(row.get("sha256") or "").upper()
        actual_size = file_path.stat().st_size
        actual_hash = _sha256_file(file_path)
        if actual_size != expected_size:
            raise SmokeError(f"checksum size mismatch for {relative}: {actual_size} != {expected_size}")
        if actual_hash != expected_hash:
            raise SmokeError(f"checksum hash mismatch for {relative}: {actual_hash} != {expected_hash}")
        verified_paths.add(relative)

    sums_path = bundle_root / "SHA256SUMS.txt"
    sums_rows: dict[str, str] = {}
    for raw_line in sums_path.read_text(encoding="ascii").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise SmokeError(f"invalid SHA256SUMS line: {raw_line}")
        checksum, relative = parts[0].upper(), parts[1].strip()
        if relative not in verified_paths:
            raise SmokeError(f"SHA256SUMS contains unknown path: {relative}")
        if checksum != _sha256_file(bundle_root / _assert_safe_relative(relative)):
            raise SmokeError(f"SHA256SUMS hash mismatch for {relative}")
        sums_rows[relative] = checksum

    if set(sums_rows) != verified_paths:
        missing = sorted(verified_paths - set(sums_rows))
        raise SmokeError(f"SHA256SUMS missing paths: {', '.join(missing)}")
    return {
        "checksumsJsonFilesVerified": len(verified_paths),
        "sha256SumsFilesVerified": len(sums_rows),
        "releaseJsonCovered": "release.json" in verified_paths,
    }


def _read_release_version(path: Path) -> str:
    release_path = path / "release.json"
    if not release_path.is_file():
        return ""
    payload = _load_json(release_path)
    return str(payload.get("version") or "")


class CurrentPointer:
    def __init__(self, install_root: Path) -> None:
        self.install_root = install_root
        self.current = install_root / "current"
        self.pointer_file = install_root / "current-release.json"
        self.method = self._choose_method()

    def _choose_method(self) -> str:
        probe_target = self.install_root / "probe-target"
        probe_link = self.install_root / "probe-current"
        probe_target.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(probe_target, probe_link, target_is_directory=True)
            return "symlink"
        except OSError:
            return "manifest-pointer-fallback"
        finally:
            if probe_link.exists() or probe_link.is_symlink():
                probe_link.unlink()
            shutil.rmtree(probe_target, ignore_errors=True)

    def set(self, target: Path, release_id: str) -> None:
        if self.current.exists() or self.current.is_symlink():
            if self.current.is_symlink() or self.current.is_file():
                self.current.unlink()
            else:
                shutil.rmtree(self.current)
        if self.method == "symlink":
            os.symlink(target, self.current, target_is_directory=True)
        else:
            self.pointer_file.write_text(
                json.dumps({
                    "releaseId": release_id,
                    "target": str(target.resolve()),
                    "method": self.method,
                }, indent=2, sort_keys=True),
                encoding="utf-8",
            )

    def resolve(self) -> Path:
        if self.method == "symlink":
            if not self.current.is_symlink():
                raise SmokeError("current symlink was not created")
            return self.current.resolve()
        payload = _load_json(self.pointer_file)
        return Path(str(payload.get("target") or "")).resolve()


def _simulate_deploy_and_rollback(bundle_root: Path, expected_version: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ecorex-v022-release-smoke-") as temp_raw:
        temp_root = Path(temp_raw)
        install_root = temp_root / "ecorex-web"
        releases = install_root / "releases"
        releases.mkdir(parents=True)

        previous_id = "20260624000000-v0.2.1"
        previous_release = releases / previous_id
        (previous_release / "runtime").mkdir(parents=True)
        (previous_release / "runtime" / "app.py").write_text("print('previous release')\n", encoding="utf-8")
        (previous_release / "release.json").write_text(
            json.dumps({
                "product": "EcoreX",
                "version": "0.2.1",
                "artifactId": "web-linux-service",
            }, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        candidate_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-v{expected_version}"
        candidate_release = releases / candidate_id
        shutil.copytree(bundle_root, candidate_release)

        pointer = CurrentPointer(install_root)
        pointer.set(previous_release, previous_id)
        if pointer.resolve() != previous_release.resolve():
            raise SmokeError("failed to establish previous current pointer")

        pointer.set(candidate_release, candidate_id)
        deployed_target = pointer.resolve()
        deployed_version = _read_release_version(deployed_target)
        if deployed_target != candidate_release.resolve() or deployed_version != expected_version:
            raise SmokeError("candidate deploy pointer verification failed")

        pointer.set(previous_release, previous_id)
        rollback_target = pointer.resolve()
        rollback_version = _read_release_version(rollback_target)
        if rollback_target != previous_release.resolve() or rollback_version != "0.2.1":
            raise SmokeError("rollback pointer verification failed")
        if not (candidate_release / "runtime" / "app.py").is_file():
            raise SmokeError("candidate release was not retained after rollback")

        return {
            "deploy": {
                "verified": True,
                "candidateReleaseId": candidate_id,
                "currentVersionAfterDeploy": deployed_version,
            },
            "rollback": {
                "verified": True,
                "rollbackReleaseId": previous_id,
                "currentVersionAfterRollback": rollback_version,
                "candidateRetainedForAudit": True,
            },
            "pointerMethod": pointer.method,
        }


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    package = Path(args.package)
    if not package.is_absolute():
        package = ROOT / package
    package = package.resolve()
    if not package.is_file():
        raise SmokeError(f"release package not found: {package}")
    if package.name != f"EcoreX_{args.expected_version}-web-linux-service.tar.gz":
        raise SmokeError(f"unexpected release package name: {package.name}")

    package_sha = _sha256_file(package)
    sha256_file = _verify_external_sha256(package)
    with tempfile.TemporaryDirectory(prefix="ecorex-v022-package-extract-") as extract_raw:
        extract_dir = Path(extract_raw)
        _safe_extract(package, extract_dir)
        bundle_root = _find_bundle_root(extract_dir)
        required_paths = _verify_required_paths(bundle_root)
        release_json = _verify_release_json(bundle_root, args.expected_version)
        checksum_summary = _verify_checksums(bundle_root, args.expected_version)
        deployment = _simulate_deploy_and_rollback(bundle_root, args.expected_version)

    return {
        "status": "PASS",
        "scope": "local-filesystem-web-linux-service",
        "productionEnvironment": False,
        "requiresRoot": False,
        "requiresSystemd": False,
        "requiresNetwork": False,
        "artifact": {
            "path": _repo_relative(package),
            "version": args.expected_version,
            "artifactId": "web-linux-service",
            "size": package.stat().st_size,
            "sha256": package_sha,
            "sha256File": sha256_file,
            "releaseJson": release_json,
        },
        "packageChecks": {
            "requiredPaths": required_paths,
            **checksum_summary,
        },
        "deploy": deployment["deploy"],
        "rollback": deployment["rollback"],
        "pointerMethod": deployment["pointerMethod"],
        "notes": [
            "Local filesystem smoke only; this does not start systemd, hit HTTP endpoints, or validate a production host.",
            "The pointer fallback is recorded when Windows cannot create a directory symlink.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default=str(DEFAULT_PACKAGE.relative_to(ROOT)))
    parser.add_argument("--expected-version", default="0.2.2")
    parser.add_argument("--artifact", default="", help="Optional JSON artifact path.")
    args = parser.parse_args(argv)

    try:
        result = run_smoke(args)
    except Exception as exc:  # pragma: no cover
        print(json.dumps({"status": "FAIL", "error": str(exc)}, indent=2, sort_keys=True))
        return 1

    if args.artifact:
        artifact = Path(args.artifact)
        if not artifact.is_absolute():
            artifact = ROOT / artifact
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
