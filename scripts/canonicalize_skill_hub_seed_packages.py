from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
from typing import Any, Mapping
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecorex.extensions.local_bundle import (  # noqa: E402
    MAX_LOCAL_BUNDLE_BYTES,
    MAX_LOCAL_BUNDLE_FILES,
    LocalSkillBundleStore,
)


LOCK = ROOT / "docs/v0.3.0/skill-hub/cow-skill-hub.lock.json"
SOURCES = ROOT / "docs/v0.3.0/skill-hub/source-packages"
PACKAGES = ROOT / "docs/v0.3.0/skill-hub/seed-packages"
AUDIT = ROOT / "docs/v0.3.0/skill-hub/seed-canonicalization-audit.json"
ALIASES = frozenset({"docx", "xlsx", "pptx", "pdf", "lark-cli"})
SCRIPT_SUFFIXES = frozenset({".py", ".js", ".mjs", ".sh"})
STATIC_SUFFIXES = frozenset(
    {
        ".avif", ".bmp", ".csv", ".gif", ".jpeg", ".jpg", ".json",
        ".md", ".png", ".txt", ".webp", ".xml", ".yaml", ".yml",
        ".tsv",
    }
)
TRANSFORMATION_ID = "emate-declarative-canonical-v1"


def canonicalize(
    lock_path: Path = LOCK,
    sources_root: Path = SOURCES,
    packages_root: Path = PACKAGES,
) -> dict[str, Any]:
    lock = _object(lock_path)
    upstream = lock["upstream"]
    candidates = lock["seed_candidates"]
    packages_root.mkdir(parents=True, exist_ok=True)
    decisions: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="emate-seed-canonical-cas-") as temporary:
        store = LocalSkillBundleStore(Path(temporary))
        for candidate in candidates:
            slug = str(candidate["slug"])
            version = str(candidate["version"])
            source_name = f"{slug}-{version}.zip"
            source_path = sources_root / source_name
            identity = _verified_source(candidate, source_path)
            if slug in ALIASES:
                decisions.append(
                    {
                        "slug": slug,
                        "status": "native_alias",
                        "source_package_file": source_name,
                        **identity,
                    }
                )
                continue
            try:
                canonical, facts = _canonical_zip(
                    source_path.read_bytes(),
                    candidate=candidate,
                    upstream=upstream,
                    captured_at=str(lock["captured_at"]),
                )
                bundle = store.ingest_zip(canonical)
                if bundle.metadata.version != version:
                    raise ValueError("canonical package version differs from catalog lock")
                package_name = f"{slug}-{version}.zip"
                package_path = packages_root / package_name
                _write_bytes_atomic(package_path, canonical)
                decisions.append(
                    {
                        "slug": slug,
                        "status": "mirrored",
                        "source_package_file": source_name,
                        **identity,
                        "package_file": package_name,
                        "package_size_bytes": len(canonical),
                        "package_sha256": hashlib.sha256(canonical).hexdigest(),
                        "cas_sha256": bundle.artifact_sha256,
                        "transformation_id": TRANSFORMATION_ID,
                        "facts": facts,
                    }
                )
            except CanonicalizationRefused as error:
                decisions.append(
                    {
                        "slug": slug,
                        "status": "unsupported",
                        "source_package_file": source_name,
                        **identity,
                        "reason_code": error.reason_code,
                        "detail": str(error),
                        "facts": error.facts,
                    }
                )
            except Exception as error:
                decisions.append(
                    {
                        "slug": slug,
                        "status": "unsupported",
                        "source_package_file": source_name,
                        **identity,
                        "reason_code": "canonical_contract_rejected",
                        "detail": f"{type(error).__name__}: {str(error)[:360]}",
                        "facts": {},
                    }
                )
    mirrored = sum(item["status"] == "mirrored" for item in decisions)
    return {
        "schema_version": 1,
        "upstream_commit": upstream["commit"],
        "transformation_id": TRANSFORMATION_ID,
        "candidate_count": len(decisions),
        "source_identity_verified_count": len(decisions),
        "native_alias_count": sum(item["status"] == "native_alias" for item in decisions),
        "mirrored_count": mirrored,
        "unsupported_count": sum(item["status"] == "unsupported" for item in decisions),
        "network_dependency": False,
        "user_directories_modified": False,
        "decisions": decisions,
    }


class CanonicalizationRefused(ValueError):
    def __init__(self, reason_code: str, detail: str, facts: Mapping[str, Any]):
        super().__init__(detail)
        self.reason_code = reason_code
        self.facts = dict(facts)


def _verified_source(candidate: Mapping[str, Any], source: Path) -> dict[str, Any]:
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"source mirror is missing: {source.name}")
    payload = source.read_bytes()
    size = int(candidate["source_package_size_bytes"])
    digest = str(candidate["source_package_sha256"])
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError(f"source mirror identity changed: {source.name}")
    return {"source_package_size_bytes": size, "source_package_sha256": digest}


