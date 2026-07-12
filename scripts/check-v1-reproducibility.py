#!/usr/bin/env python3
"""Verify the v1 checkout and Web bundles have platform-stable bytes.

This is a build-integrity gate, not release or signing evidence. It verifies
that identity-bearing JSON is canonical, shell sources survive a Windows
checkout with LF bytes, and JavaScript/CSS names contain their actual SHA-256
prefix. CI can write one platform-neutral manifest per runner and compare all
of them byte-for-byte.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SITE = ROOT / "deploy" / "ecorex-site"
PUBLIC_POINTER = PUBLIC_SITE / "public-bootstrap-index.json"
CONTRACT_TYPE = "ecorex.v1-byte-contract"
REQUIRED_ATTRIBUTES = (
    "*.sh text eol=lf",
    "*.bash text eol=lf",
    "*.css text eol=lf",
    "*.html text eol=lf",
    "*.go text eol=lf",
    "*.in text eol=lf",
    "*.js text eol=lf",
    "*.json text eol=lf",
    "*.lock text eol=lf",
    "*.mjs text eol=lf",
    "*.py text eol=lf",
    "*.toml text eol=lf",
    "*.yaml text eol=lf",
    "*.yml text eol=lf",
)
V1_SHELL_FILES = (
    "run.sh",
    "scripts/start.sh",
)
V1_DEPENDENCY_LOCK_FILES = (
    "pyproject.toml",
    "requirements/locks/bootstrap.in",
    "requirements/locks/bootstrap.lock",
    "requirements/locks/cloud.in",
    "requirements/locks/cloud.lock",
    "requirements/locks/dev.in",
    "requirements/locks/dev.lock",
    "requirements/locks/manifest.json",
    "requirements/locks/platform-stage.in",
    "requirements/locks/platform-stage.lock",
    "requirements/locks/runtime.in",
    "requirements/locks/runtime.lock",
)
ATTRIBUTE_PROBES = (
    ("byte-contract-probe.sh", "text", "set"),
    ("byte-contract-probe.sh", "eol", "lf"),
    ("byte-contract-probe.bash", "text", "set"),
    ("byte-contract-probe.bash", "eol", "lf"),
    ("byte-contract-probe.js", "text", "set"),
    ("byte-contract-probe.js", "eol", "lf"),
    ("byte-contract-probe.css", "text", "set"),
    ("byte-contract-probe.css", "eol", "lf"),
    ("byte-contract-probe.html", "text", "set"),
    ("byte-contract-probe.html", "eol", "lf"),
    ("byte-contract-probe.go", "text", "set"),
    ("byte-contract-probe.go", "eol", "lf"),
    ("byte-contract-probe.json", "text", "set"),
    ("byte-contract-probe.json", "eol", "lf"),
)


class ReproducibilityError(ValueError):
    """The checkout cannot produce one stable v1 byte identity."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the only accepted JSON document encoding for byte identities."""

    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise ReproducibilityError("value cannot be encoded as canonical JSON") from error


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix()
    except (OSError, ValueError) as error:
        raise ReproducibilityError(f"path escapes the repository: {path}") from error


def _require_lf_text(path: Path, payload: bytes) -> None:
    if not payload or b"\x00" in payload:
        raise ReproducibilityError(f"text input is empty or binary: {path}")
    if b"\r" in payload:
        raise ReproducibilityError(f"text input contains CR/CRLF bytes: {path}")
    if not payload.endswith(b"\n"):
        raise ReproducibilityError(f"text input must end with one LF-compatible line: {path}")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReproducibilityError(f"text input is not UTF-8: {path}") from error


def _check_attributes(root: Path) -> None:
    path = root / ".gitattributes"
    try:
        lines = {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    except (OSError, UnicodeError) as error:
        raise ReproducibilityError(".gitattributes is missing or invalid") from error
    missing = [value for value in REQUIRED_ATTRIBUTES if value not in lines]
    if missing:
        raise ReproducibilityError(
            ".gitattributes lacks byte-stability rules: " + ", ".join(missing)
        )
    probes = tuple(dict.fromkeys(value[0] for value in ATTRIBUTE_PROBES))
    try:
        result = subprocess.run(
            ["git", "check-attr", "text", "eol", "--", *probes],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError, UnicodeError) as error:
        raise ReproducibilityError("git cannot resolve effective byte attributes") from error
    observed: dict[tuple[str, str], str] = {}
    for line in result.stdout.splitlines():
        parts = line.split(": ", 2)
        if len(parts) == 3:
            observed[(parts[0], parts[1])] = parts[2]
    ineffective = [
        f"{path}:{attribute}={observed.get((path, attribute), 'missing')}"
        for path, attribute, expected in ATTRIBUTE_PROBES
        if observed.get((path, attribute)) != expected
    ]
    if ineffective:
        raise ReproducibilityError(
            "effective Git attributes are not byte stable: " + ", ".join(ineffective)
        )


def _v1_shell_sources(root: Path) -> tuple[Path, ...]:
    paths = tuple(root / relative for relative in V1_SHELL_FILES)
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise ReproducibilityError(
            "v1 shell source is missing: " + ", ".join(str(path) for path in missing)
        )
    return paths


def _file_record(root: Path, path: Path, kind: str) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "kind": kind,
        "path": _relative(root, path),
        "sha256": sha256_bytes(payload),
        "size_bytes": len(payload),
    }


