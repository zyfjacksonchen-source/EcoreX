#!/usr/bin/env python3
"""Verify the four desktop handoffs and assemble one atomic e-Mate feed tree."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from typing import Any, Iterable, Mapping
from urllib.parse import quote
import zipfile

from ecorex.release import required_publication_sources, validate_public_bootstrap_index
from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseManifest,
    verify_artifact_file,
    verify_manifest_signature,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA512 = re.compile(r"^[A-Za-z0-9+/]{86}==$")
_SEMVER = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
_SAFE_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RELEASE_DATE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
)
_MAX_FILES = 500
_MAX_FILE_BYTES = 16 * 1024 * 1024 * 1024


class FeedError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--windows-root", required=True, type=Path)
    parser.add_argument("--macos-arm64-root", required=True, type=Path)
    parser.add_argument("--macos-x64-root", required=True, type=Path)
    parser.add_argument("--nginx-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--public-bootstrap-index", type=Path)
    parser.add_argument("--unsigned-manual", action="store_true")
    return parser


def _json(path: Path, maximum: int = 16 * 1024 * 1024) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or not 1 <= metadata.st_size <= maximum
        ):
            raise FeedError(f"invalid JSON: {path.name}")
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise FeedError(f"invalid JSON: {path.name}") from None
    if len(payload) != metadata.st_size or not isinstance(value, dict):
        raise FeedError(f"invalid JSON: {path.name}")
    return value


def _files(root: Path) -> tuple[Path, ...]:
    try:
        metadata = root.lstat()
    except OSError:
        raise FeedError(f"artifact root is unavailable: {root}") from None
    if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise FeedError(f"artifact root is invalid: {root}")
    result: list[Path] = []
    for path in sorted(root.rglob("*")):
        item = path.lstat()
        if path.is_symlink() or not (
            stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode)
        ):
            raise FeedError(f"artifact tree contains a link or special file: {path}")
        if stat.S_ISREG(item.st_mode):
            if not 1 <= item.st_size <= _MAX_FILE_BYTES:
                raise FeedError(f"artifact size is invalid: {path.name}")
            result.append(path)
    if not result or len(result) > _MAX_FILES:
        raise FeedError(f"artifact tree inventory is invalid: {root}")
    return tuple(result)


def _one(paths: Iterable[Path], name: str) -> Path:
    matches = [path for path in paths if path.name == name]
    if len(matches) != 1:
        raise FeedError(f"expected exactly one {name}")
    return matches[0]


def _sha256(path: Path) -> str:
    return _digest(path, "sha256").hex()


def _sha512(path: Path) -> str:
    return base64.b64encode(_digest(path, "sha512")).decode("ascii")


def _digest(path: Path, algorithm: str) -> bytes:
    before = path.lstat()
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise FeedError(f"file changed while opening: {path.name}")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ):
        raise FeedError(f"file changed while hashing: {path.name}")
    return digest.digest()


def _scalar(value: str) -> str:
    candidate = value.strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "\"'":
        candidate = candidate[1:-1]
    if not candidate or "\0" in candidate or "\\" in candidate:
        raise FeedError("update metadata scalar is invalid")
    return candidate


def _metadata(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or not 1 <= metadata.st_size <= 64 * 1024
        ):
            raise FeedError(f"update metadata size is invalid: {path.name}")
        payload = path.read_bytes()
        source = payload.decode("utf-8")
    except (OSError, UnicodeError):
        raise FeedError(f"update metadata is unreadable: {path.name}") from None
    if len(payload) != metadata.st_size:
        raise FeedError(f"update metadata changed while reading: {path.name}")
    result: dict[str, Any] = {"files": []}
    current: dict[str, Any] | None = None
    seen_top: set[str] = set()
    for raw in source.splitlines():
        if not raw.strip():
            continue
        match = re.fullmatch(r"version:\s*(.+)", raw)
        if match:
            if "version" in seen_top:
                raise FeedError("duplicate update metadata version")
            seen_top.add("version")
            result["version"] = _scalar(match.group(1))
            continue
        if raw == "files:":
            if "files" in seen_top:
                raise FeedError("duplicate update metadata files")
            seen_top.add("files")
            continue
        match = re.fullmatch(r"\s{2}- url:\s*(.+)", raw)
        if match:
            current = {"url": _scalar(match.group(1))}
            result["files"].append(current)
            continue
        match = re.fullmatch(r"\s{4}(sha512|size):\s*(.+)", raw)
        if match and current is not None:
            key, value = match.groups()
            if key in current:
                raise FeedError(f"duplicate update metadata {key}")
            current[key] = (
                int(value) if key == "size" and value.isdigit() else _scalar(value)
            )
            continue
        match = re.fullmatch(r"(path|sha512|releaseDate):\s*(.+)", raw)
        if match:
            key, value = match.groups()
            if key in seen_top:
                raise FeedError(f"duplicate update metadata {key}")
            seen_top.add(key)
            result[key] = _scalar(value)
            continue
        raise FeedError(f"unsupported update metadata line in {path.name}")
    if set(result) != {"version", "files", "path", "sha512", "releaseDate"}:
        raise FeedError(f"update metadata fields are incomplete: {path.name}")
    return result


def _validate_metadata(
    metadata_path: Path,
    files: Mapping[str, Path],
    *,
    expected_version: str,
    expected_names: set[str],
) -> dict[str, Any]:
    value = _metadata(metadata_path)
    if value["version"] != expected_version:
        raise FeedError(f"update metadata version mismatch: {metadata_path.name}")
    entries = value["files"]
    if (
        not isinstance(entries, list)
        or {item.get("url") for item in entries} != expected_names
    ):
        raise FeedError(f"update metadata inventory mismatch: {metadata_path.name}")
    by_name: dict[str, dict[str, Any]] = {}
    for item in entries:
        if set(item) != {"url", "sha512", "size"}:
            raise FeedError(
                f"update metadata file fields are invalid: {metadata_path.name}"
            )
        name = str(item["url"])
        path = files.get(name)
        if path is None or not _SAFE_NAME.fullmatch(name):
            raise FeedError(f"update metadata filename is invalid: {name}")
        if item["size"] != path.stat().st_size or item["sha512"] != _sha512(path):
            raise FeedError(f"update metadata digest mismatch: {name}")
        if not _SHA512.fullmatch(str(item["sha512"])):
            raise FeedError(f"update metadata SHA-512 is invalid: {name}")
        by_name[name] = dict(item)
    primary = by_name.get(str(value["path"]))
    if primary is None or value["sha512"] != primary["sha512"]:
        raise FeedError(f"update metadata primary file mismatch: {metadata_path.name}")
    return value


def _validate_checksums(
    path: Path, files: Mapping[str, Path], expected: set[str]
) -> None:
    observed: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise FeedError(f"checksum receipt is unreadable: {path.name}") from None
    for line in lines:
        match = re.fullmatch(
            r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]{0,179})", line
        )
        if match is None or match.group(2) in observed:
            raise FeedError(f"checksum receipt is invalid: {path.name}")
        observed[match.group(2)] = match.group(1)
    if set(observed) != expected:
        raise FeedError(f"checksum receipt inventory mismatch: {path.name}")
    if any(_sha256(files[name]) != digest for name, digest in observed.items()):
        raise FeedError(f"checksum receipt digest mismatch: {path.name}")


def _bootstrap_keyring(
    configs: Iterable[Mapping[str, Any]], field: str, role: str
) -> dict[str, bytes]:
    keyrings: list[dict[str, bytes]] = []
    for config in configs:
        value = config.get(field)
        if not isinstance(value, dict) or not 1 <= len(value) <= 8:
            raise FeedError(f"Bootstrap {role} trust is invalid")
        decoded: dict[str, bytes] = {}
        for key_id, encoded in value.items():
            if (
                not isinstance(key_id, str)
                or _SAFE_KEY_ID.fullmatch(key_id) is None
                or not isinstance(encoded, str)
            ):
                raise FeedError(f"Bootstrap {role} trust is invalid")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except ValueError:
                raise FeedError(f"Bootstrap {role} trust is invalid") from None
            if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != encoded:
                raise FeedError(f"Bootstrap {role} trust is invalid")
            decoded[key_id] = raw
        keyrings.append(decoded)
    if any(keyring != keyrings[0] for keyring in keyrings[1:]):
        raise FeedError(f"Bootstrap {role} trust differs by target")
    return keyrings[0]


def _signed_bootstrap_config(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise FeedError("signed Bootstrap archive is invalid")
        with zipfile.ZipFile(path) as archive:
            matches = [
                member
                for member in archive.infolist()
                if member.filename == "bootstrap-config.json"
            ]
            if (
                len(matches) != 1
                or matches[0].is_dir()
                or matches[0].flag_bits & 1
                or not 1 <= matches[0].file_size <= 64 * 1024
            ):
                raise FeedError("signed Bootstrap trust configuration is invalid")
            payload = archive.read(matches[0])
        value = json.loads(payload.decode("utf-8"))
    except (
        OSError,
        RuntimeError,
        NotImplementedError,
        UnicodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ):
        raise FeedError("signed Bootstrap trust configuration is invalid") from None
    if len(payload) != matches[0].file_size or not isinstance(value, dict):
        raise FeedError("signed Bootstrap trust configuration is invalid")
    return value, payload


def _verify_runtime(
    paths: tuple[Path, ...], *, version: str, source_sha: str
) -> tuple[
    Path,
    ReleaseManifest,
    dict[str, Any],
    dict[str, bytes],
    dict[str, bytes],
]:
    receipt = _json(_one(paths, "manual-webui-build-receipt.json"))
    manifests = [
        path
        for path in paths
        if path.name == "release-manifest.json" and path.parent.name == "release"
    ]
    if len(manifests) != 1:
        raise FeedError("signed Runtime manifest is missing or ambiguous")
    release_dir = manifests[0].parent
    manifest = ReleaseManifest.from_json(manifests[0].read_bytes())
    if (
        receipt.get("schema") != "emate.desktop-runtime-build-receipt.v2"
        or receipt.get("status") != "verified"
        or receipt.get("version") != version
        or receipt.get("source_commit") != source_sha
        or receipt.get("release_id") != manifest.release_id
        or receipt.get("build_digest") != manifest.build_digest
        or receipt.get("manifest_sha256") != _sha256(manifests[0])
        or manifest.version != version
    ):
        raise FeedError("Runtime receipt identity is invalid")
    config_paths = [path for path in paths if path.name == "bootstrap-config.json"]
    if len(config_paths) != 3 or {path.parent.name for path in config_paths} != {
        "windows-x64",
        "macos-arm64",
        "macos-x64",
    }:
        raise FeedError("Bootstrap trust configuration is missing")
    extracted = {path.parent.name: path for path in config_paths}
    configs: list[dict[str, Any]] = []
    for target in ("windows-x64", "macos-arm64", "macos-x64"):
        artifact = next(
            (
                item
                for item in manifest.artifacts
                if item.artifact_id == f"bootstrap-{target}"
            ),
            None,
        )
        if artifact is None:
            raise FeedError("signed Bootstrap trust configuration is missing")
        config, signed_payload = _signed_bootstrap_config(
            release_dir / artifact.file_name
        )
        try:
            extracted_payload = extracted[target].read_bytes()
        except OSError:
            raise FeedError("Bootstrap trust configuration is unreadable") from None
        if extracted_payload != signed_payload:
            raise FeedError("Bootstrap trust configuration differs from signed Runtime")
        configs.append(config)
    release_keys = _bootstrap_keyring(configs, "release_public_keys", "release")
    publication_keys = _bootstrap_keyring(
        configs, "publication_public_keys", "publication"
    )
    key = release_keys.get(manifest.signature.key_id)
    signing = receipt.get("signing")
    if (
        not isinstance(key, bytes)
        or len(key) != 32
        or not isinstance(signing, dict)
        or signing.get("key_id") != manifest.signature.key_id
        or signing.get("public_key_sha256") != hashlib.sha256(key).hexdigest()
        or signing.get("inner_integrity") != "ed25519"
    ):
        raise FeedError("Runtime signing identity is invalid")
    verifier = Ed25519SignatureVerifier(release_keys)
    verify_manifest_signature(manifest, verifier)
    for artifact in manifest.artifacts:
        verify_artifact_file(
            release_dir / artifact.file_name, manifest, artifact, verifier
        )
    metadata = _json(release_dir / "release-metadata.json")
    if (
        metadata.get("release_id") != manifest.release_id
        or metadata.get("version") != version
        or metadata.get("build_digest") != manifest.build_digest
        or metadata.get("manifest_sha256") != _sha256(manifests[0])
        or metadata.get("sbom_sha256") != _sha256(release_dir / "sbom.cdx.json")
    ):
        raise FeedError("Runtime release metadata is invalid")
    return release_dir, manifest, receipt, release_keys, publication_keys


def _merge_mac(arm64: Mapping[str, Any], x64: Mapping[str, Any]) -> bytes:
    files = sorted((*arm64["files"], *x64["files"]), key=lambda item: item["url"])
    if len(files) != 4 or len({item["url"] for item in files}) != 4:
        raise FeedError("macOS metadata cannot be merged without collisions")
    primary = next(item for item in files if str(item["url"]).endswith("-arm64.zip"))
    lines = [f"version: {arm64['version']}", "files:"]
    for item in files:
        lines.extend(
            (
                f"  - url: {item['url']}",
                f"    sha512: {item['sha512']}",
                f"    size: {item['size']}",
            )
        )
    lines.extend(
        (
            f"path: {primary['url']}",
            f"sha512: {primary['sha512']}",
            f"releaseDate: '{max(str(arm64['releaseDate']), str(x64['releaseDate']))}'",
        )
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _download_index(
    version: str,
    metadata: Mapping[str, Mapping[str, Any]],
    desktop: Mapping[str, Mapping[str, Path]],
    *,
    unsigned_manual: bool,
) -> bytes:
    released_at = max(str(value["releaseDate"]) for value in metadata.values())
    if not _RELEASE_DATE.fullmatch(released_at):
        raise FeedError("desktop release date is invalid")
    names = {
        "windows-x64": f"e-Mate-Setup-{version}-x64.exe",
        "macos-arm64": f"e-Mate-{version}-arm64.dmg",
        "macos-x64": f"e-Mate-{version}-x64.dmg",
    }
    downloads = []
    for target, platform, architecture in (
        ("windows-x64", "windows", "x64"),
        ("macos-arm64", "macos", "arm64"),
        ("macos-x64", "macos", "x64"),
    ):
        name = names[target]
        path = desktop[target][name]
        downloads.append(
            {
                "target": target,
                "platform": platform,
                "architecture": architecture,
                "file_name": name,
                "url": f"https://dl.ecoremedia.net/e-mate/update/{name}",
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return (
        json.dumps(
            {
                "schema_version": 2 if unsigned_manual else 1,
                "product": "e-Mate",
                "version": version,
                **({"distribution_mode": "unsigned-manual"} if unsigned_manual else {}),
                "released_at": released_at,
                "downloads": downloads,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _validate_nginx(path: Path, *, unsigned_manual: bool) -> str:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise FeedError("Nginx update-feed configuration is unreadable") from None
    required = (
        "location = /e-mate/update/latest.yml",
        "location = /e-mate/update/latest-mac.yml",
        "location = /e-mate/update/download-index.json",
        "location = /e-mate/update/public-bootstrap-index.json",
        "location ^~ /e-mate/update/",
        "alias /srv/e-mate-update/current/",
    )
    if (
        any(value not in source for value in required)
        or "index.html" in source
        or "@spa" in source
    ):
        raise FeedError("Nginx update-feed configuration can fall through to the SPA")
    manual_required = (
        "location = /e-mate/update/latest.yml {\n    return 404;",
        "location = /e-mate/update/latest-mac.yml {\n    return 404;",
        "location = /e-mate/update/public-bootstrap-index.json {\n    return 404;",
        "location = /e-mate/update/ {\n    return 302 /e-mate/;",
        "application/vnd.microsoft.portable-executable exe;",
        "application/x-apple-diskimage dmg;",
        "application/zip zip;",
        "max_ranges 1;",
    )
    if unsigned_manual and any(value not in source for value in manual_required):
        raise FeedError("Nginx unsigned-manual routes are incomplete")
    signed_required = (
        "alias /srv/e-mate-update/current/latest.yml;",
        "alias /srv/e-mate-update/current/latest-mac.yml;",
        "alias /srv/e-mate-update/current/public-bootstrap-index.json;",
    )
    if not unsigned_manual and any(value not in source for value in signed_required):
        raise FeedError("Nginx signed feed routes are incomplete")
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _snapshot_file(
    source: Path, destination: Path, maximum: int = _MAX_FILE_BYTES
) -> None:
    before = source.lstat()
    if (
        source.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or not 1 <= before.st_size <= maximum
    ):
        raise FeedError(f"artifact is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("xb") as writer:
        opened = os.fstat(reader.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise FeedError(f"file changed while opening: {source.name}")
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
        after = os.fstat(reader.fileno())
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ):
        raise FeedError(f"file changed while copying: {source.name}")


def _snapshot_tree(source: Path, destination: Path) -> tuple[Path, ...]:
    paths = _files(source)
    for path in paths:
        _snapshot_file(path, destination / path.relative_to(source))
    return _files(destination)


def _publish_snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def _bind_public_index(
    value: Mapping[str, Any], manifest: ReleaseManifest, manifest_sha256: str
) -> None:
    def sources(file_name: str) -> list[dict[str, Any]]:
        return [
            {
                "source_id": source.source_id,
                "kind": source.kind.value,
                "priority": source.priority,
                "url": f"{source.base_url}/{quote(file_name, safe='')}",
            }
            for source in required_publication_sources(manifest)
        ]

    release = value.get("release")
    if not isinstance(release, Mapping) or (
        release.get("version"),
        release.get("release_id"),
        release.get("channel"),
        release.get("created_at"),
        release.get("build_digest"),
    ) != (
        manifest.version,
        manifest.release_id,
        manifest.channel.value,
        manifest.created_at,
        manifest.build_digest,
    ):
        raise FeedError("public Bootstrap index targets a different release")
    public_manifest = release.get("manifest")
    if not isinstance(public_manifest, Mapping) or any(
        public_manifest.get(field) != expected
        for field, expected in (
            ("file_name", "release-manifest.json"),
            ("sha256", manifest_sha256),
            ("signature", manifest.signature.to_dict()),
            ("sources", sources("release-manifest.json")),
        )
    ):
        raise FeedError("public Bootstrap index differs from signed Runtime")
    artifacts = release.get("bootstrap_artifacts")
    expected_artifacts = [
        manifest.artifact(artifact_id)
        for artifact_id in (
            "bootstrap-windows-x64",
            "bootstrap-macos-arm64",
            "bootstrap-macos-x64",
        )
    ]
    if not isinstance(artifacts, list) or len(artifacts) != len(expected_artifacts):
        raise FeedError("public Bootstrap index differs from signed Runtime")
    fields = (
        "artifact_id",
        "platform",
        "architecture",
        "file_name",
        "size_bytes",
        "sha256",
        "signature",
    )
    for public, expected in zip(artifacts, expected_artifacts, strict=True):
        expected_value = expected.to_dict()
        if (
            not isinstance(public, Mapping)
            or any(public.get(field) != expected_value[field] for field in fields)
            or public.get("sources") != sources(expected.file_name)
        ):
            raise FeedError("public Bootstrap index differs from signed Runtime")


def _record(
    path: Path, *, role: str, source_artifact: str, root: Path
) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "role": role,
        "source_artifact": source_artifact,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    if not _SEMVER.fullmatch(args.expected_version) or not _COMMIT.fullmatch(
        args.expected_source_sha
    ):
        raise FeedError("expected release identity is invalid")
    if args.unsigned_manual and args.public_bootstrap_index is not None:
        raise FeedError("unsigned manual feed cannot carry a public Bootstrap index")
    source_roots = {
        "runtime": args.runtime_root.resolve(strict=True),
        "windows-x64": args.windows_root.resolve(strict=True),
        "macos-arm64": args.macos_arm64_root.resolve(strict=True),
        "macos-x64": args.macos_x64_root.resolve(strict=True),
    }
    output = args.output.absolute()
    if os.path.lexists(output):
        raise FeedError("feed output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    output = output.parent.resolve(strict=True) / output.name
    if any(output.is_relative_to(root) for root in source_roots.values()):
        raise FeedError("feed output overlaps an artifact root")
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        input_root = staging / ".inputs"
        roots = {
            name: input_root / name
            for name in ("runtime", "windows-x64", "macos-arm64", "macos-x64")
        }
        inventories = {
            name: _snapshot_tree(source_roots[name], roots[name]) for name in roots
        }
        nginx_config = input_root / "nginx.conf"
        _snapshot_file(args.nginx_config.resolve(strict=True), nginx_config, 256 * 1024)
        public_index: Path | None = None
        if args.public_bootstrap_index is not None:
            public_index = input_root / "public-bootstrap-index.json"
            _snapshot_file(
                args.public_bootstrap_index.resolve(strict=True),
                public_index,
                256 * 1024,
            )

        release_dir, manifest, runtime_receipt, release_keys, publication_keys = (
            _verify_runtime(
                inventories["runtime"],
                version=args.expected_version,
                source_sha=args.expected_source_sha,
            )
        )
        version = args.expected_version
        expected = {
            "windows-x64": {f"e-Mate-Setup-{version}-x64.exe"},
            "macos-arm64": {
                f"e-Mate-{version}-arm64.dmg",
                f"e-Mate-{version}-arm64.zip",
            },
            "macos-x64": {
                f"e-Mate-{version}-x64.dmg",
                f"e-Mate-{version}-x64.zip",
            },
        }
        desktop: dict[str, dict[str, Path]] = {}
        metadata: dict[str, dict[str, Any]] = {}
        for target, names in expected.items():
            paths = inventories[target]
            files = {name: _one(paths, name) for name in names}
            for name in names:
                files[f"{name}.blockmap"] = _one(paths, f"{name}.blockmap")
            desktop[target] = files
            checksum = _one(paths, f"{target}.sha256")
            metadata_name = (
                "latest.yml"
                if target == "windows-x64"
                else f"latest-mac-{target.removeprefix('macos-')}.yml"
            )
            metadata_path = _one(paths, metadata_name)
            checksum_files = {**files, metadata_name: metadata_path}
            _validate_checksums(checksum, checksum_files, set(checksum_files))
            metadata[target] = _validate_metadata(
                metadata_path,
                files,
                expected_version=version,
                expected_names=names,
            )
        nginx_sha256 = _validate_nginx(
            nginx_config, unsigned_manual=args.unsigned_manual
        )
        if public_index is not None:
            value = _json(public_index, 256 * 1024)
            validate_public_bootstrap_index(
                value,
                verifier=Ed25519SignatureVerifier(release_keys),
                freshness_verifier=Ed25519SignatureVerifier(publication_keys),
            )
            _bind_public_index(value, manifest, str(runtime_receipt["manifest_sha256"]))

        records: list[dict[str, Any]] = []
        if not args.unsigned_manual:
            windows_metadata = _one(inventories["windows-x64"], "latest.yml")
            _publish_snapshot(windows_metadata, staging / "latest.yml")
            records.append(
                _record(
                    staging / "latest.yml",
                    role="pointer",
                    source_artifact="windows-x64",
                    root=staging,
                )
            )
            merged = staging / "latest-mac.yml"
            merged.write_bytes(
                _merge_mac(metadata["macos-arm64"], metadata["macos-x64"])
            )
            records.append(
                _record(
                    merged, role="pointer", source_artifact="feed-gate", root=staging
                )
            )
        download_index = staging / "download-index.json"
        download_index.write_bytes(
            _download_index(
                version, metadata, desktop, unsigned_manual=args.unsigned_manual
            )
        )
        records.append(
            _record(
                download_index,
                role="pointer",
                source_artifact="feed-gate",
                root=staging,
            )
        )
        for target, files in desktop.items():
            for name, source in sorted(files.items()):
                destination = staging / name
                _publish_snapshot(source, destination)
                records.append(
                    _record(
                        destination,
                        role="immutable-desktop",
                        source_artifact=target,
                        root=staging,
                    )
                )
        runtime_target = staging / "runtime" / manifest.release_id
        for source in sorted(path for path in release_dir.iterdir() if path.is_file()):
            destination = runtime_target / source.name
            _publish_snapshot(source, destination)
            records.append(
                _record(
                    destination,
                    role="immutable-runtime",
                    source_artifact="runtime",
                    root=staging,
                )
            )
        if public_index is not None:
            destination = staging / "public-bootstrap-index.json"
            _publish_snapshot(public_index, destination)
            records.append(
                _record(
                    destination,
                    role="pointer",
                    source_artifact="runtime-publication",
                    root=staging,
                )
            )
        shutil.rmtree(input_root)
        records.sort(key=lambda item: item["path"])
        build_id = hashlib.sha256(
            json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        receipt = {
            "schema_version": 2 if args.unsigned_manual else 1,
            "document_type": "emate.desktop-feed-stage",
            **({"distribution_mode": "unsigned-manual"} if args.unsigned_manual else {}),
            "status": (
                "activation-ready"
                if public_index is not None
                else "activation-ready-unsigned-manual"
                if args.unsigned_manual
                else "awaiting-public-bootstrap-index"
            ),
            "version": version,
            "source_commit": args.expected_source_sha,
            "release_id": manifest.release_id,
            "build_digest": manifest.build_digest,
            "runtime_manifest_sha256": runtime_receipt["manifest_sha256"],
            "feed_build_id": build_id,
            "candidate_target": f"releases/v{version}-{build_id[:16]}",
            "nginx_config_sha256": nginx_sha256,
            "files": records,
            "activation": {
                "strategy": "same-filesystem-current-symlink-rename",
                "allowed_operations": ["activate", "rollback"],
                "link": "/srv/e-mate-update/current",
                "pointer_files": (
                    ["download-index.json"]
                    if args.unsigned_manual
                    else [
                        "latest.yml",
                        "latest-mac.yml",
                        "download-index.json",
                        "public-bootstrap-index.json",
                    ]
                ),
                "missing_files_must_return": 404,
                "receipt_required_fields": [
                    "operation",
                    "feed_build_id",
                    "previous_target",
                    "new_target",
                    "manifest_sha256",
                    "public_readback_sha256",
                    "completed_at",
                ],
            },
        }
        (staging / "feed-stage-receipt.json").write_text(
            json.dumps(
                receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return receipt


def main(argv: list[str] | None = None) -> int:
    try:
        result = prepare(_parser().parse_args(argv))
    except (RuntimeError, OSError, ValueError) as error:
        print(f"emate_feed_gate_failed:{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
