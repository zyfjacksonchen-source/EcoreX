from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "docs/v0.3.0/skill-hub/cow-skill-hub.lock.json"
AUDIT = ROOT / "docs/v0.3.0/skill-hub/seed-public-source-audit.json"
CANONICAL_AUDIT = ROOT / "docs/v0.3.0/skill-hub/seed-canonicalization-audit.json"

ALIASES = {
    "docx": "skill.office-documents",
    "xlsx": "skill.office-spreadsheets",
    "pptx": "skill.office-presentations",
    "pdf": "skill.office-pdf",
    "lark-cli": "skill.feishu-lark",
}
def build_seed_lock(
    lock_path: Path = LOCK,
    audit_path: Path = AUDIT,
    canonical_audit_path: Path = CANONICAL_AUDIT,
) -> dict[str, Any]:
    """Freeze observed Cow bytes into installable aliases or explicit refusals."""

    base = _object(lock_path)
    audit = _object(audit_path)
    canonical_audit = _object(canonical_audit_path)
    audited = {
        item["slug"]: item
        for item in audit.get("candidates", [])
        if isinstance(item, Mapping) and isinstance(item.get("slug"), str)
    }
    canonical = {
        item["slug"]: item
        for item in canonical_audit.get("decisions", [])
        if isinstance(item, Mapping) and isinstance(item.get("slug"), str)
    }
    candidates = base.get("seed_candidates")
    if (
        not isinstance(candidates, list)
        or len(candidates) != 53
        or len(audited) != 53
        or len(canonical) != 53
        or canonical_audit.get("upstream_commit") != base["upstream"]["commit"]
    ):
        raise ValueError("seed source inventory must contain exactly 53 candidates")
    resolved: list[dict[str, Any]] = []
    for raw in candidates:
        if not isinstance(raw, Mapping):
            raise ValueError("seed source entry is invalid")
        slug = raw.get("slug")
        source = audited.get(slug)
        decision = canonical.get(slug)
        if (
            source is None
            or decision is None
            or source.get("expected_version") != raw.get("version")
            or decision.get("source_package_sha256") != source.get("package_sha256")
            or decision.get("source_package_size_bytes") != source.get("package_size_bytes")
        ):
            raise ValueError(f"seed audit identity mismatch: {slug}")
        entry = {
            key: raw[key]
            for key in ("slug", "version", "category", "tags", "provider")
        }
        entry["source_package_size_bytes"] = source["package_size_bytes"]
        entry["source_package_sha256"] = source["package_sha256"]
        entry["source_package_file"] = decision["source_package_file"]
        if slug in ALIASES:
            entry["resolution"] = {
                "kind": "native_alias",
                "native_extension_id": ALIASES[slug],
                "reason_code": "native_capability_alias",
            }
        elif decision.get("status") == "mirrored":
            entry["resolution"] = {
                "kind": "mirrored",
                "package_file": decision["package_file"],
                "package_size_bytes": decision["package_size_bytes"],
                "package_sha256": decision["package_sha256"],
                "cas_sha256": decision["cas_sha256"],
                "transformation_id": decision["transformation_id"],
            }
        else:
            detail = decision.get("detail")
            code = decision.get("reason_code")
            if (
                decision.get("status") != "unsupported"
                or not isinstance(detail, str)
                or not isinstance(code, str)
            ):
                raise ValueError(f"seed refusal reason is not classified: {slug}")
            entry["resolution"] = {
                "kind": "unsupported",
                "reason_code": code,
                "detail": detail,
            }
        resolved.append(entry)

    snapshot = dict(base["catalog_snapshot"])
    snapshot.pop("seed_packages_locked", None)
    snapshot.pop("seed_packages_note", None)
    snapshot.update(
        {
            "seed_resolution_locked": True,
            "network_dependency": False,
            "native_alias_count": len(ALIASES),
            "mirrored_package_count": sum(
                item["resolution"]["kind"] == "mirrored" for item in resolved
            ),
            "unsupported_count": sum(
                item["resolution"]["kind"] == "unsupported" for item in resolved
            ),
            "resolution_note": (
                "Every selected item is fixed to an observed source digest. Native aliases "
                "bind existing e-Mate abilities; digest-locked source mirrors are transformed "
                "offline into declarative canonical packages; packages requiring guessed "
                "execution contracts remain unsupported. No Cow service is consulted after "
                "this snapshot."
            ),
        }
    )
    return {
        "schema_version": 2,
        "captured_at": base["captured_at"],
        "upstream": base["upstream"],
        "catalog_snapshot": snapshot,
        "excluded_slugs": base["excluded_slugs"],
        "seed_candidates": resolved,
    }


def _object(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _write_atomic(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
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
    parser = argparse.ArgumentParser(description="Build the fixed e-Mate Cow seed lock")
    parser.add_argument("--lock", type=Path, default=LOCK)
    parser.add_argument("--audit", type=Path, default=AUDIT)
    parser.add_argument("--canonical-audit", type=Path, default=CANONICAL_AUDIT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = build_seed_lock(args.lock, args.audit, args.canonical_audit)
    _write_atomic(args.output or args.lock, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
