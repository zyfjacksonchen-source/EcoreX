from __future__ import annotations

import argparse
import hashlib
import io
import json
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
    release = root / "runtime" / "release"
    release.mkdir(parents=True)
    artifacts = []
    for artifact_id, name in (
        ("bootstrap-windows-x64", "bootstrap-windows-x64.zip"),
        ("bootstrap-macos-arm64", "bootstrap-macos-arm64.zip"),
        ("bootstrap-macos-x64", "bootstrap-macos-x64.zip"),
        ("core-windows-x64", "core-windows-x64.zip"),
    ):
        path = release / name
        path.write_bytes(artifact_id.encode())
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "file_name": name,
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    manifest = release / "release-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": VERSION,
                "release_id": "release-stable-" + "a" * 24,
                "sources": [
                    {
                        "source_id": "github-cn",
                        "kind": "github-cn-mirror",
                        "priority": 0,
                        "base_url": (
                            "https://pub-ada3f610c0234a76838f4e19fe2bb25e.r2.dev/"
                            f"desktop/v{VERSION}"
                        ),
                    }
                ],
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )
    (release / "release-metadata.json").write_bytes(b"{}")
    (release / "sbom.cdx.json").write_bytes(b"{}")
    return argparse.Namespace(
        version=VERSION,
        runtime_root=root / "runtime",
        windows_root=windows,
        macos_arm64_root=arm64,
        macos_x64_root=x64,
        receipt=root / "r2-receipt.json",
        publication_receipt=root / "release-publication-receipt.json",
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


class ReclaimS3(FakeS3):
    def __init__(self) -> None:
        super().__init__()
        self.uploads: list[dict[str, str]] = []
        self.aborted: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def get_paginator(self, operation: str):
        assert operation == "list_multipart_uploads"
        uploads = self.uploads

        class Paginator:
            def paginate(self, **kwargs: object):
                assert kwargs == {"Bucket": "emate-desktop-downloads"}
                return [{"Uploads": list(uploads)}]

        return Paginator()

    def abort_multipart_upload(
        self, *, Bucket: str, Key: str, UploadId: str
    ) -> None:
        assert Bucket == "emate-desktop-downloads"
        self.aborted.append((Key, UploadId))
        self.uploads = [
            item
            for item in self.uploads
            if (item["Key"], item["UploadId"]) != (Key, UploadId)
        ]

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        assert Bucket == "emate-desktop-downloads"
        self.deleted.append(Key)
        self.objects.pop(Key, None)


def _reclaim_inputs(tmp_path: Path, module: dict[str, object]) -> tuple[argparse.Namespace, list[dict[str, object]]]:
    publish_args = _inputs(tmp_path)
    records = [
        {key: value for key, value in record.items() if key not in {"content_type", "source_identity"}}
        for record, _path in module["_records"](publish_args)
    ]
    records.sort(key=lambda item: str(item["key"]))
    admission = {
        "schema_version": 2,
        "document_type": "emate.r2-download-admission",
        "status": "verified",
        "version": VERSION,
        "bucket": "emate-desktop-downloads",
        "public_origin": "https://pub-ada3f610c0234a76838f4e19fe2bb25e.r2.dev",
        "max_parallel_multipart": 3,
        "objects": records,
    }
    admission_bytes = json.dumps(admission, sort_keys=True, separators=(",", ":")).encode()
    admission_path = tmp_path / "r2-download-admission.json"
    admission_path.write_bytes(admission_bytes + b"\n")
    manifest = json.loads(
        (publish_args.runtime_root / "release/release-manifest.json").read_text()
    )
    publication = module["_release_publication_receipt"](manifest, records)
    publication_path = tmp_path / "release-publication-receipt.json"
    publication_path.write_text(
        json.dumps(publication, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    source_sha = "7" * 40
    return (
        argparse.Namespace(
            admission_receipt=admission_path,
            publication_receipt=publication_path,
            expected_admission_sha256=hashlib.sha256(admission_bytes).hexdigest(),
            expected_version=VERSION,
            expected_source_sha=source_sha,
            observed_source_sha=source_sha,
            expected_release_id=publication["release_id"],
        ),
        records,
    )


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

    with pytest.raises(RuntimeError, match=f"r2_upload_failed:{failed}"):
        module["publish"](args, client=client, public_probe=probe)

    assert client.maximum_active == 3
    assert {
        f"e-Mate-{VERSION}-arm64.dmg",
        f"e-Mate-{VERSION}-x64.dmg",
        f"e-Mate-Setup-{VERSION}-x64.exe",
    } <= set(client.uploaded)
    assert not args.receipt.exists()

    first_uploads = list(client.uploaded)
    receipt = module["publish"](args, client=client, public_probe=probe)

    assert failed in client.uploaded[len(first_uploads) :]
    assert receipt["status"] == "verified"
    assert receipt["bucket"] == "emate-desktop-downloads"
    assert receipt["max_parallel_multipart"] == 3
    assert receipt["schema_version"] == 2
    assert {item["target"] for item in receipt["objects"]} >= {
        "bootstrap-windows-x64",
        "bootstrap-macos-arm64",
        "bootstrap-macos-x64",
        "runtime-manifest",
        "runtime-metadata",
        "runtime-sbom",
    }
    assert all(
        item["key"] == f"desktop/v{VERSION}/{item['file_name']}"
        and item["url"].endswith(f"/{item['key']}")
        and item["sha256"]
        == hashlib.sha256(
            next(path for path in (
                args.windows_root / item["file_name"],
                args.macos_arm64_root / item["file_name"],
                args.macos_x64_root / item["file_name"],
                args.runtime_root / "release" / item["file_name"],
            ) if path.exists()).read_bytes()
        ).hexdigest()
        for item in receipt["objects"]
    )
    assert sorted(probes[-len(receipt["objects"]):]) == sorted(
        item["file_name"] for item in receipt["objects"]
    )
    publication = json.loads(args.publication_receipt.read_text(encoding="utf-8"))
    assert publication["release_id"] == "release-stable-" + "a" * 24
    assert publication["publication_policy"] == "stable-primary-only"
    assert set(publication["source_receipts"]) == {"github-cn"}
    assert {item["name"] for item in publication["source_receipts"]["github-cn"]} == {
        "release-manifest.json",
        "release-metadata.json",
        "sbom.cdx.json",
        *(item["file_name"] for item in json.loads(
            (args.runtime_root / "release/release-manifest.json").read_text()
        )["artifacts"]),
    }


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
            "--runtime-root",
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

    assert len(client.uploaded) >= 3
    receipt = module["publish"](args, client=client, public_probe=probe)

    assert client.uploaded.count(f"e-Mate-Setup-{VERSION}-x64.exe.blockmap") == 1
    assert receipt["status"] == "verified"


def test_authenticated_head_after_upload_retries_without_reuploading(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    module["_upload"].__globals__["_transfer_config"] = lambda: SimpleNamespace(
        max_concurrency=1, use_threads=False
    )
    args = _inputs(tmp_path)
    blockmap = f"e-Mate-Setup-{VERSION}-x64.exe.blockmap"

    class TransientHeadS3(FakeS3):
        def __init__(self, failures: int) -> None:
            super().__init__()
            self.failures = failures

        def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
            if Key in self.objects and Key.endswith(blockmap) and self.failures:
                self.failures -= 1
                raise OSError("private endpoint temporarily unavailable")
            return super().head_object(Bucket=Bucket, Key=Key)

    sleeps: list[int] = []
    monkeypatch.setattr(module["time"], "sleep", sleeps.append)
    client = TransientHeadS3(failures=4)

    receipt = module["publish"](args, client=client, public_probe=lambda *_: None)

    assert receipt["status"] == "verified"
    assert client.uploaded.count(blockmap) == 1
    assert sleeps == [1, 2, 4, 8]

    exhausted = TransientHeadS3(failures=5)
    sleeps.clear()
    with pytest.raises(
        RuntimeError,
        match=f"^r2_authenticated_head_failed:{blockmap}$",
    ):
        module["publish"](args, client=exhausted, public_probe=lambda *_: None)

    assert exhausted.uploaded.count(blockmap) == 1
    assert sleeps == [1, 2, 4, 8]


def test_upload_exception_after_committed_object_is_admitted_without_reuploading(
    tmp_path: Path,
) -> None:
    module = _module()
    module["_upload"].__globals__["_transfer_config"] = lambda: SimpleNamespace(
        max_concurrency=1, use_threads=False
    )
    record, path = module["_records"](_inputs(tmp_path))[3]

    class CommittedThenFailedS3(FakeS3):
        def upload_fileobj(
            self,
            source: io.BufferedReader,
            bucket: str,
            key: str,
            *,
            ExtraArgs: dict[str, object],
            Config: object,
        ) -> None:
            self.uploaded.append(key.rsplit("/", 1)[-1])
            self.objects[key] = (source.read(), dict(ExtraArgs["Metadata"]))
            raise OSError("multipart completion response was lost")

    client = CommittedThenFailedS3()

    module["_upload"](client, record, path)

    assert client.uploaded == [record["file_name"]]


def test_upload_exception_without_matching_object_fails_safely(
    tmp_path: Path,
) -> None:
    module = _module()
    module["_upload"].__globals__["_transfer_config"] = lambda: SimpleNamespace(
        max_concurrency=1, use_threads=False
    )
    record, path = module["_records"](_inputs(tmp_path))[3]

    class FailedS3(FakeS3):
        def upload_fileobj(self, source, bucket, key, **kwargs) -> None:
            self.uploaded.append(key.rsplit("/", 1)[-1])
            self.objects[key] = (b"partial object", {"sha256": "0" * 64})
            raise OSError("multipart completion failed with a mismatched object")

    client = FailedS3()

    with pytest.raises(
        RuntimeError,
        match=f"^r2_upload_failed:{record['file_name']}$",
    ):
        module["_upload"](client, record, path)

    assert client.uploaded == [record["file_name"]]


def test_existing_immutable_key_mismatch_is_not_overwritten(tmp_path: Path) -> None:
    module = _module()
    module["_upload"].__globals__["_transfer_config"] = lambda: SimpleNamespace(
        max_concurrency=1, use_threads=False
    )
    record, path = module["_records"](_inputs(tmp_path))[3]
    client = FakeS3()
    client.objects[str(record["key"])] = (
        b"different immutable object",
        {"sha256": "0" * 64},
    )

    with pytest.raises(
        RuntimeError,
        match=f"^r2_object_collision:{record['file_name']}$",
    ):
        module["_upload"](client, record, path)

    assert client.uploaded == []


def test_reclaim_rejected_candidate_deletes_only_exact_admitted_keys(
    tmp_path: Path,
) -> None:
    module = _module()
    args, records = _reclaim_inputs(tmp_path, module)
    client = ReclaimS3()
    for record in records:
        client.objects[str(record["key"])] = (
            b"x" * int(record["size_bytes"]),
            {"sha256": str(record["sha256"])},
        )
    first_key = str(records[0]["key"])
    neighbor = first_key + ".keep"
    client.uploads = [
        {"Key": first_key, "UploadId": "candidate-upload"},
        {"Key": neighbor, "UploadId": "neighbor-upload"},
        {"Key": "desktop/v2.0.4/keep.exe", "UploadId": "other-upload"},
    ]
    references: list[tuple[int, str, str]] = []
    public_missing: list[str] = []

    def reference_probe(identities: set[str]) -> None:
        references.append((len(records), args.expected_source_sha, args.expected_release_id))
        assert first_key in identities
        assert str(records[0]["url"]) in identities

    result = module["reclaim_rejected"](
        args,
        client=client,
        reference_probe=reference_probe,
        public_missing_probe=lambda record: public_missing.append(str(record["key"])),
    )

    expected_keys = [str(record["key"]) for record in records]
    assert references == [(len(records), args.expected_source_sha, args.expected_release_id)]
    assert client.aborted == [(first_key, "candidate-upload")]
    assert client.deleted == expected_keys
    assert public_missing == expected_keys
    assert neighbor not in client.deleted
    assert result == {"status": "reclaimed", "objects_reclaimed": len(records)}


@pytest.mark.parametrize("drift", ["remote", "live", "identity"])
def test_reclaim_drift_fails_before_any_mutation(tmp_path: Path, drift: str) -> None:
    module = _module()
    args, records = _reclaim_inputs(tmp_path, module)
    client = ReclaimS3()
    for record in records:
        client.objects[str(record["key"])] = (
            b"x" * int(record["size_bytes"]),
            {"sha256": str(record["sha256"])},
        )
    if drift == "remote":
        client.objects[str(records[-1]["key"])] = (b"wrong", {"sha256": "0" * 64})
    if drift == "identity":
        args.observed_source_sha = "8" * 40

    def reference_probe(*_args: object) -> None:
        if drift == "live":
            raise RuntimeError("r2_reclaim_live_reference")

    with pytest.raises(RuntimeError, match="^r2_reclaim_"):
        module["reclaim_rejected"](
            args,
            client=client,
            reference_probe=reference_probe,
            public_missing_probe=lambda _record: None,
        )

    assert client.aborted == []
    assert client.deleted == []


def test_reclaim_resumes_after_an_already_deleted_object(tmp_path: Path) -> None:
    module = _module()
    args, records = _reclaim_inputs(tmp_path, module)
    client = ReclaimS3()
    for record in records[1:]:
        client.objects[str(record["key"])] = (
            b"x" * int(record["size_bytes"]),
            {"sha256": str(record["sha256"])},
        )
    missing: list[str] = []
    kwargs = {
        "client": client,
        "reference_probe": lambda _identities: None,
        "public_missing_probe": lambda record: missing.append(str(record["key"])),
    }

    module["reclaim_rejected"](args, **kwargs)
    first_deleted = list(client.deleted)
    module["reclaim_rejected"](args, **kwargs)

    assert str(records[0]["key"]) not in first_deleted
    assert first_deleted == [str(record["key"]) for record in records[1:]]
    assert client.deleted == first_deleted
    assert missing.count(str(records[0]["key"])) == 2


def test_live_reference_gate_checks_same_origin_current_and_all_pointer_files() -> None:
    module = _module()
    requested: list[str] = []

    def open_request(request, *, timeout: int):
        assert timeout == 30
        requested.append(request.full_url)
        if request.full_url.endswith("latest-mac.yml"):
            raise HTTPError(request.full_url, 404, "missing", {}, None)
        return _Response(200, {"Content-Length": "7"}, b"current")

    module["_verify_no_live_references"](
        {"release-stable-" + "a" * 24, "7" * 40, "https://r2/rejected.exe"},
        opener=open_request,
    )

    assert requested == list(module["_LIVE_REFERENCE_URLS"])

    def leaked(request, *, timeout: int):
        return _Response(
            200,
            {},
            b"https://r2/rejected.exe",
        )

    with pytest.raises(RuntimeError, match="^r2_reclaim_live_reference$"):
        module["_verify_no_live_references"](
            {"release-stable-" + "a" * 24, "7" * 40, "https://r2/rejected.exe"},
            opener=leaked,
        )


def test_public_reclaim_readback_requires_404(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module["time"], "sleep", lambda _seconds: None)
    record = {"file_name": "rejected.exe", "url": "https://r2/rejected.exe"}

    def missing(request, *, timeout: int):
        assert request.headers["Cache-control"] == "no-cache"
        raise HTTPError(request.full_url, 404, "missing", {}, None)

    module["_verify_public_missing"](record, opener=missing)

    with pytest.raises(RuntimeError, match="^r2_reclaim_public_readback_failed:rejected.exe$"):
        module["_verify_public_missing"](
            record,
            opener=lambda *_args, **_kwargs: _Response(200, {}),
        )


def test_release_workflow_exposes_reclaim_without_new_secrets() -> None:
    workflow = (ROOT / ".github/workflows/emate-2.0-desktop-release.yml").read_text()
    reclaim = workflow.split("reclaim-rejected-r2:", 1)[1]

    assert "reclaim-rejected-r2:" in workflow
    assert "actions: read" in workflow
    assert "reclaim-rejected" in workflow
    assert "actions/setup-python@" in reclaim
    assert 'python-version: "3.11.9"' in reclaim
    assert "--expected-admission-sha256" in workflow
    assert "--observed-source-sha" in workflow
    assert workflow.count("ECOREX_R2_ACCESS_KEY_ID: ${{ secrets.ECOREX_R2_ACCESS_KEY_ID }}") >= 2