def _canonical_zip(
    payload: bytes,
    *,
    candidate: Mapping[str, Any],
    upstream: Mapping[str, Any],
    captured_at: str,
) -> tuple[bytes, dict[str, Any]]:
    files = _source_files(payload)
    prefix, skill_path = _skill_root(files)
    normalized: dict[str, bytes] = {}
    original_paths: dict[str, str] = {}
    for source_path, content in files.items():
        path = PurePosixPath(source_path)
        if prefix:
            if not path.parts or path.parts[0] != prefix:
                raise CanonicalizationRefused(
                    "ambiguous_skill_root",
                    "source archive contains files outside its single Skill root",
                    {"outside_root": source_path},
                )
            path = PurePosixPath(*path.parts[1:])
        normalized_path = path.as_posix()
        if source_path == skill_path:
            normalized_path = "SKILL.md"
        if not normalized_path or normalized_path in normalized:
            raise CanonicalizationRefused(
                "ambiguous_skill_root",
                "source paths collide after canonical root normalization",
                {"source_path": source_path, "normalized_path": normalized_path},
            )
        normalized[normalized_path] = content
        original_paths[normalized_path] = source_path

    scripts = sorted(
        path for path in normalized if PurePosixPath(path).suffix.casefold() in SCRIPT_SUFFIXES
    )
    if scripts:
        raise CanonicalizationRefused(
            "execution_contract_unattested",
            "source contains executable scripts but no audited e-Mate runtime/effect contract",
            {"script_count": len(scripts), "scripts": scripts[:24]},
        )
    nonstatic = sorted(
        path
        for path in normalized
        if PurePosixPath(path).suffix.casefold() not in STATIC_SUFFIXES
    )
    if nonstatic:
        raise CanonicalizationRefused(
            "unsupported_resource_type",
            "source contains resources outside the declarative canonical allowlist",
            {"resource_count": len(nonstatic), "resources": nonstatic[:24]},
        )

    source_skill = normalized["SKILL.md"]
    metadata = _legacy_identity(source_skill)
    body = _frontmatter_body(source_skill)
    locked_tags = sorted(
        {
            str(tag)
            for tag in candidate.get("tags", [])
            if isinstance(tag, str) and tag
        }
    )
    frontmatter = [
        "---",
        "name: " + json.dumps(metadata["name"], ensure_ascii=False),
        "description: " + json.dumps(metadata["description"], ensure_ascii=False),
        "version: " + json.dumps(str(candidate["version"])),
        'compatibility: "*"',
    ]
    if metadata.get("license"):
        frontmatter.append("license: " + json.dumps(metadata["license"], ensure_ascii=False))
    frontmatter.append("tags: " + json.dumps(locked_tags, ensure_ascii=False, separators=(",", ":")))
    frontmatter.extend(["---", ""])
    normalized["SKILL.md"] = ("\n".join(frontmatter) + body).encode("utf-8")
    provenance = {
        "schema_version": 1,
        "brand": "e-Mate",
        "source_platform": "Cow Skill Hub",
        "source_url": f"https://skills.cowagent.ai/{candidate['slug']}",
        "source_slug": candidate["slug"],
        "source_provider": candidate["provider"],
        "source_package_size_bytes": candidate["source_package_size_bytes"],
        "source_package_sha256": candidate["source_package_sha256"],
        "catalog_version": candidate["version"],
        "catalog_captured_at": captured_at,
        "upstream_repository": upstream["repository"],
        "upstream_commit": upstream["commit"],
        "transformation_id": TRANSFORMATION_ID,
        "transformation": [
            "strip_single_archive_root",
            "canonicalize_root_skill_filename",
            "retain_instruction_body_and_declarative_resources",
            "replace_legacy_frontmatter_with_locked_product_fields",
            "attach_original_source_identity",
        ],
    }
    normalized["e-mate-provenance.json"] = json.dumps(
        provenance, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _deterministic_zip(normalized), {
        "source_file_count": len(files),
        "canonical_file_count": len(normalized),
        "source_skill_path": skill_path,
        "root_prefix_stripped": prefix,
        "renamed_paths": {
            path: source
            for path, source in original_paths.items()
            if path != source
        },
    }


def _source_files(payload: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as error:
        raise CanonicalizationRefused("canonical_contract_rejected", "source is not a ZIP", {}) from error
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if not infos or len(infos) > MAX_LOCAL_BUNDLE_FILES:
            raise CanonicalizationRefused(
                "canonical_contract_rejected",
                "source file count exceeds the e-Mate package boundary",
                {"file_count": len(infos)},
            )
        for info in infos:
            path = PurePosixPath(info.filename)
            if (
                path.is_absolute()
                or not path.parts
                or any(part in {"", ".", ".."} for part in path.parts)
                or "\\" in info.filename
                or stat.S_ISLNK(info.external_attr >> 16)
                or info.flag_bits & 0x1
                or info.filename in files
            ):
                raise CanonicalizationRefused(
                    "canonical_contract_rejected",
                    "source ZIP contains an unsafe, encrypted, linked or duplicate path",
                    {"path": info.filename},
                )
            content = archive.read(info)
            total += len(content)
            if total > MAX_LOCAL_BUNDLE_BYTES:
                raise CanonicalizationRefused(
                    "canonical_contract_rejected",
                    "source expansion exceeds the e-Mate package boundary",
                    {"expanded_bytes": total},
                )
            files[path.as_posix()] = content
    return files


def _skill_root(files: Mapping[str, bytes]) -> tuple[str, str]:
    candidates = sorted(
        path
        for path in files
        if PurePosixPath(path).name.casefold() == "skill.md"
    )
    roots = [path for path in candidates if len(PurePosixPath(path).parts) == 1]
    if len(roots) == 1:
        return "", roots[0]
    top_roots = [path for path in candidates if len(PurePosixPath(path).parts) == 2]
    if len(top_roots) == 1:
        prefix = PurePosixPath(top_roots[0]).parts[0]
        if all(PurePosixPath(path).parts[0] == prefix for path in files):
            return prefix, top_roots[0]
    raise CanonicalizationRefused(
        "ambiguous_skill_root",
        "source does not identify exactly one root Skill document",
        {"skill_documents": candidates[:24], "skill_document_count": len(candidates)},
    )


def _frontmatter_body(payload: bytes) -> str:
    text = payload.decode("utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise CanonicalizationRefused(
            "canonical_contract_rejected", "root Skill frontmatter is missing", {}
        )
    closing = next(
        (index for index, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"),
        None,
    )
    if closing is None:
        raise CanonicalizationRefused(
            "canonical_contract_rejected", "root Skill frontmatter is not closed", {}
        )
    return "".join(lines[closing + 1 :]).replace("\r\n", "\n")


def _legacy_identity(payload: bytes) -> dict[str, str]:
    """Read only bounded scalar identity fields; ignore legacy policy metadata."""

    text = payload.decode("utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise CanonicalizationRefused(
            "canonical_contract_rejected", "root Skill frontmatter is missing", {}
        )
    closing = next((index for index, line in enumerate(lines[1:], 1) if line == "---"), None)
    if closing is None:
        raise CanonicalizationRefused(
            "canonical_contract_rejected", "root Skill frontmatter is not closed", {}
        )
    values: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        if key not in {"name", "description", "license"} or key in values:
            continue
        raw = raw.strip()
        if not raw or raw[0] in "|>&*!{}[]@`" or "\t" in raw:
            raise CanonicalizationRefused(
                "unbounded_description" if key == "description" else "untrusted_metadata",
                f"source {key} is not an unambiguous bounded scalar",
                {"field": key},
            )
        if raw.startswith('"'):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as error:
                raise CanonicalizationRefused(
                    "untrusted_metadata", f"source {key} quoted scalar is invalid", {"field": key}
                ) from error
            if not isinstance(value, str):
                raise CanonicalizationRefused(
                    "untrusted_metadata", f"source {key} is not a string", {"field": key}
                )
        elif raw.startswith("'"):
            if len(raw) < 2 or not raw.endswith("'"):
                raise CanonicalizationRefused(
                    "untrusted_metadata", f"source {key} quoted scalar is invalid", {"field": key}
                )
            value = raw[1:-1].replace("''", "'")
        else:
            value = raw.split(" #", 1)[0].rstrip()
        maximum = 2048 if key == "description" else 128
        if (
            not value
            or value != value.strip()
            or len(value.encode("utf-8")) > maximum
            or any(ord(character) < 32 and character not in "\t\r\n" for character in value)
        ):
            raise CanonicalizationRefused(
                "unbounded_description" if key == "description" else "untrusted_metadata",
                f"source {key} exceeds the bounded scalar contract",
                {"field": key, "utf8_bytes": len(value.encode('utf-8'))},
            )
        values[key] = value
    if "name" not in values or "description" not in values:
        raise CanonicalizationRefused(
            "untrusted_metadata",
            "source does not provide bounded scalar name and description fields",
            {"present_fields": sorted(values)},
        )
    return values


def _deterministic_zip(files: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, files[path])
    return output.getvalue()


def _object(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_bytes_atomic(path, payload)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Canonicalize digest-locked Cow packages without inventing execution contracts"
    )
    parser.add_argument("--lock", type=Path, default=LOCK)
    parser.add_argument("--sources-root", type=Path, default=SOURCES)
    parser.add_argument("--packages-root", type=Path, default=PACKAGES)
    parser.add_argument("--audit", type=Path, default=AUDIT)
    args = parser.parse_args()
    result = canonicalize(args.lock, args.sources_root, args.packages_root)
    _write_json_atomic(args.audit, result)
    print(json.dumps({key: result[key] for key in (
        "candidate_count", "source_identity_verified_count", "native_alias_count",
        "mirrored_count", "unsupported_count",
    )}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
