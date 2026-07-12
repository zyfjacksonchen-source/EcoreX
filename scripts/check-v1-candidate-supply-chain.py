#!/usr/bin/env python3
"""Deterministic license, secret, SBOM and size gates for v1 Candidates."""

from __future__ import annotations

import argparse
import hashlib
import io
from importlib import metadata as importlib_metadata
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any
import zipfile

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.release import (  # noqa: E402
    MAX_BOOTSTRAP_BYTES,
    MAX_CAPABILITY_PACK_BYTES,
    MAX_CORE_BYTES,
)
from ecorex.release.dependency_lock import (  # noqa: E402
    load_dependency_lock_manifest,
)
from ecorex.update import ReleaseManifest  # noqa: E402


_FORBIDDEN_LICENSE = re.compile(r"(?:^|[^A-Z])(?:AGPL|GPL|SSPL)(?:[- .0-9]|$)", re.I)
_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
)
_TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".c",
        ".cpp",
        ".conf",
        ".css",
        ".env",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".md",
        ".mjs",
        ".key",
        ".pem",
        ".py",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_LICENSE_OVERRIDES = {
    "fastapi": "MIT",
    "websockets": "BSD-3-Clause",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--repo", type=Path, default=ROOT)
    preflight.add_argument("--report", required=True, type=Path)
    release = commands.add_parser("release")
    release.add_argument("--release-dir", required=True, type=Path)
    release.add_argument("--dependency-lock-manifest", required=True, type=Path)
    release.add_argument("--report", required=True, type=Path)
    return parser


def _license_inventory(repo: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    runtime_names = {
        "cryptography",
        "fastapi",
        "httpx",
        "pydantic",
        "uvicorn",
        "websockets",
    }
    python: list[dict[str, str]] = []
    pending = list(sorted(runtime_names))
    visited: set[str] = set()
    while pending:
        requested_name = pending.pop(0)
        canonical_name = canonicalize_name(requested_name)
        if canonical_name in visited:
            continue
        try:
            package = importlib_metadata.metadata(requested_name)
        except importlib_metadata.PackageNotFoundError:
            raise ValueError(f"license_package_missing:{requested_name}") from None
        visited.add(canonical_name)
        name = str(package.get("Name") or requested_name)
        classifiers = package.get_all("Classifier") or []
        classifier_license = next(
            (
                value.removeprefix("License :: OSI Approved :: ")
                for value in classifiers
                if value.startswith("License :: OSI Approved :: ")
            ),
            None,
        )
        license_value = (
            package.get("License-Expression")
            or classifier_license
            or package.get("License")
            or _LICENSE_OVERRIDES.get(canonical_name)
        )
        if (
            not isinstance(license_value, str)
            or not license_value.strip()
            or license_value.strip().casefold() in {"unknown", "n/a"}
        ):
            raise ValueError(f"license_unclassified:{name}")
        if _FORBIDDEN_LICENSE.search(license_value):
            raise ValueError(f"license_forbidden:{name}")
        python.append(
            {
                "name": name,
                "version": importlib_metadata.version(name),
                "license": license_value.strip(),
            }
        )
        for raw_requirement in package.get_all("Requires-Dist") or []:
            try:
                requirement = Requirement(raw_requirement)
                if requirement.marker is not None and not requirement.marker.evaluate(
                    {"extra": ""}
                ):
                    continue
            except (InvalidRequirement, ValueError):
                raise ValueError(f"license_requirement_invalid:{name}") from None
            dependency = canonicalize_name(requirement.name)
            if dependency not in visited and dependency not in {
                canonicalize_name(item) for item in pending
            }:
                pending.append(requirement.name)
        pending.sort(key=canonicalize_name)
    python.sort(key=lambda item: canonicalize_name(item["name"]))
    lock_path = repo / "desktop" / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if not isinstance(packages, dict) or not packages:
        raise ValueError("web_license_inventory_invalid")
    node: list[dict[str, str]] = []
    for package_path, value in sorted(packages.items()):
        if not package_path or not isinstance(value, dict) or value.get("link") is True:
            continue
        license_value = value.get("license")
        if not isinstance(license_value, str) or not license_value.strip():
            raise ValueError(f"web_license_unclassified:{package_path}")
        if _FORBIDDEN_LICENSE.search(license_value):
            raise ValueError(f"web_license_forbidden:{package_path}")
        node.append(
            {
                "name": package_path.removeprefix("node_modules/"),
                "version": str(value.get("version") or ""),
                "license": license_value.strip(),
            }
        )
    return python, node


def _production_files(repo: Path) -> tuple[Path, ...]:
    command = [
        "git",
        "-C",
        str(repo),
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        raise ValueError("secret_scan_inventory_failed")
    selected: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            relative = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("secret_scan_path_invalid") from None
        normalized = relative.replace("\\", "/")
        include = (
            normalized == "pyproject.toml"
            or normalized.startswith("requirements/locks/")
            or normalized.startswith("ecorex/")
            or normalized.startswith("desktop/src/")
            or normalized in {"desktop/package.json", "desktop/package-lock.json"}
            or normalized.startswith(".github/workflows/ecorex-v1")
            or normalized.startswith("platform-staging/")
            or normalized.startswith("release/capability-packs/")
            or (
                normalized.startswith("scripts/")
                and "v1" in Path(normalized).name.casefold()
            )
        )
        candidate = repo / Path(*normalized.split("/"))
        # ``git ls-files --cached`` also reports paths deleted in a dirty
        # developer checkout. A clean Candidate checkout cannot have this
        # state; local preflight scans the files that actually exist.
        if include and os.path.lexists(candidate):
            selected.append(candidate)
    if not selected:
        raise ValueError("secret_scan_inventory_empty")
    return tuple(selected)


def _scan_secret_files(files: tuple[Path, ...]) -> tuple[int, str]:
    inventory: list[dict[str, Any]] = []
    for path in sorted(files, key=lambda item: item.as_posix().casefold()):
        metadata = path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
            or not stat.S_ISREG(metadata.st_mode)
        ):
            raise ValueError("secret_scan_non_regular_file")
        if metadata.st_size > 4 * 1024 * 1024:
            continue
        payload = path.read_bytes()
        if any(pattern.search(payload) for pattern in _SECRET_PATTERNS):
            raise ValueError(f"secret_scan_match:{path.name}")
        inventory.append(
            {
                "path_sha256": hashlib.sha256(str(path).encode()).hexdigest(),
                "content_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    digest = hashlib.sha256(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return len(inventory), digest


def _preflight(repo: Path) -> dict[str, Any]:
    resolved = repo.resolve(strict=True)
    python, node = _license_inventory(resolved)
    dependency_lock = load_dependency_lock_manifest(
        resolved / "requirements" / "locks" / "manifest.json"
    )
    runtime_versions = _lock_versions(
        dependency_lock.path.parent / dependency_lock.profiles["runtime"]["lock"]
    )
    for item in python:
        name = canonicalize_name(item["name"])
        if runtime_versions.get(name) != item["version"]:
            raise ValueError(f"installed_dependency_lock_mismatch:{name}")
    secret_count, secret_digest = _scan_secret_files(_production_files(resolved))
    return {
        "schema_version": 1,
        "status": "passed",
        "gates": {
            "license": {
                "status": "passed",
                "python_packages": python,
                "node_packages": node,
            },
            "secret-scan": {
                "status": "passed",
                "file_count": secret_count,
                "inventory_sha256": secret_digest,
                "patterns": ["private-key", "aws-access-key", "github-token", "slack-token"],
            },
            "dependency-lock": {
                "status": "passed",
                "manifest_sha256": dependency_lock.sha256,
                "runtime_packages": len(runtime_versions),
            },
        },
    }


def _release(release_dir: Path, dependency_lock_path: Path) -> dict[str, Any]:
    root = release_dir.resolve(strict=True)
    dependency_lock = load_dependency_lock_manifest(dependency_lock_path)
    manifest_path = root / "release-manifest.json"
    manifest = ReleaseManifest.from_json(manifest_path.read_text(encoding="utf-8"))
    sbom_path = root / "sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    if (
        not isinstance(sbom, dict)
        or sbom.get("bomFormat") != "CycloneDX"
        or sbom.get("specVersion") != "1.5"
        or not isinstance(sbom.get("components"), list)
    ):
        raise ValueError("candidate_sbom_invalid")
    components = sbom["components"]
    component_hashes: dict[str, str] = {}
    for component in components:
        if not isinstance(component, dict) or not isinstance(component.get("name"), str):
            raise ValueError("candidate_sbom_invalid")
        hashes = component.get("hashes")
        if not isinstance(hashes, list):
            continue
        for item in hashes:
            if isinstance(item, dict) and item.get("alg") == "SHA-256":
                component_hashes[component["name"]] = str(item.get("content") or "")
    if component_hashes.get("requirements/locks/manifest.json") != dependency_lock.sha256:
        raise ValueError("candidate_dependency_lock_sbom_mismatch")
    metadata = json.loads((root / "release-metadata.json").read_text(encoding="utf-8"))
    if metadata.get("python_dependency_lock_sha256") != dependency_lock.sha256:
        raise ValueError("candidate_dependency_lock_metadata_mismatch")
    sbom_properties = {
        item.get("name"): item.get("value")
        for item in sbom.get("metadata", {}).get("properties", [])
        if isinstance(item, dict)
    }
    if (
        sbom_properties.get("ecorex:python-dependency-lock-sha256")
        != dependency_lock.sha256
    ):
        raise ValueError("candidate_dependency_lock_sbom_mismatch")
    archives: list[dict[str, Any]] = []
    for artifact in manifest.artifacts:
        path = root / artifact.file_name
        if not path.is_file() or path.stat().st_size != artifact.size_bytes:
            raise ValueError("candidate_artifact_missing")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != artifact.sha256 or component_hashes.get(path.name) != digest:
            raise ValueError("candidate_sbom_digest_mismatch")
        if artifact.artifact_id.startswith("core-"):
            limit = MAX_CORE_BYTES
        elif artifact.artifact_id.startswith("bootstrap-"):
            limit = MAX_BOOTSTRAP_BYTES
        elif artifact.artifact_id.startswith("capability-pack-") and not artifact.artifact_id.endswith("-manifest"):
            limit = MAX_CAPABILITY_PACK_BYTES
        else:
            limit = 16 * 1024 * 1024
        if artifact.size_bytes > limit:
            raise ValueError("candidate_artifact_size_limit")
        if path.suffix == ".zip":
            _scan_archive(path)
        archives.append(
            {
                "artifact_id": artifact.artifact_id,
                "size_bytes": artifact.size_bytes,
                "sha256": digest,
                "limit_bytes": limit,
            }
        )
    return {
        "schema_version": 1,
        "status": "passed",
        "release_id": manifest.release_id,
        "gates": {
            "sbom": {
                "status": "passed",
                "sha256": hashlib.sha256(sbom_path.read_bytes()).hexdigest(),
                "component_count": len(components),
                "python_dependency_lock_sha256": dependency_lock.sha256,
            },
            "size-scan": {"status": "passed", "artifacts": archives},
            "secret-scan": {"status": "passed", "archives_scanned": len(archives)},
        },
    }


def _lock_versions(path: Path) -> dict[str, str]:
    entries: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or (stripped.startswith("#") and not pending):
            continue
        continued = stripped.endswith("\\")
        if continued:
            stripped = stripped[:-1].strip()
        pending = f"{pending} {stripped}".strip()
        if not continued:
            entries.append(pending)
            pending = ""
    if pending or not entries:
        raise ValueError("dependency_lock_syntax_invalid")
    versions: dict[str, str] = {}
    for entry in entries:
        try:
            requirement = Requirement(entry.split(" --hash=", 1)[0].strip())
        except InvalidRequirement:
            raise ValueError("dependency_lock_syntax_invalid") from None
        specifiers = tuple(requirement.specifier)
        if len(specifiers) != 1 or specifiers[0].operator != "==":
            raise ValueError("dependency_lock_not_exact")
        versions[canonicalize_name(requirement.name)] = specifiers[0].version
    return versions


def _scan_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            _scan_zip_members(archive, depth=0)
    except zipfile.BadZipFile:
        raise ValueError("candidate_archive_invalid") from None


def _scan_zip_members(archive: zipfile.ZipFile, *, depth: int) -> None:
    members = archive.infolist()
    if len(members) > 50_000:
        raise ValueError("candidate_archive_member_limit")
    total = 0
    seen: set[str] = set()
    for member in members:
        normalized = member.filename.replace("\\", "/")
        path = Path(normalized)
        collision = normalized.casefold().rstrip("/")
        total += member.file_size
        if (
            not normalized
            or normalized.startswith("/")
            or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
            or collision in seen
            or member.flag_bits & 0x1
            or total > 2 * 1024 * 1024 * 1024
            or (
                member.file_size > 1024 * 1024
                and member.file_size > max(1, member.compress_size) * 250
            )
        ):
            raise ValueError("candidate_archive_invalid")
        seen.add(collision)
        if member.is_dir():
            continue
        suffix = path.suffix.casefold()
        name = path.name
        if suffix == ".zip" and depth == 0:
            if not 1 <= member.file_size <= 512 * 1024 * 1024:
                raise ValueError("candidate_nested_archive_invalid")
            try:
                with zipfile.ZipFile(io.BytesIO(archive.read(member))) as nested:
                    _scan_zip_members(nested, depth=1)
            except zipfile.BadZipFile:
                raise ValueError("candidate_nested_archive_invalid") from None
            continue
        if member.file_size > 4 * 1024 * 1024:
            continue
        if suffix not in _TEXT_SUFFIXES and name not in {
            "runtime-config.json",
            "storage-migrations.json",
            "pack-python.json",
            "ecorex-pack.json",
            "ecorex-image-pack.json",
            "browser-runtime.json",
        }:
            continue
        payload = archive.read(member)
        if any(pattern.search(payload) for pattern in _SECRET_PATTERNS):
            raise ValueError("candidate_archive_secret_match")


def _write_report(path: Path, value: dict[str, Any]) -> None:
    destination = path.resolve()
    if os.path.lexists(destination):
        raise ValueError("supply_chain_report_exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = (
            _preflight(args.repo)
            if args.command == "preflight"
            else _release(args.release_dir, args.dependency_lock_manifest)
        )
        _write_report(args.report, report)
        print(json.dumps({"ok": True, "report": str(args.report.resolve())}, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc) or type(exc).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