def _validate_canonical_json(root: Path, path: Path) -> dict[str, object]:
    try:
        payload = path.read_bytes()
        parsed = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReproducibilityError(f"identity JSON is invalid: {path}") from error
    if payload != canonical_json_bytes(parsed):
        raise ReproducibilityError(f"identity JSON is not canonical: {path}")
    return _file_record(root, path, "canonical-json")


def _validate_hashed_assets(
    root: Path,
    directory: Path,
    *,
    digest_length: int,
    kind: str,
    recursive: bool = True,
) -> list[dict[str, object]]:
    if not directory.is_dir():
        raise ReproducibilityError(f"asset directory is missing: {directory}")
    pattern = re.compile(
        rf"^[A-Za-z0-9][A-Za-z0-9._-]*\.(?P<digest>[0-9a-f]{{{digest_length}}})"
        r"\.(?P<suffix>js|css)$"
    )
    source = directory.rglob("*") if recursive else directory.glob("*")
    candidates = sorted(
        path
        for path in source
        if path.is_file() and path.suffix.casefold() in {".js", ".css"}
    )
    if not candidates:
        raise ReproducibilityError(f"asset directory contains no JavaScript/CSS: {directory}")
    records: list[dict[str, object]] = []
    for path in candidates:
        match = pattern.fullmatch(path.name)
        if match is None:
            raise ReproducibilityError(f"asset is not content addressed: {path}")
        payload = path.read_bytes()
        _require_lf_text(path, payload)
        digest = sha256_bytes(payload)
        if not digest.startswith(match.group("digest")):
            raise ReproducibilityError(f"asset digest/name mismatch: {path}")
        records.append(_file_record(root, path, kind))
    return records


def build_contract(root: Path = ROOT, web_dist: Path | None = None) -> dict[str, object]:
    root = root.resolve(strict=True)
    _check_attributes(root)
    records: list[dict[str, object]] = []
    for path in _v1_shell_sources(root):
        payload = path.read_bytes()
        _require_lf_text(path, payload)
        records.append(_file_record(root, path, "shell-source"))
    for relative in V1_DEPENDENCY_LOCK_FILES:
        path = root / relative
        payload = path.read_bytes()
        _require_lf_text(path, payload)
        records.append(_file_record(root, path, "dependency-lock-source"))

    records.append(_validate_canonical_json(root, root / PUBLIC_POINTER.relative_to(ROOT)))
    records.append(_file_record(root, root / "deploy" / "ecorex-site" / "index.html", "public-entry"))
    records.extend(
        _validate_hashed_assets(
            root,
            root / "deploy" / "ecorex-site",
            digest_length=12,
            kind="public-content-addressed-asset",
            recursive=False,
        )
    )
    if web_dist is not None:
        resolved_dist = web_dist if web_dist.is_absolute() else root / web_dist
        resolved_dist = resolved_dist.resolve(strict=True)
        _relative(root, resolved_dist / "index.html")
        records.append(_file_record(root, resolved_dist / "index.html", "web-entry"))
        records.extend(
            _validate_hashed_assets(
                root,
                resolved_dist / "assets",
                digest_length=16,
                kind="web-content-addressed-asset",
            )
        )
    records.sort(key=lambda value: (str(value["path"]), str(value["kind"])))
    return {
        "document_type": CONTRACT_TYPE,
        "files": records,
        "schema_version": 1,
    }


def compare_contracts(directory: Path, expected_count: int) -> tuple[Path, ...]:
    if expected_count < 2:
        raise ReproducibilityError("manifest comparison requires at least two runners")
    manifests = tuple(sorted(directory.rglob("byte-contract.json")))
    if len(manifests) != expected_count:
        raise ReproducibilityError(
            f"expected {expected_count} byte contracts, found {len(manifests)}"
        )
    reference: bytes | None = None
    for path in manifests:
        payload = path.read_bytes()
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReproducibilityError(f"byte contract is invalid: {path}") from error
        if parsed.get("document_type") != CONTRACT_TYPE or payload != canonical_json_bytes(parsed):
            raise ReproducibilityError(f"byte contract is not canonical v1 evidence: {path}")
        if reference is None:
            reference = payload
        elif payload != reference:
            raise ReproducibilityError(
                "runner byte contracts differ: "
                f"{manifests[0].parent.name} != {path.parent.name}"
            )
    return manifests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check-v1-reproducibility")
    parser.add_argument(
        "--web-dist",
        type=Path,
        help="include a completed WebUI dist in the byte contract",
    )
    parser.add_argument(
        "--write-manifest",
        type=Path,
        help="write canonical runner-neutral byte evidence",
    )
    parser.add_argument(
        "--compare-manifests",
        type=Path,
        help="compare downloaded runner contracts instead of checking the checkout",
    )
    parser.add_argument("--expected-count", type=int, default=4)
    args = parser.parse_args(argv)
    try:
        if args.compare_manifests is not None:
            manifests = compare_contracts(args.compare_manifests, args.expected_count)
            result = {
                "compared_contracts": len(manifests),
                "schema_version": 1,
                "status": "passed",
            }
            print(canonical_json_bytes(result).decode("utf-8"), end="")
            return 0
        contract = build_contract(ROOT, args.web_dist)
        payload = canonical_json_bytes(contract)
        if args.write_manifest is not None:
            target = args.write_manifest
            if not target.is_absolute():
                target = ROOT / target
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        print(payload.decode("utf-8"), end="")
        return 0
    except (OSError, ReproducibilityError) as error:
        print(f"EcoreX v1 reproducibility gate failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
