from __future__ import annotations

import argparse
import hashlib
import io
from pathlib import Path
import runpy
from threading import Event, Lock
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "publish-emate-r2-downloads.py"
VERSION = "2.0.5"


def _module() -> dict[str, object]:
    return runpy.run_path(str(SCRIPT))


def _inputs(root: Path) -> argparse.Namespace:
    windows = root / "windows-x64"
    arm64 = root / "macos-arm64"
    x64 = root / "macos-x64"
    for path in (windows, arm64, x64):
        path.mkdir()
    (windows / f"e-Mate-Setup-{VERSION}-x64.exe").write_bytes(b"windows-package")
    (windows / f"e-Mate-Setup-{VERSION}-x64.exe.blockmap").write_bytes(
        b"windows-blockmap"
    )
    (arm64 / f"e-Mate-{VERSION}-arm64.dmg").write_bytes(b"mac-arm64")
    (x64 / f"e-Mate-{VERSION}-x64.dmg").write_bytes(b"mac-x64")
    return argparse.Namespace(
        version=VERSION,
        windows_root=windows,
        macos_arm64_root=arm64,
        macos_x64_root=x64,
        receipt=root / "r2-receipt.json",
    )


class _Missing(Exception):
    response = {"Error": {"Code": "404", "Message": "missing"}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.uploaded: list[str] = []
        self.fail_once: set[str] = set()
        self.active = 0
        self.maximum_active = 0
        self._lock = Lock()
        self._three_started = Event()

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        assert Bucket == "emate-desktop-downloads"
        try:
            payload, metadata = self.objects[Key]
        except KeyError:
            raise _Missing(Key) from None
        return {"ContentLength": len(payload), "Metadata": metadata}

    def upload_fileobj(
        self,
        source: io.BufferedReader,
        bucket: str,
        key: str,
        *,
        ExtraArgs: dict[str, object],
        Config: object,
    ) -> None:
        assert bucket == "emate-desktop-downloads"
        assert getattr(Config, "max_concurrency") == 1
        assert getattr(Config, "use_threads") is False
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.active == 3:
                self._three_started.set()
        self._three_started.wait(timeout=2)
        try:
            name = key.rsplit("/", 1)[-1]
            self.uploaded.append(name)
            if name in self.fail_once:
                self.fail_once.remove(name)
                raise RuntimeError(f"failed:{name}")
            self.objects[key] = (
                source.read(),
                dict(ExtraArgs["Metadata"]),
            )
        finally:
            with self._lock:
                self.active -= 1


def test_r2_publisher_runs_three_independent_multipart_uploads_and_resumes(
    tmp_path: Path,
) -> None:
    module = _module()
    module["_upload"].__globals__["_transfer_config"] = lambda: SimpleNamespace(
        max_concurrency=1, use_threads=False
    )
    args = _inputs(tmp_path)
    client = FakeS3()
    failed = f"e-Mate-{VERSION}-x64.dmg"
    client.fail_once.add(failed)
    probes: list[str] = []

    def probe(record: dict[str, object], path: Path) -> None:
        payload, metadata = client.objects[str(record["key"])]
        assert payload == path.read_bytes()
        assert metadata == {"sha256": record["sha256"]}
        probes.append(path.name)

    with pytest.raises(RuntimeError, match=f"failed:{failed}"):
        module["publish"](args, client=client, public_probe=probe)

    assert client.maximum_active == 3
    assert sorted(client.uploaded) == [
        f"e-Mate-{VERSION}-arm64.dmg",
        f"e-Mate-{VERSION}-x64.dmg",
        f"e-Mate-Setup-{VERSION}-x64.exe",
    ]
    assert not args.receipt.exists()

    first_uploads = list(client.uploaded)
    receipt = module["publish"](args, client=client, public_probe=probe)

    assert client.uploaded[len(first_uploads) :] == [
        failed,
        f"e-Mate-Setup-{VERSION}-x64.exe.blockmap",
    ]
    assert receipt["status"] == "verified"
    assert receipt["bucket"] == "emate-desktop-downloads"
    assert receipt["max_parallel_multipart"] == 3
    assert len(receipt["objects"]) == 4
    assert all(
        item["key"] == f"desktop/v{VERSION}/{item['file_name']}"
        and item["url"].endswith(f"/{item['key']}")
        and item["sha256"]
        == hashlib.sha256(
            next(
                path
                for path in (
                    args.windows_root,
                    args.macos_arm64_root,
                    args.macos_x64_root,
                )
                if (path / item["file_name"]).exists()
            )
            .joinpath(item["file_name"])
            .read_bytes()
        ).hexdigest()
        for item in receipt["objects"]
    )
    assert sorted(probes[-4:]) == sorted(
        item["file_name"] for item in receipt["objects"]
    )


def test_r2_client_reads_credentials_only_from_environment(monkeypatch) -> None:
    module = _module()
    captured: dict[str, object] = {}

    def client(service: str, **kwargs: object) -> object:
        captured.update(service=service, **kwargs)
        return object()

    class RetryConfig:
        def __init__(self, *, retries: dict[str, object]) -> None:
            self.retries = retries

    result = module["_s3_client"](
        {
            "ECOREX_R2_ACCOUNT_ID": "account",
            "ECOREX_R2_ACCESS_KEY_ID": "access-secret",
            "ECOREX_R2_SECRET_ACCESS_KEY": "private-secret",
        },
        client_factory=client,
        config_factory=RetryConfig,
    )

    assert result is not None
    assert captured["service"] == "s3"
    assert captured["endpoint_url"] == "https://account.r2.cloudflarestorage.com"
    assert captured["aws_access_key_id"] == "access-secret"
    assert captured["aws_secret_access_key"] == "private-secret"
    assert captured["config"].retries == {"mode": "standard", "total_max_attempts": 5}


def test_cli_never_prints_remote_errors_or_credentials(capsys) -> None:
    module = _module()

    def fail(_args):
        raise ValueError("Access key access-secret rejected at private endpoint")

    module["main"].__globals__["publish"] = fail
    assert module["main"](
        [
            "--version",
            VERSION,
            "--windows-root",
            ".",
            "--macos-arm64-root",
            ".",
            "--macos-x64-root",
            ".",
            "--receipt",
            "receipt.json",
        ]
    ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "emate_r2_publish_failed:r2_operation_failed\n"


class _Response:
    def __init__(self, status: int, headers: dict[str, str], payload: bytes = b""):
        self.status = status
        self.headers = headers
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        return self._payload if amount < 0 else self._payload[:amount]


def test_public_gate_checks_head_size_and_exact_first_and_last_ranges(
    tmp_path: Path,
) -> None:
    module = _module()
    artifact = tmp_path / "artifact.exe"
    artifact.write_bytes(bytes(range(64)))
    record = {
        "url": "https://public.example/desktop/v2.0.5/artifact.exe",
        "size_bytes": 64,
    }
    ranges: list[str] = []

    def open_request(request, *, timeout: int):
        assert timeout == 30
        assert request.headers["User-agent"] == "e-Mate-Desktop-Publisher/1.0"
        assert request.headers["Accept"] == "*/*"
        if request.get_method() == "HEAD":
            return _Response(200, {"Content-Length": "64", "Accept-Ranges": "bytes"})
        requested = request.headers["Range"]
        ranges.append(requested)
        start, end = (int(value) for value in requested[6:].split("-"))
        return _Response(
            206,
            {
                "Content-Length": str(end - start + 1),
                "Content-Range": f"bytes {start}-{end}/64",
            },
            artifact.read_bytes()[start : end + 1],
        )

    module["_verify_public"](record, artifact, opener=open_request)

    assert ranges == ["bytes=0-15", "bytes=48-63"]


def test_public_head_http_error_keeps_safe_artifact_category(tmp_path: Path) -> None:
    module = _module()
    artifact = tmp_path / "artifact.exe"
    artifact.write_bytes(b"artifact")
    record = {
        "file_name": "artifact.exe",
        "url": "https://access-secret@private.example/artifact.exe",
        "size_bytes": artifact.stat().st_size,
    }

    def denied(request, *, timeout: int):
        raise HTTPError(request.full_url, 403, "access-secret", {}, None)

    with pytest.raises(RuntimeError) as caught:
        module["_verify_public_once"](record, artifact, opener=denied)

    assert str(caught.value) == "r2_public_head_failed:artifact.exe"
    assert "access-secret" not in str(caught.value)


def test_public_probe_failure_rerun_skips_three_uploaded_packages_and_adds_blockmap(
    tmp_path: Path,
) -> None:
    module = _module()
    module["_upload"].__globals__["_transfer_config"] = lambda: SimpleNamespace(
        max_concurrency=1, use_threads=False
    )
    args = _inputs(tmp_path)
    client = FakeS3()
    fail_once = Event()

    def probe(record: dict[str, object], _path: Path) -> None:
        if record["target"] == "windows-x64" and not fail_once.is_set():
            fail_once.set()
            raise RuntimeError(
                f"r2_public_head_failed:{record['file_name']}"
            )

    with pytest.raises(RuntimeError, match="r2_public_head_failed"):
        module["publish"](args, client=client, public_probe=probe)

    assert len(client.uploaded) == 3
    receipt = module["publish"](args, client=client, public_probe=probe)

    assert client.uploaded[3:] == [
        f"e-Mate-Setup-{VERSION}-x64.exe.blockmap"
    ]
    assert receipt["status"] == "verified"
