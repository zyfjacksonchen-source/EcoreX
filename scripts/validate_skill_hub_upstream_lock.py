from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.extensions.local_bundle import (  # noqa: E402
    MAX_LOCAL_BUNDLE_BYTES,
    LocalSkillBundleStore,
)
from ecorex.extensions.models import parse_semver  # noqa: E402
from ecorex.extensions.skill_migration import (  # noqa: E402
    EXCLUDED_SKILL_SLUGS,
    SKILL_ALIASES,
)


LOCK = ROOT / "docs/v0.3.0/skill-hub/cow-skill-hub.lock.json"
NOTICE = ROOT / "docs/v0.3.0/skill-hub/NOTICE.md"
PACKAGES = ROOT / "docs/v0.3.0/skill-hub/seed-packages"
SOURCE_PACKAGES = ROOT / "docs/v0.3.0/skill-hub/source-packages"
EVIDENCE = ROOT / "docs/v0.3.0/skill-hub/seed-package-gate.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,95}$")
_SAFE_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,191}\.zip$")
_CANDIDATE_KEYS = frozenset(
    {
        "slug",
        "version",
        "category",
        "tags",
        "provider",
        "source_package_size_bytes",
        "source_package_sha256",
        "source_package_file",
        "resolution",
    }
)
_UNSUPPORTED_REASONS = frozenset(
    {
        "noncanonical_layout",
        "untrusted_metadata",
        "unsupported_resource_type",
        "executable_resource_not_declared",
        "undeclared_script_files",
        "missing_runtime_manifest",
        "unbounded_description",
        "source_version_unattested",
        "execution_contract_unattested",
        "ambiguous_skill_root",
        "canonical_contract_rejected",
    }
)


class SeedLockError(ValueError):
    pass


