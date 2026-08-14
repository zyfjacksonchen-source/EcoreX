#!/usr/bin/env python3
"""Upload and verify the immutable e-Mate desktop and Runtime downloads in R2."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.request import Request, urlopen


BUCKET = "emate-desktop-downloads"
PUBLIC_ORIGIN = "https://pub-ada3f610c0234a76838f4e19fe2bb25e.r2.dev"
_VERSION = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_ID = re.compile(r"^release-stable-[0-9a-f]{24}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
_PUBLIC_HEADERS = {"User-Agent": "e-Mate-Desktop-Publisher/1.0", "Accept": "*/*"}
_LIVE_REFERENCE_URLS = (
    "https://mvdcm.ecoremedia.net/e-mate/update/download-index.json",
    "https://mvdcm.ecoremedia.net/e-mate/update/latest.yml",
    "https://mvdcm.ecoremedia.net/e-mate/update/latest-mac.yml",
    "https://mvdcm.ecoremedia.net/e-mate/update/public-bootstrap-index.json",
    "https://dl.ecoremedia.net/e-mate/update/download-index.json",
    "https://dl.ecoremedia.net/e-mate/update/latest.yml",
    "https://dl.ecoremedia.net/e-mate/update/latest-mac.yml",
    "https://dl.ecoremedia.net/e-mate/update/public-bootstrap-index.json",
)
_MAX_RECEIPT_BYTES = 2 * 1024 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--windows-root", required=True, type=Path)
    parser.add_argument("--macos-arm64-root", required=True, type=Path)
    parser.add_argument("--macos-x64-root", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--publication-receipt", type=Path)
    return parser


def _reclaim_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    for name in ("admission-receipt", "publication-receipt"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    for name in ("admission-sha256", "version", "source-sha", "release-id"):
        parser.add_argument(f"--expected-{name}", required=True)
    parser.add_argument("--observed-source-sha", required=True)
    return parser


def _sha256(path: Path) -> tuple[str, tuple[int, int, int, int]]:
    before = path.lstat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise RuntimeError(f"r2_artifact_changed:{path.name}")
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(source.fileno())
    identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
        raise RuntimeError(f"r2_artifact_changed:{path.name}")
    return digest.hexdigest(), identity


def _artifact(root: Path, name: str) -> Path:
    if _SAFE_NAME.fullmatch(name) is None:
        raise RuntimeError("r2_artifact_name_invalid")
    root_metadata = root.lstat()
    if root.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
        raise RuntimeError("r2_artifact_root_invalid")
    path = root.resolve(strict=True) / name
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1:
        raise RuntimeError(f"r2_artifact_invalid:{name}")
    return path


def _records(args: argparse.Namespace) -> list[tuple[dict[str, Any], Path]]:
    if _VERSION.fullmatch(args.version) is None:
        raise RuntimeError("r2_version_invalid")
    names = (
        (
            "windows-x64",
            args.windows_root,
            f"e-Mate-Setup-{args.version}-x64.exe",
            "application/vnd.microsoft.portable-executable",
        ),
        (
            "macos-arm64",
            args.macos_arm64_root,
            f"e-Mate-{args.version}-arm64.dmg",
            "application/x-apple-diskimage",
        ),
        (
            "macos-x64",
            args.macos_x64_root,
            f"e-Mate-{args.version}-x64.dmg",
            "application/x-apple-diskimage",
        ),
        (
            "windows-x64-blockmap",
            args.windows_root,
            f"e-Mate-Setup-{args.version}-x64.exe.blockmap",
            "application/octet-stream",
        ),
    )
    result: list[tuple[dict[str, Any], Path]] = []
    for target, root, name, content_type in names:
        path = _artifact(root, name)
        digest, identity = _sha256(path)
        key = f"desktop/v{args.version}/{name}"
        result.append(
            (
                {
                    "target": target,
                    "file_name": name,
                    "key": key,
                    "url": f"{PUBLIC_ORIGIN}/{key}",
                    "size_bytes": path.stat().st_size,
                    "sha256": digest,
                    "source_identity": identity,
                    "content_type": content_type,
                },
                path,
            )
        )
    release = args.runtime_root.resolve(strict=True) / "release"
    manifest_path = _artifact(release, "release-manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError("r2_runtime_manifest_invalid") from None
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != args.version
        or not isinstance(artifacts, list)
    ):
        raise RuntimeError("r2_runtime_manifest_invalid")
    runtime: list[tuple[str, Path, str]] = []
    seen_targets: set[str] = set()
    seen_names: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            raise RuntimeError("r2_runtime_manifest_invalid")
        target, name = item.get("artifact_id"), item.get("file_name")
        if (
            not isinstance(target, str)
            or not isinstance(name, str)
            or target in seen_targets
            or name in seen_names
        ):
            raise RuntimeError("r2_runtime_manifest_invalid")
        path = _artifact(release, name)
        digest, _ = _sha256(path)
        if item.get("size_bytes") != path.stat().st_size or item.get("sha256") != digest:
            raise RuntimeError(f"r2_runtime_artifact_mismatch:{name}")
        seen_targets.add(target)
        seen_names.add(name)
        runtime.append((target, path, "application/zip"))
    if not {
        "bootstrap-windows-x64",
        "bootstrap-macos-arm64",
        "bootstrap-macos-x64",
    }.issubset(seen_targets):
        raise RuntimeError("r2_runtime_bootstrap_incomplete")
    for target, name in (
        ("runtime-manifest", "release-manifest.json"),
        ("runtime-metadata", "release-metadata.json"),
        ("runtime-sbom", "sbom.cdx.json"),
    ):
        runtime.append((target, _artifact(release, name), "application/json"))
    for target, path, content_type in runtime:
        digest, identity = _sha256(path)
        key = f"desktop/v{args.version}/{path.name}"
        result.append(
            (
                {
                    "target": target,
                    "file_name": path.name,
                    "key": key,
                    "url": f"{PUBLIC_ORIGIN}/{key}",
                    "size_bytes": path.stat().st_size,
                    "sha256": digest,
                    "source_identity": identity,
                    "content_type": content_type,
                },
                path,
            )
        )
    return result


def _s3_client(
    environ: Mapping[str, str] = os.environ,
    *,
    client_factory: Callable[..., object] | None = None,
    config_factory: Callable[..., object] | None = None,
):
    names = (
        "ECOREX_R2_ACCOUNT_ID",
        "ECOREX_R2_ACCESS_KEY_ID",
        "ECOREX_R2_SECRET_ACCESS_KEY",
    )
    values = {name: environ.get(name, "") for name in names}
    if any(not value for value in values.values()):
        raise RuntimeError("r2_credentials_missing")
    if client_factory is None or config_factory is None:
        try:
            import boto3  # noqa: PLC0415 - release job installs locked cloud profile
            from botocore.config import Config  # noqa: PLC0415
        except ImportError:
            raise RuntimeError("r2_cloud_profile_missing") from None
        client_factory = boto3.client
        config_factory = Config
    return client_factory(
        "s3",
        endpoint_url=(
            f"https://{values['ECOREX_R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
        ),
        aws_access_key_id=values["ECOREX_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=values["ECOREX_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=config_factory(retries={"mode": "standard", "total_max_attempts": 5}),
    )


def _transfer_config():
    try:
        from boto3.s3.transfer import TransferConfig  # noqa: PLC0415
    except ImportError:
        raise RuntimeError("r2_cloud_profile_missing") from None
    return TransferConfig(
        multipart_threshold=8 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=1,
        use_threads=False,
    )


def _remote_matches(client: object, record: Mapping[str, Any]) -> bool | None:
    for attempt in range(5):
        try:
            value = client.head_object(Bucket=BUCKET, Key=record["key"])
            return value.get("ContentLength") == record["size_bytes"] and value.get(
                "Metadata"
            ) == {"sha256": record["sha256"]}
        except Exception as error:
            response = getattr(error, "response", None)
            if isinstance(response, dict) and str(
                response.get("Error", {}).get("Code")
            ) in {
                "404",
                "NoSuchKey",
                "NotFound",
            }:
                return None
            if attempt < 4:
                time.sleep(2**attempt)
    raise RuntimeError(
        f"r2_authenticated_head_failed:{record['file_name']}"
    ) from None


def _upload(client: object, record: Mapping[str, Any], path: Path) -> None:
    current = path.lstat()
    if (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ) != tuple(record["source_identity"]):
        raise RuntimeError(f"r2_artifact_changed:{record['file_name']}")
    remote = _remote_matches(client, record)
    if remote is True:
        return
    if remote is False:
        raise RuntimeError(f"r2_object_collision:{record['file_name']}")
    upload_error: Exception | None = None
    if remote is None:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ) != tuple(record["source_identity"]):
                raise RuntimeError(f"r2_artifact_changed:{record['file_name']}")
            try:
                client.upload_fileobj(
                    source,
                    BUCKET,
                    str(record["key"]),
                    ExtraArgs={
                        "Metadata": {"sha256": str(record["sha256"])},
                        "ContentType": str(record["content_type"]),
                        "CacheControl": "public,max-age=31536000,immutable",
                    },
                    Config=_transfer_config(),
                )
            except Exception as error:
                upload_error = error
            after = os.fstat(source.fileno())
            if (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) != tuple(record["source_identity"]):
                raise RuntimeError(f"r2_artifact_changed:{record['file_name']}")
    try:
        admitted = _remote_matches(client, record) is True
    except Exception:
        if upload_error is not None:
            raise RuntimeError(f"r2_upload_failed:{record['file_name']}") from None
        raise
    if upload_error is not None:
        if admitted:
            return
        raise RuntimeError(f"r2_upload_failed:{record['file_name']}") from None
    if not admitted:
        raise RuntimeError(f"r2_authenticated_head_failed:{record['file_name']}")


def _verify_public_once(
    record: Mapping[str, Any],
    path: Path,
    *,
    opener: Callable[..., Any],
) -> None:
    try:
        with opener(
            Request(str(record["url"]), headers=_PUBLIC_HEADERS, method="HEAD"),
            timeout=30,
        ) as response:
            if (
                response.status != 200
                or response.headers.get("Content-Length")
                != str(record["size_bytes"])
                or response.headers.get("Accept-Ranges", "").casefold() != "bytes"
            ):
                raise RuntimeError(f"r2_public_head_failed:{record['file_name']}")
    except Exception:
        raise RuntimeError(
            f"r2_public_head_failed:{record['file_name']}"
        ) from None
    size = int(record["size_bytes"])
    spans = ((0, min(15, size - 1)), (max(0, size - 16), size - 1))
    with path.open("rb") as source:
        for start, end in spans:
            request = Request(
                str(record["url"]),
                headers={
                    **_PUBLIC_HEADERS,
                    "Range": f"bytes={start}-{end}",
                },
            )
            try:
                with opener(request, timeout=30) as response:
                    payload = response.read(end - start + 2)
                    if (
                        response.status != 206
                        or response.headers.get("Content-Range")
                        != f"bytes {start}-{end}/{size}"
                        or len(payload) != end - start + 1
                    ):
                        raise RuntimeError(
                            f"r2_public_range_failed:{record['file_name']}"
                        )
            except Exception:
                raise RuntimeError(
                    f"r2_public_range_failed:{record['file_name']}"
                ) from None
            source.seek(start)
            if payload != source.read(end - start + 1):
                raise RuntimeError(f"r2_public_bytes_failed:{record['file_name']}")


def _verify_public(
    record: Mapping[str, Any],
    path: Path,
    *,
    opener: Callable[..., Any] = urlopen,
) -> None:
    last: Exception | None = None
    for attempt in range(5):
        try:
            _verify_public_once(record, path, opener=opener)
            return
        except Exception as error:  # public R2 read-after-write may briefly lag
            last = error
            if attempt < 4:
                time.sleep(2**attempt)
    assert last is not None
    raise last


def _publish_one(
    client: object,
    record: dict[str, Any],
    path: Path,
    public_probe: Callable[[Mapping[str, Any], Path], None],
) -> dict[str, Any]:
    _upload(client, record, path)
    public_probe(record, path)
    current = path.lstat()
    if (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ) != tuple(record["source_identity"]):
        raise RuntimeError(f"r2_artifact_changed:{record['file_name']}")
    return {
        key: value
        for key, value in record.items()
        if key not in {"content_type", "source_identity"}
    }


def _write_receipt(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise RuntimeError("r2_receipt_path_invalid")
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as output:
        json.dump(
            value, output, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        output.write("\n")
        temporary = Path(output.name)
    os.replace(temporary, path)


def _strict_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or not 1 <= len(payload) <= _MAX_RECEIPT_BYTES
            or len(payload) != metadata.st_size
        ):
            raise RuntimeError(f"r2_reclaim_{label}_invalid")
        value = json.loads(payload.decode("utf-8"))
        canonical = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if not isinstance(value, dict):
            raise ValueError("not an object")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise RuntimeError(f"r2_reclaim_{label}_invalid") from None
    return value, hashlib.sha256(canonical).hexdigest()


def _reclaim_documents(
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], set[str]]:
    if (
        _VERSION.fullmatch(str(args.expected_version)) is None
        or _SHA256.fullmatch(str(args.expected_admission_sha256)) is None
        or _COMMIT.fullmatch(str(args.expected_source_sha)) is None
        or args.observed_source_sha != args.expected_source_sha
        or _RELEASE_ID.fullmatch(str(args.expected_release_id)) is None
    ):
        raise RuntimeError("r2_reclaim_identity_mismatch")
    admission, admission_sha256 = _strict_json(args.admission_receipt, "admission")
    if (
        admission_sha256 != args.expected_admission_sha256
        or admission.get("document_type") != "emate.r2-download-admission"
        or admission.get("status") != "verified"
        or admission.get("version") != args.expected_version
        or admission.get("bucket") != BUCKET
        or admission.get("public_origin") != PUBLIC_ORIGIN
        or not isinstance(admission.get("objects"), list)
        or not 1 <= len(admission["objects"]) <= 500
    ):
        raise RuntimeError("r2_reclaim_admission_invalid")

    records: list[dict[str, Any]] = []
    identities: set[str] = set()
    for item in admission["objects"]:
        name = item.get("file_name") if isinstance(item, dict) else None
        key = f"desktop/v{args.expected_version}/{name}"
        if (
            not isinstance(item, dict)
            or set(item)
            != {"target", "file_name", "key", "url", "size_bytes", "sha256"}
            or not isinstance(name, str)
            or _SAFE_NAME.fullmatch(name) is None
            or not isinstance(item.get("size_bytes"), int)
            or isinstance(item["size_bytes"], bool)
            or item["size_bytes"] < 1
            or _SHA256.fullmatch(str(item.get("sha256"))) is None
            or item.get("key") != key
            or item.get("url") != f"{PUBLIC_ORIGIN}/{key}"
            or key in identities
        ):
            raise RuntimeError("r2_reclaim_admission_invalid")
        identities.update((name, key, item["url"]))
        records.append(dict(item))

    publication, _ = _strict_json(args.publication_receipt, "publication")
    manifest = next(
        (record for record in records if record["target"] == "runtime-manifest"),
        None,
    )
    if (
        publication.get("release_id") != args.expected_release_id
        or publication.get("version") != args.expected_version
        or manifest is None
        or publication.get("manifest_sha256") != manifest["sha256"]
    ):
        raise RuntimeError("r2_reclaim_publication_invalid")
    identities.update((args.expected_admission_sha256, args.expected_source_sha,
                       args.expected_release_id, publication["manifest_sha256"]))
    return records, identities


def _verify_no_live_references(
    identities: set[str], *, opener: Callable[..., Any] = urlopen
) -> None:
    needles = {value.encode("utf-8") for value in identities}
    for url in _LIVE_REFERENCE_URLS:
        try:
            with opener(
                Request(url, headers={**_PUBLIC_HEADERS, "Cache-Control": "no-cache"}),
                timeout=30,
            ) as response:
                payload = response.read(_MAX_RECEIPT_BYTES + 1)
                if response.status != 200 or len(payload) > _MAX_RECEIPT_BYTES:
                    raise RuntimeError("r2_reclaim_live_read_failed")
        except HTTPError as error:
            if error.code == 404:
                continue
            raise RuntimeError("r2_reclaim_live_read_failed") from None
        except Exception:
            raise RuntimeError("r2_reclaim_live_read_failed") from None
        if any(needle in payload for needle in needles):
            raise RuntimeError("r2_reclaim_live_reference")


def _verify_public_missing(
    record: Mapping[str, Any], *, opener: Callable[..., Any] = urlopen
) -> None:
    request = Request(
        str(record["url"]),
        headers={**_PUBLIC_HEADERS, "Cache-Control": "no-cache"},
        method="HEAD",
    )
    for attempt in range(5):
        try:
            with opener(request, timeout=30) as response:
                if response.status == 404:
                    return
        except HTTPError as error:
            if error.code == 404:
                return
        except Exception:
            pass
        if attempt < 4:
            time.sleep(2**attempt)
    raise RuntimeError(
        f"r2_reclaim_public_readback_failed:{record['file_name']}"
    )


def _abort_exact_multipart(client: object, keys: set[str]) -> None:
    try:
        pages = client.get_paginator("list_multipart_uploads").paginate(Bucket=BUCKET)
        uploads = [
            (item["Key"], item["UploadId"])
            for page in pages
            for item in page.get("Uploads", [])
            if item.get("Key") in keys
        ]
        if len(uploads) != len(set(uploads)):
            raise ValueError("duplicate upload")
        for key, upload_id in sorted(uploads):
            client.abort_multipart_upload(Bucket=BUCKET, Key=key, UploadId=upload_id)
    except Exception:
        raise RuntimeError("r2_reclaim_multipart_failed") from None


def _release_publication_receipt(
    manifest: Mapping[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    sources = manifest.get("sources")
    artifacts = manifest.get("artifacts")
    if (
        not isinstance(sources, list)
        or not sources
        or not isinstance(sources[0], dict)
        or sources[0].get("source_id") != "github-cn"
        or sources[0].get("kind") != "github-cn-mirror"
        or sources[0].get("priority") != 0
        or sources[0].get("base_url")
        != f"{PUBLIC_ORIGIN}/desktop/v{manifest.get('version')}"
        or re.fullmatch(r"release-stable-[0-9a-f]{24}", str(manifest.get("release_id")))
        is None
        or not isinstance(artifacts, list)
    ):
        raise RuntimeError("r2_publication_manifest_invalid")
    by_name = {str(item.get("file_name")): item for item in records}
    expected = {
        "release-manifest.json",
        "release-metadata.json",
        "sbom.cdx.json",
        *(str(item.get("file_name")) for item in artifacts if isinstance(item, dict)),
    }
    if len(expected) != len(artifacts) + 3 or any(name not in by_name for name in expected):
        raise RuntimeError("r2_publication_inventory_invalid")
    return {
        "schema_version": 2,
        "release_id": manifest.get("release_id"),
        "version": manifest.get("version"),
        "manifest_sha256": by_name["release-manifest.json"]["sha256"],
        "publication_policy": "stable-primary-only",
        "source_receipts": {
            "github-cn": [
                {
                    "name": name,
                    "size_bytes": by_name[name]["size_bytes"],
                    "sha256": by_name[name]["sha256"],
                    "url": by_name[name]["url"],
                }
                for name in sorted(expected)
            ]
        },
    }


def _write_canonical_receipt(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise RuntimeError("r2_receipt_path_invalid")
    with tempfile.NamedTemporaryFile(
        "wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as output:
        output.write(payload)
        temporary = Path(output.name)
    os.replace(temporary, path)


def publish(
    args: argparse.Namespace,
    *,
    client: object | None = None,
    public_probe: Callable[[Mapping[str, Any], Path], None] = _verify_public,
) -> dict[str, Any]:
    client = _s3_client() if client is None else client
    records = _records(args)
    completed: list[dict[str, Any]] = []
    errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_publish_one, client, record, path, public_probe): record
            for record, path in records
        }
        for future in as_completed(futures):
            try:
                completed.append(future.result())
            except Exception as error:
                errors.append(error)
    if errors:
        raise errors[0]
    completed.sort(key=lambda item: str(item["key"]))
    receipt = {
        "schema_version": 2,
        "document_type": "emate.r2-download-admission",
        "status": "verified",
        "version": args.version,
        "bucket": BUCKET,
        "public_origin": PUBLIC_ORIGIN,
        "max_parallel_multipart": 3,
        "objects": completed,
    }
    _write_receipt(args.receipt, receipt)
    publication_receipt = getattr(args, "publication_receipt", None)
    if publication_receipt is not None:
        manifest_path = args.runtime_root.resolve(strict=True) / "release/release-manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise RuntimeError("r2_publication_manifest_invalid") from None
        if not isinstance(manifest, dict):
            raise RuntimeError("r2_publication_manifest_invalid")
        _write_canonical_receipt(
            publication_receipt,
            _release_publication_receipt(manifest, completed),
        )
    return receipt


def reclaim_rejected(
    args: argparse.Namespace,
    *,
    client: object | None = None,
    reference_probe: Callable[[set[str]], None] = _verify_no_live_references,
    public_missing_probe: Callable[[Mapping[str, Any]], None] = _verify_public_missing,
) -> dict[str, Any]:
    records, identities = _reclaim_documents(args)
    client = _s3_client() if client is None else client
    reference_probe(identities)
    states = [_remote_matches(client, record) for record in records]
    if False in states:
        raise RuntimeError("r2_reclaim_remote_drift")
    for record, state in zip(records, states):
        if state is None:
            public_missing_probe(record)
    _abort_exact_multipart(client, {str(record["key"]) for record in records})
    for record, state in zip(records, states):
        if state is None:
            continue
        current = _remote_matches(client, record)
        if current is False:
            raise RuntimeError("r2_reclaim_remote_drift")
        if current is None:
            public_missing_probe(record)
            continue
        try:
            client.delete_object(Bucket=BUCKET, Key=record["key"])
        except Exception:
            raise RuntimeError("r2_reclaim_delete_failed") from None
        if _remote_matches(client, record) is not None:
            raise RuntimeError("r2_reclaim_authenticated_readback_failed")
        public_missing_probe(record)
    return {"status": "reclaimed", "objects_reclaimed": len(records)}


def main(argv: list[str] | None = None) -> int:
    raw = list(os.sys.argv[1:] if argv is None else argv)
    reclaiming = bool(raw and raw[0] == "reclaim-rejected")
    try:
        receipt = (
            reclaim_rejected(_reclaim_parser().parse_args(raw[1:]))
            if reclaiming
            else publish(_parser().parse_args(raw))
        )
    except Exception as error:
        category = (
            str(error)
            if isinstance(error, RuntimeError)
            and re.fullmatch(r"r2_[a-z_]+(?::[A-Za-z0-9._-]+)?", str(error))
            else "r2_operation_failed"
        )
        operation = "reclaim" if reclaiming else "publish"
        print(f"emate_r2_{operation}_failed:{category}", file=os.sys.stderr)
        return 1
    print(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
