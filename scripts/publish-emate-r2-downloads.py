#!/usr/bin/env python3
"""Upload and verify the immutable e-Mate desktop downloads in Cloudflare R2."""

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
from urllib.request import Request, urlopen


BUCKET = "emate-desktop-downloads"
PUBLIC_ORIGIN = "https://pub-ada3f610c0234a76838f4e19fe2bb25e.r2.dev"
_VERSION = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--windows-root", required=True, type=Path)
    parser.add_argument("--macos-arm64-root", required=True, type=Path)
    parser.add_argument("--macos-x64-root", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
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


def _remote_matches(client: object, record: Mapping[str, Any]) -> bool:
    try:
        value = client.head_object(Bucket=BUCKET, Key=record["key"])
    except Exception as error:
        response = getattr(error, "response", None)
        if isinstance(response, dict) and str(
            response.get("Error", {}).get("Code")
        ) in {
            "404",
            "NoSuchKey",
            "NotFound",
        }:
            return False
        raise
    return value.get("ContentLength") == record["size_bytes"] and value.get(
        "Metadata"
    ) == {"sha256": record["sha256"]}


def _upload(client: object, record: Mapping[str, Any], path: Path) -> None:
    current = path.lstat()
    if (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ) != tuple(record["source_identity"]):
        raise RuntimeError(f"r2_artifact_changed:{record['file_name']}")
    if not _remote_matches(client, record):
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ) != tuple(record["source_identity"]):
                raise RuntimeError(f"r2_artifact_changed:{record['file_name']}")
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
            after = os.fstat(source.fileno())
            if (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) != tuple(record["source_identity"]):
                raise RuntimeError(f"r2_artifact_changed:{record['file_name']}")
    if not _remote_matches(client, record):
        raise RuntimeError(f"r2_authenticated_head_failed:{record['file_name']}")


def _verify_public_once(
    record: Mapping[str, Any],
    path: Path,
    *,
    opener: Callable[..., Any],
) -> None:
    with opener(Request(str(record["url"]), method="HEAD"), timeout=30) as response:
        if (
            response.status != 200
            or response.headers.get("Content-Length") != str(record["size_bytes"])
            or response.headers.get("Accept-Ranges", "").casefold() != "bytes"
        ):
            raise RuntimeError(f"r2_public_head_failed:{record['file_name']}")
    size = int(record["size_bytes"])
    spans = ((0, min(15, size - 1)), (max(0, size - 16), size - 1))
    with path.open("rb") as source:
        for start, end in spans:
            request = Request(
                str(record["url"]), headers={"Range": f"bytes={start}-{end}"}
            )
            with opener(request, timeout=30) as response:
                payload = response.read(end - start + 2)
                if (
                    response.status != 206
                    or response.headers.get("Content-Range")
                    != f"bytes {start}-{end}/{size}"
                    or len(payload) != end - start + 1
                ):
                    raise RuntimeError(f"r2_public_range_failed:{record['file_name']}")
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
            for record, path in records[:3]
        }
        for future in as_completed(futures):
            try:
                completed.append(future.result())
            except Exception as error:
                errors.append(error)
    if errors:
        raise errors[0]
    completed.append(_publish_one(client, *records[3], public_probe))
    completed.sort(key=lambda item: str(item["key"]))
    receipt = {
        "schema_version": 1,
        "document_type": "emate.r2-download-admission",
        "status": "verified",
        "version": args.version,
        "bucket": BUCKET,
        "public_origin": PUBLIC_ORIGIN,
        "max_parallel_multipart": 3,
        "objects": completed,
    }
    _write_receipt(args.receipt, receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    try:
        receipt = publish(_parser().parse_args(argv))
    except Exception as error:
        category = (
            str(error)
            if isinstance(error, RuntimeError)
            and re.fullmatch(r"r2_[a-z_]+(?::[A-Za-z0-9._-]+)?", str(error))
            else "r2_operation_failed"
        )
        print(f"emate_r2_publish_failed:{category}", file=os.sys.stderr)
        return 1
    print(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