def validate_seed_lock(
    lock_path: Path = LOCK,
    notice_path: Path = NOTICE,
    packages_root: Path = PACKAGES,
    source_packages_root: Path = SOURCE_PACKAGES,
) -> dict[str, Any]:
    """Verify package bytes into a temporary CAS without touching user state."""

    data = _load(lock_path)
    notice = notice_path.read_text(encoding="utf-8")
    upstream = data.get("upstream")
    snapshot = data.get("catalog_snapshot")
    candidates = data.get("seed_candidates")
    excluded = data.get("excluded_slugs")
    if (
        set(data) != {
            "schema_version", "captured_at", "upstream", "catalog_snapshot",
            "excluded_slugs", "seed_candidates",
        }
        or data.get("schema_version") != 2
        or not isinstance(upstream, Mapping)
        or not isinstance(snapshot, Mapping)
        or not isinstance(candidates, list)
        or len(candidates) != 53
        or not isinstance(excluded, list)
        or set(excluded) != set(EXCLUDED_SKILL_SLUGS)
        or len(excluded) != len(EXCLUDED_SKILL_SLUGS)
    ):
        raise SeedLockError("Skill Hub metadata lock is invalid")
    commit = upstream.get("commit")
    if (
        not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
        or not _digest(upstream.get("archive_sha256"))
        or not _digest(snapshot.get("html_sha256"))
        or commit not in notice
        or "Copyright (c) 2026 zhayujie" not in notice
        or "MIT License" not in notice
    ):
        raise SeedLockError("Skill Hub upstream identity or NOTICE is invalid")

    slugs: set[str] = set()
    aliases: list[dict[str, str]] = []
    unsupported: list[dict[str, str]] = []
    verified: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="emate-skill-seed-cas-") as temporary:
        store = LocalSkillBundleStore(Path(temporary))
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                raise SeedLockError("Skill seed entry is invalid")
            keys = frozenset(candidate)
            if keys != _CANDIDATE_KEYS:
                raise SeedLockError("Skill seed entry fields are invalid")
            slug = candidate.get("slug")
            version = candidate.get("version")
            category = candidate.get("category")
            tags = candidate.get("tags")
            provider = candidate.get("provider")
            source_size = candidate.get("source_package_size_bytes")
            source_sha = candidate.get("source_package_sha256")
            source_name = candidate.get("source_package_file")
            resolution = candidate.get("resolution")
            if (
                not isinstance(slug, str)
                or not isinstance(version, str)
                or _SLUG.fullmatch(slug) is None
                or slug in slugs
                or slug in EXCLUDED_SKILL_SLUGS
                or not isinstance(category, str)
                or not isinstance(tags, list)
                or not all(isinstance(tag, str) for tag in tags)
                or not isinstance(provider, str)
                or not provider
                or isinstance(source_size, bool)
                or not isinstance(source_size, int)
                or not 1 <= source_size <= MAX_LOCAL_BUNDLE_BYTES
                or not _digest(source_sha)
                or not isinstance(source_name, str)
                or _SAFE_PACKAGE.fullmatch(source_name) is None
                or not isinstance(resolution, Mapping)
                or not (
                    category == "external" or "office" in tags or "content" in tags
                )
            ):
                raise SeedLockError("Skill seed slug/version set is invalid")
            parse_semver(version)
            slugs.add(slug)
            source_package = source_packages_root / source_name
            if not source_package.is_file() or source_package.is_symlink():
                raise SeedLockError(f"Skill seed source package is missing: {slug}")
            source_before = source_package.stat()
            source_payload = source_package.read_bytes()
            source_after = source_package.stat()
            if (
                source_before.st_size != source_after.st_size
                or source_before.st_mtime_ns != source_after.st_mtime_ns
                or len(source_payload) != source_size
                or hashlib.sha256(source_payload).hexdigest() != source_sha
            ):
                raise SeedLockError(f"Skill seed source package changed: {slug}")
            kind = resolution.get("kind")
            if kind == "native_alias":
                canonical = SKILL_ALIASES.get(slug)
                if (
                    set(resolution)
                    != {"kind", "native_extension_id", "reason_code"}
                    or canonical is None
                    or resolution.get("native_extension_id") != f"skill.{canonical}"
                    or resolution.get("reason_code") != "native_capability_alias"
                ):
                    raise SeedLockError(f"Skill seed native alias is invalid: {slug}")
                aliases.append(
                    {
                        "slug": slug,
                        "version": version,
                        "native_extension_id": str(resolution["native_extension_id"]),
                        "source_package_sha256": str(source_sha),
                    }
                )
                continue
            if kind == "unsupported":
                reason = resolution.get("reason_code")
                detail = resolution.get("detail")
                if (
                    set(resolution) != {"kind", "reason_code", "detail"}
                    or reason not in _UNSUPPORTED_REASONS
                    or not isinstance(detail, str)
                    or not 1 <= len(detail) <= 512
                ):
                    raise SeedLockError(f"Skill seed refusal is invalid: {slug}")
                unsupported.append(
                    {
                        "slug": slug,
                        "version": version,
                        "reason_code": str(reason),
                        "source_package_sha256": str(source_sha),
                    }
                )
                continue
            if kind != "mirrored" or set(resolution) != {
                "kind",
                "package_file",
                "package_size_bytes",
                "package_sha256",
                "cas_sha256",
                "transformation_id",
            }:
                raise SeedLockError(f"Skill seed resolution is invalid: {slug}")
            package_name = resolution.get("package_file")
            package_size = resolution.get("package_size_bytes")
            package_sha = resolution.get("package_sha256")
            cas_sha = resolution.get("cas_sha256")
            transformation_id = resolution.get("transformation_id")
            if (
                not isinstance(package_name, str)
                or _SAFE_PACKAGE.fullmatch(package_name) is None
                or isinstance(package_size, bool)
                or not isinstance(package_size, int)
                or not 1 <= package_size <= MAX_LOCAL_BUNDLE_BYTES
                or not _digest(package_sha)
                or not _digest(cas_sha)
                or transformation_id != "emate-declarative-canonical-v1"
            ):
                raise SeedLockError(f"Skill seed package lock is invalid: {slug}")
            package = packages_root / package_name
            if not package.is_file() or package.is_symlink():
                raise SeedLockError(f"Skill seed package is missing: {slug}")
            before = package.stat()
            if before.st_size != package_size:
                raise SeedLockError(f"Skill seed package changed: {slug}")
            payload = package.read_bytes()
            after = package.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or len(payload) != package_size
                or hashlib.sha256(payload).hexdigest() != package_sha
            ):
                raise SeedLockError(f"Skill seed package changed: {slug}")
            bundle = store.ingest_zip(payload)
            if bundle.artifact_sha256 != cas_sha or bundle.metadata.version != version:
                raise SeedLockError(f"Skill seed CAS identity changed: {slug}")
            try:
                provenance = json.loads(
                    store.read_verified_file(
                        bundle.artifact_sha256, "e-mate-provenance.json"
                    ).decode("utf-8"),
                    object_pairs_hook=_unique_object,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError):
                raise SeedLockError(
                    f"Skill seed provenance is invalid: {slug}"
                ) from None
            if (
                not isinstance(provenance, Mapping)
                or provenance.get("schema_version") != 1
                or provenance.get("brand") != "e-Mate"
                or provenance.get("source_slug") != slug
                or provenance.get("source_provider") != provider
                or provenance.get("source_package_size_bytes") != source_size
                or provenance.get("source_package_sha256") != source_sha
                or provenance.get("catalog_version") != version
                or provenance.get("upstream_commit") != commit
                or provenance.get("transformation_id") != transformation_id
            ):
                raise SeedLockError(f"Skill seed provenance changed: {slug}")
            verified.append(
                {
                    "slug": slug,
                    "version": version,
                    "package_sha256": package_sha,
                    "cas_sha256": cas_sha,
                }
            )

    ready = len(aliases) + len(unsupported) + len(verified) == len(candidates)
    if (
        snapshot.get("seed_resolution_locked") is not True
        or snapshot.get("network_dependency") is not False
        or snapshot.get("native_alias_count") != len(aliases)
        or snapshot.get("mirrored_package_count") != len(verified)
        or snapshot.get("unsupported_count") != len(unsupported)
        or not isinstance(snapshot.get("resolution_note"), str)
        or not snapshot.get("resolution_note")
        or not ready
    ):
        raise SeedLockError("seed resolution summary disagrees with verified facts")
    return {
        "schema_version": 1,
        "status": "ready",
        "release_gate": "pass",
        "upstream_commit": commit,
        "candidate_count": len(candidates),
        "native_alias_count": len(aliases),
        "unsupported_count": len(unsupported),
        "verified_count": len(verified),
        "pending_count": 0,
        "excluded_slugs": sorted(excluded),
        "native_aliases": aliases,
        "unsupported": unsupported,
        "verified": verified,
        "network_sync": "disabled",
        "user_directories_modified": False,
    }


