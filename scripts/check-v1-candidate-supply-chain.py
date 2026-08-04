#!/usr/bin/env python3
"""Deterministic license, secret, SBOM and size gates for v1 Candidates."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
from importlib import metadata as importlib_metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from typing import Any
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

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
from ecorex.release.macos_native_contract import (  # noqa: E402
    MACOS_NATIVE_COMPONENTS,
    MACOS_NATIVE_LICENSES,
    MACOS_PACK_PYTHON_RUNTIME_DYLIBS,
    PYTHON_MACOS_DISTRIBUTION,
    PYTHON_MACOS_LICENSE,
)
from ecorex.release.secret_scan import detect_secret  # noqa: E402
from ecorex.product_version import stable_release_sequence  # noqa: E402
from ecorex.update import ReleaseManifest  # noqa: E402


_FORBIDDEN_LICENSE = re.compile(r"(?:^|[^A-Z])(?:AGPL|GPL|SSPL)(?:[- .0-9]|$)", re.I)
_LICENSE_OVERRIDES = {
    "fastapi": "MIT",
    "websockets": "BSD-3-Clause",
}
_INACTIVE_MARKER_LICENSES = {
    # colorama is present in the universal Runtime lock only for Windows. A
    # Linux/macOS release gate must still account for this exact reviewed lock
    # entry without treating arbitrary missing Runtime packages as licensed.
    "colorama": ("0.4.6", "BSD-3-Clause"),
}
_NODE_LICENSE_OVERRIDES = {
    # zod-to-ts 1.2.0 ships an MIT LICENSE file but omits the package.json
    # license field copied into package-lock.json. Keep the review exact.
    ("node_modules/zod-to-ts", "1.2.0"): "MIT",
}


def _stable_release_sequence(version: str) -> int:
    try:
        return stable_release_sequence(version)
    except ValueError:
        raise ValueError("candidate_bootstrap_minimum_stable_invalid")


def _verify_bootstrap_minimum_stable(archive_path: Path, version: str) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = [item for item in archive.infolist() if item.filename == "bootstrap-config.json"]
            if len(members) != 1 or members[0].file_size > 64 * 1024:
                raise ValueError
            value = json.loads(archive.read(members[0]).decode("utf-8"))
        minimum = value["minimum_stable"]
        signature = minimum["signature"]
        sequence = minimum["sequence"]
        key_id = signature["key_id"]
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence != _stable_release_sequence(version)
            or minimum["version"] != version
            or signature["algorithm"] != "ed25519"
            or not isinstance(key_id, str)
            or not key_id
        ):
            raise ValueError
        public_key = base64.b64decode(value["release_public_keys"][key_id], validate=True)
        signed = base64.b64decode(signature["value"], validate=True)
        payload = b"\0".join(
            (b"ecorex.bootstrap-minimum-stable.v1", str(sequence).encode("ascii"), version.encode("ascii"))
        )
        if len(public_key) != 32 or len(signed) != 64:
            raise ValueError
        Ed25519PublicKey.from_public_bytes(public_key).verify(signed, payload)
    except (InvalidSignature, KeyError, TypeError, ValueError, UnicodeDecodeError, zipfile.BadZipFile, json.JSONDecodeError):
        raise ValueError("candidate_bootstrap_minimum_stable_invalid") from None


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


def _license_inventory(
    repo: Path,
    runtime_versions: dict[str, str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    python: list[dict[str, str]] = []
    for requested_name in sorted(runtime_versions, key=canonicalize_name):
        canonical_name = canonicalize_name(requested_name)
        try:
            package = importlib_metadata.metadata(requested_name)
        except importlib_metadata.PackageNotFoundError:
            reviewed = _INACTIVE_MARKER_LICENSES.get(canonical_name)
            if reviewed is None or reviewed[0] != runtime_versions[canonical_name]:
                raise ValueError(f"license_package_missing:{requested_name}") from None
            python.append(
                {
                    "name": requested_name,
                    "version": runtime_versions[canonical_name],
                    "license": reviewed[1],
                }
            )
            continue
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
        version = str(value.get("version") or "")
        license_value = value.get("license") or _NODE_LICENSE_OVERRIDES.get(
            (package_path, version)
        )
        if not isinstance(license_value, str) or not license_value.strip():
            raise ValueError(f"web_license_unclassified:{package_path}")
        if _FORBIDDEN_LICENSE.search(license_value):
            raise ValueError(f"web_license_forbidden:{package_path}")
        node.append(
            {
                "name": package_path.removeprefix("node_modules/"),
                "version": version,
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
        if detect_secret(payload, path.as_posix()):
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
    dependency_lock = load_dependency_lock_manifest(
        resolved / "requirements" / "locks" / "manifest.json"
    )
    runtime_versions = _lock_versions(
        dependency_lock.path.parent / dependency_lock.profiles["runtime"]["lock"]
    )
    python, node = _license_inventory(resolved, runtime_versions)
    licensed_versions = {
        canonicalize_name(item["name"]): item["version"] for item in python
    }
    if set(licensed_versions) != set(runtime_versions):
        raise ValueError("license_runtime_lock_coverage_mismatch")
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
                "patterns": [
                    "private-key",
                    "aws-access-key",
                    "github-token",
                    "slack-token",
                ],
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
    references = [
        component.get("bom-ref")
        for component in components
        if isinstance(component, dict)
    ]
    if (
        any(not isinstance(reference, str) or not reference for reference in references)
        or len(references) != len(components)
        or len(set(references)) != len(references)
    ):
        raise ValueError("candidate_sbom_invalid")
    component_hashes: dict[str, str] = {}
    for component in components:
        if not isinstance(component, dict) or not isinstance(
            component.get("name"), str
        ):
            raise ValueError("candidate_sbom_invalid")
        hashes = component.get("hashes")
        if not isinstance(hashes, list):
            continue
        for item in hashes:
            if isinstance(item, dict) and item.get("alg") == "SHA-256":
                component_hashes[component["name"]] = str(item.get("content") or "")
    if (
        component_hashes.get("requirements/locks/manifest.json")
        != dependency_lock.sha256
    ):
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
    expected_native_references: set[str] = set()
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
        elif artifact.artifact_id.startswith(
            "capability-pack-"
        ) and not artifact.artifact_id.endswith("-manifest"):
            limit = MAX_CAPABILITY_PACK_BYTES
        else:
            limit = 16 * 1024 * 1024
        if artifact.size_bytes > limit:
            raise ValueError("candidate_artifact_size_limit")
        if path.suffix == ".zip":
            _scan_archive(path)
        if artifact.artifact_id.startswith("bootstrap-"):
            _verify_bootstrap_minimum_stable(path, manifest.version)
        if artifact.artifact_id.startswith("core-") and artifact.platform == "macos":
            expected_native_references.update(
                _verify_macos_native_sbom(path, artifact, components)
            )
        archives.append(
            {
                "artifact_id": artifact.artifact_id,
                "size_bytes": artifact.size_bytes,
                "sha256": digest,
                "limit_bytes": limit,
            }
        )
    _verify_native_reference_union(references, expected_native_references)
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


def _verify_native_reference_union(references: list[str], expected: set[str]) -> None:
    if {
        reference for reference in references if reference.startswith("native:")
    } != expected:
        raise ValueError("candidate_native_sbom_mismatch")


def _verify_macos_native_sbom(
    archive_path: Path,
    artifact: Any,
    sbom_components: list[Any],
) -> set[str]:
    member = "bin/pack-python/native-components.json"
    try:
        with zipfile.ZipFile(archive_path) as archive:
            if not any(
                item.filename.startswith("bin/pack-python/")
                for item in archive.infolist()
            ):
                return set()
            matches = [item for item in archive.infolist() if item.filename == member]
            if (
                len(matches) != 1
                or matches[0].is_dir()
                or matches[0].file_size > 64 * 1024
            ):
                raise ValueError("candidate_native_inventory_invalid")
            inventory = json.loads(
                archive.read(matches[0]).decode("utf-8"),
                object_pairs_hook=_unique_native_object,
            )
    except ValueError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile):
        raise ValueError("candidate_native_inventory_invalid") from None
    if not isinstance(inventory, dict):
        raise ValueError("candidate_native_inventory_invalid")
    native = inventory.get("components")
    notice = inventory.get("license_notice")
    license_texts = inventory.get("license_texts")
    if (
        set(inventory)
        != {
            "architecture",
            "components",
            "distribution",
            "license_notice",
            "license_texts",
            "platform",
            "schema_version",
        }
        or inventory.get("schema_version") != 1
        or inventory.get("platform") != "macos"
        or inventory.get("architecture") != artifact.architecture
        or inventory.get("distribution") != dict(PYTHON_MACOS_DISTRIBUTION)
        or not isinstance(native, list)
        or not isinstance(license_texts, list)
        or (
            bool(native)
            and notice
            != {
                "path": PYTHON_MACOS_LICENSE["path"],
                "sha256": PYTHON_MACOS_LICENSE["sha256"],
                "size_bytes": PYTHON_MACOS_LICENSE["size_bytes"],
            }
        )
        or (not native and notice is not None)
    ):
        raise ValueError("candidate_native_inventory_invalid")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            notice_path = f"bin/pack-python/{PYTHON_MACOS_LICENSE['path']}"
            notice_members = [
                item for item in archive.infolist() if item.filename == notice_path
            ]
            if bool(native) != (
                len(notice_members) == 1
                and not notice_members[0].is_dir()
                and notice_members[0].file_size == PYTHON_MACOS_LICENSE["size_bytes"]
            ):
                raise ValueError("candidate_native_inventory_invalid")
            if native:
                notice_payload = archive.read(notice_members[0])
                if hashlib.sha256(notice_payload).hexdigest() != PYTHON_MACOS_LICENSE[
                    "sha256"
                ] or any(
                    notice_payload.count(token) != 1
                    for token in PYTHON_MACOS_LICENSE["tokens"]
                ):
                    raise ValueError("candidate_native_inventory_invalid")
            archive_members = {item.filename: item for item in archive.infolist()}
            immediate_dylibs = {
                item.filename.removeprefix("bin/pack-python/")
                for item in archive.infolist()
                if item.filename.startswith("bin/pack-python/lib/")
                and "/" not in item.filename.removeprefix("bin/pack-python/lib/")
                and item.filename.endswith(".dylib")
                and not item.is_dir()
            }
            component_payloads = {
                component.get("path"): archive.read(
                    archive_members[f"bin/pack-python/{component.get('path')}"]
                )
                for component in native
                if isinstance(component, dict)
                and f"bin/pack-python/{component.get('path')}" in archive_members
            }
    except (OSError, KeyError, zipfile.BadZipFile):
        raise ValueError("candidate_native_inventory_invalid") from None
    indexed = {
        item.get("bom-ref"): item
        for item in sbom_components
        if isinstance(item, dict) and isinstance(item.get("bom-ref"), str)
    }
    seen: set[str] = set()
    expected_license_texts: set[str] = set()
    for component in native:
        expected_component_keys = {
            "license",
            "license_text",
            "name",
            "path",
            "sha256",
            "source_sha256",
            "version",
        }
        path_value = component.get("path") if isinstance(component, dict) else None
        path = PurePosixPath(path_value) if isinstance(path_value, str) else None
        if (
            not isinstance(component, dict)
            or set(component) != expected_component_keys
            or path is None
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in path_value
            or path.as_posix() != path_value
            or path_value in seen
            or path_value not in component_payloads
            or hashlib.sha256(component_payloads[path_value]).hexdigest()
            != component.get("sha256")
        ):
            raise ValueError("candidate_native_inventory_invalid")
        seen.add(path_value)
        contract = MACOS_NATIVE_COMPONENTS.get(path.name)
        if (
            contract is None
            or path != PurePosixPath("lib") / path.name
            or component["name"] != contract.name
            or component["version"] != contract.version
            or component["license"] != contract.license
            or component["license_text"]
            != MACOS_NATIVE_LICENSES[contract.license_text].archive_path
            or component["source_sha256"] != contract.source_sha256
        ):
            raise ValueError("candidate_native_inventory_invalid")
        expected_license_texts.add(contract.license_text)
        reference = f"native:macos:{artifact.architecture}:{component.get('path', '')}"
        emitted = indexed.get(reference)
        hashes = emitted.get("hashes") if isinstance(emitted, dict) else None
        licenses = emitted.get("licenses") if isinstance(emitted, dict) else None
        external = (
            emitted.get("externalReferences") if isinstance(emitted, dict) else None
        )
        property_items = (
            emitted.get("properties", []) if isinstance(emitted, dict) else []
        )
        properties = {
            item.get("name"): item.get("value")
            for item in property_items
            if isinstance(item, dict)
        }
        expected_property_names = {
            "ecorex:distribution-size-bytes",
            "ecorex:license-notice",
            "ecorex:license-notice-sha256",
            "ecorex:license-source-internal-path",
            "ecorex:license-text",
            "ecorex:license-text-sha256",
            "ecorex:native-path",
            "ecorex:packaged-in",
            "ecorex:source-sha256",
        }
        license_text = MACOS_NATIVE_LICENSES[contract.license_text]
        external_contract = {
            (
                "distribution",
                PYTHON_MACOS_DISTRIBUTION["url"],
                PYTHON_MACOS_DISTRIBUTION["sha256"],
            ),
            (
                "other",
                license_text.source_url,
                license_text.source_archive_sha256,
            ),
        }
        if (
            not isinstance(emitted, dict)
            or set(emitted)
            != {
                "bom-ref",
                "externalReferences",
                "hashes",
                "licenses",
                "name",
                "properties",
                "type",
                "version",
            }
            or emitted.get("type") != "library"
            or emitted.get("name") != component.get("name")
            or emitted.get("version") != component.get("version")
            or not isinstance(hashes, list)
            or len(hashes) != 1
            or any(
                not isinstance(item, dict) or set(item) != {"alg", "content"}
                for item in hashes
            )
            or {
                (item.get("alg"), item.get("content"))
                for item in hashes
                if isinstance(item, dict)
            }
            != {("SHA-256", component.get("sha256"))}
            or not isinstance(licenses, list)
            or len(licenses) != 1
            or not isinstance(external, list)
            or len(external) != 2
            or any(
                not isinstance(item, dict)
                or set(item) != {"hashes", "type", "url"}
                or not isinstance(item.get("hashes"), list)
                or len(item["hashes"]) != 1
                or not isinstance(item["hashes"][0], dict)
                or set(item["hashes"][0]) != {"alg", "content"}
                for item in external
            )
            or {
                (
                    item.get("type"),
                    item.get("url"),
                    next(
                        (
                            digest.get("content")
                            for digest in item.get("hashes", [])
                            if isinstance(digest, dict)
                            and digest.get("alg") == "SHA-256"
                        ),
                        None,
                    ),
                )
                for item in external
                if isinstance(item, dict)
            }
            != external_contract
            or not isinstance(property_items, list)
            or any(
                not isinstance(item, dict) or set(item) != {"name", "value"}
                for item in property_items
            )
            or len(property_items) != len(expected_property_names)
            or len(properties) != len(expected_property_names)
            or set(properties) != expected_property_names
            or properties.get("ecorex:native-path") != component["path"]
            or properties.get("ecorex:packaged-in") != artifact.artifact_id
            or properties.get("ecorex:source-sha256") != component["source_sha256"]
            or properties.get("ecorex:distribution-size-bytes")
            != str(PYTHON_MACOS_DISTRIBUTION["size_bytes"])
            or properties.get("ecorex:license-notice") != PYTHON_MACOS_LICENSE["path"]
            or properties.get("ecorex:license-notice-sha256")
            != PYTHON_MACOS_LICENSE["sha256"]
            or properties.get("ecorex:license-text") != license_text.archive_path
            or properties.get("ecorex:license-text-sha256") != license_text.sha256
            or properties.get("ecorex:license-source-internal-path")
            != license_text.source_internal_path
        ):
            raise ValueError("candidate_native_sbom_mismatch")
        if set(licenses[0]) != {"license"}:
            raise ValueError("candidate_native_sbom_mismatch")
        license_record = licenses[0].get("license")
        expected_license_record = (
            {"id": component["license"]}
            if component["license"] in {"Apache-2.0", "TCL"}
            else {"name": component["license"]}
        )
        if license_record != expected_license_record:
            raise ValueError("candidate_native_sbom_mismatch")
    expected_references = {
        f"native:macos:{artifact.architecture}:{component['path']}"
        for component in native
    }
    actual_references = {
        reference
        for reference in indexed
        if reference.startswith(f"native:macos:{artifact.architecture}:")
    }
    if actual_references != expected_references:
        raise ValueError("candidate_native_sbom_mismatch")
    allowed_runtime_dylibs = immediate_dylibs & MACOS_PACK_PYTHON_RUNTIME_DYLIBS
    if immediate_dylibs - allowed_runtime_dylibs != seen:
        raise ValueError("candidate_native_inventory_invalid")
    expected_license_inventory = [
        {
            "path": contract.archive_path,
            "provenance": contract.provenance,
            "sha256": contract.sha256,
            "size_bytes": contract.size_bytes,
            "source_archive_sha256": contract.source_archive_sha256,
            "source_internal_path": contract.source_internal_path,
            "source_url": contract.source_url,
        }
        for key, contract in sorted(MACOS_NATIVE_LICENSES.items())
        if key in expected_license_texts
    ]
    if license_texts != expected_license_inventory:
        raise ValueError("candidate_native_inventory_invalid")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for license_text in expected_license_inventory:
                member_path = f"bin/pack-python/{license_text['path']}"
                members = [
                    item for item in archive.infolist() if item.filename == member_path
                ]
                if (
                    len(members) != 1
                    or members[0].is_dir()
                    or members[0].file_size != license_text["size_bytes"]
                    or hashlib.sha256(archive.read(members[0])).hexdigest()
                    != license_text["sha256"]
                ):
                    raise ValueError("candidate_native_inventory_invalid")
    except (OSError, zipfile.BadZipFile):
        raise ValueError("candidate_native_inventory_invalid") from None
    return expected_references


def _unique_native_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("candidate_native_inventory_invalid")
        result[key] = value
    return result


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
        payload = archive.read(member)
        if detect_secret(payload, normalized):
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
        print(
            json.dumps(
                {"ok": True, "report": str(args.report.resolve())}, sort_keys=True
            )
        )
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