def _load(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SeedLockError("Skill Hub metadata lock is unreadable") from None
    if not isinstance(value, Mapping):
        raise SeedLockError("Skill Hub metadata lock must be an object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SeedLockError("Skill Hub metadata lock contains duplicate keys")
        value[key] = item
    return value


def _digest(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _write_atomic(path: Path, value: Mapping[str, Any]) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed e-Mate Skill seed gate")
    parser.add_argument("--lock", type=Path, default=LOCK)
    parser.add_argument("--notice", type=Path, default=NOTICE)
    parser.add_argument("--packages-root", type=Path, default=PACKAGES)
    parser.add_argument("--source-packages-root", type=Path, default=SOURCE_PACKAGES)
    parser.add_argument("--evidence", type=Path, default=EVIDENCE)
    args = parser.parse_args()
    try:
        evidence = validate_seed_lock(
            args.lock,
            args.notice,
            args.packages_root,
            args.source_packages_root,
        )
    except Exception as error:
        evidence = {
            "schema_version": 1,
            "status": "blocked",
            "release_gate": "fail_closed",
            "error": str(error) or type(error).__name__,
            "network_sync": "disabled",
            "user_directories_modified": False,
        }
    _write_atomic(args.evidence, evidence)
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0 if evidence.get("status") == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
