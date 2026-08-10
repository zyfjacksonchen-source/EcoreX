from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading

import pytest

from ecorex import __version__

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deploy-emate-desktop-feed.py"
VERSION = __version__
COMMIT = "a" * 40
RELEASE_ID = "release-stable-test"
BUILD_DIGEST = hashlib.sha256(b"build").hexdigest()
MANIFEST = b'{"release":"test"}\n'
MANIFEST_SHA256 = hashlib.sha256(MANIFEST).hexdigest()
OLD_TARGET = "releases/v1.9.9-0123456789abcdef"


def _record(root: Path, relative: str, role: str, source: str) -> dict[str, object]:
    payload = (root / relative).read_bytes()
    return {
        "path": relative,
        "role": role,
        "source_artifact": source,
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _feed(
    tmp_path: Path, *, previous: bool = True, unsigned_manual: bool = False
) -> tuple[Path, Path, dict[str, object]]:
    root = tmp_path / "e-mate-update"
    releases = root / "releases"
    staging = releases / "staging"
    staging.mkdir(parents=True)
    if previous:
        (root / OLD_TARGET).mkdir()
        (root / OLD_TARGET / "download-index.json").write_bytes(b'{"version":"1.9.9"}\n')
        os.symlink(OLD_TARGET, root / "current")

    files = {
        "download-index.json": json.dumps({
            "schema_version": 2 if unsigned_manual else 1,
            "version": VERSION,
            "distribution_mode": "unsigned-manual" if unsigned_manual else "signed-automatic",
        }).encode() + b"\n",
        f"runtime/{RELEASE_ID}/release-manifest.json": MANIFEST,
        f"e-Mate-Setup-{VERSION}-x64.exe": b"installer",
    }
    if not unsigned_manual:
        files.update({
            "latest.yml": f"version: {VERSION}\n".encode(),
            "latest-mac.yml": f"version: {VERSION}\n".encode(),
            "public-bootstrap-index.json": b'{"schema":"public-bootstrap"}\n',
        })
    for relative, payload in files.items():
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    records = sorted(
        [
            _record(staging, "download-index.json", "pointer", "runtime"),
            _record(
                staging,
                f"runtime/{RELEASE_ID}/release-manifest.json",
                "immutable-runtime",
                "runtime",
            ),
            _record(
                staging,
                f"e-Mate-Setup-{VERSION}-x64.exe",
                "immutable-desktop",
                "windows-x64",
            ),
            *([] if unsigned_manual else [
                _record(staging, "latest.yml", "pointer", "windows-x64"),
                _record(staging, "latest-mac.yml", "pointer", "macos-arm64"),
            ]),
            *([] if unsigned_manual else [_record(
                staging, "public-bootstrap-index.json", "pointer", "runtime-publication"
            )]),
        ],
        key=lambda item: str(item["path"]),
    )
    build_id = hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    target = f"releases/v{VERSION}-{build_id[:16]}"
    candidate = root / target
    staging.rename(candidate)
    receipt: dict[str, object] = {
        "schema_version": 2 if unsigned_manual else 1,
        "document_type": "emate.desktop-feed-stage",
        **({"distribution_mode": "unsigned-manual"} if unsigned_manual else {}),
        "status": "activation-ready-unsigned-manual" if unsigned_manual else "activation-ready",
        "version": VERSION,
        "source_commit": COMMIT,
        "release_id": RELEASE_ID,
        "build_digest": BUILD_DIGEST,
        "runtime_manifest_sha256": MANIFEST_SHA256,
        "feed_build_id": build_id,
        "candidate_target": target,
        "nginx_config_sha256": hashlib.sha256(b"nginx").hexdigest(),
        "files": records,
        "activation": {
            "strategy": "same-filesystem-current-symlink-rename",
            "allowed_operations": ["activate", "rollback"],
            "link": "/srv/e-mate-update/current",
            "pointer_files": ["download-index.json"] if unsigned_manual else [
                "latest.yml", "latest-mac.yml", "download-index.json",
                "public-bootstrap-index.json",
            ],
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
    (candidate / "feed-stage-receipt.json").write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return root, candidate, receipt


def _command(root: Path, candidate: Path, receipt_path: Path) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "--root",
        str(root),
        "--candidate",
        str(candidate),
        "--activation-receipt",
        str(receipt_path),
        "--expected-version",
        VERSION,
        "--expected-source-sha",
        COMMIT,
        "--expected-release-id",
        RELEASE_ID,
        "--expected-build-digest",
        BUILD_DIGEST,
        "--expected-manifest-sha256",
        MANIFEST_SHA256,
    ]


def test_activation_switches_relative_current_and_writes_seven_field_receipt(
    tmp_path: Path,
) -> None:
    root, candidate, stage = _feed(tmp_path)
    output = root / "activation-receipts" / "activate.json"
    result = subprocess.run(
        [
            *_command(root, candidate, output),
            "--readback-command",
            "/bin/cat",
            "--readback-argument",
            str(root / "current" / "public-bootstrap-index.json"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert os.readlink(root / "current") == stage["candidate_target"]
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert list(receipt) == [
        "operation",
        "feed_build_id",
        "previous_target",
        "new_target",
        "manifest_sha256",
        "public_readback_sha256",
        "completed_at",
    ]
    assert receipt == json.loads(result.stdout)
    assert receipt["operation"] == "activate"
    assert receipt["previous_target"] == OLD_TARGET
    assert receipt["new_target"] == stage["candidate_target"]
    assert receipt["public_readback_sha256"] == hashlib.sha256(
        (candidate / "public-bootstrap-index.json").read_bytes()
    ).hexdigest()
    assert output.stat().st_mode & 0o777 == 0o600


def test_unsigned_manual_activation_reads_back_only_download_index(tmp_path: Path) -> None:
    root, candidate, stage = _feed(tmp_path, unsigned_manual=True)
    output = root / "activation-receipts" / "manual.json"
    result = subprocess.run(
        [
            *_command(root, candidate, output),
            "--readback-command", "/bin/cat",
            "--readback-argument", str(root / "current/download-index.json"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert os.readlink(root / "current") == stage["candidate_target"]
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["public_readback_sha256"] == hashlib.sha256(
        (candidate / "download-index.json").read_bytes()
    ).hexdigest()


def test_success_receipt_failure_rolls_back_and_records_compensation(
    tmp_path: Path, monkeypatch
) -> None:
    root, candidate, _stage = _feed(tmp_path, unsigned_manual=True)
    output = root / "activation-receipts" / "compensated.json"
    module = runpy.run_path(str(SCRIPT))
    activate = module["activate"]
    args = module["_parser"]().parse_args([
        *_command(root, candidate, output)[2:],
        "--readback-command", "/bin/cat",
        "--readback-argument", str(root / "current/download-index.json"),
    ])
    original = activate.__globals__["_write_receipt"]
    calls = 0

    def fail_once(path, value):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected receipt failure")
        original(path, value)

    monkeypatch.setitem(activate.__globals__, "_write_receipt", fail_once)

    with pytest.raises(module["FeedDeployError"], match="activation_receipt_failed_rolled_back"):
        activate(args)

    assert os.readlink(root / "current") == OLD_TARGET
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["operation"] == "rollback"
    assert receipt["previous_target"] == _stage["candidate_target"]
    assert receipt["new_target"] == OLD_TARGET


def test_post_switch_installer_drift_rolls_back_and_records_compensation(
    tmp_path: Path, monkeypatch
) -> None:
    root, candidate, stage = _feed(tmp_path, unsigned_manual=True)
    output = root / "activation-receipts" / "drift.json"
    module = runpy.run_path(str(SCRIPT))
    activate = module["activate"]
    args = module["_parser"]().parse_args([
        *_command(root, candidate, output)[2:],
        "--readback-command", "/bin/cat",
        "--readback-argument", str(root / "current/download-index.json"),
    ])
    original = activate.__globals__["_validate_inventory"]
    calls = 0

    def drift_on_live_reverify(path, receipt, device):
        nonlocal calls
        calls += 1
        if calls == 2:
            (candidate / f"e-Mate-Setup-{VERSION}-x64.exe").write_bytes(b"drift")
        return original(path, receipt, device)

    monkeypatch.setitem(activate.__globals__, "_validate_inventory", drift_on_live_reverify)

    with pytest.raises(module["FeedDeployError"], match="post_switch_verification_failed_rolled_back"):
        activate(args)

    assert os.readlink(root / "current") == OLD_TARGET
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["operation"] == "rollback"
    assert receipt["previous_target"] == stage["candidate_target"]
    assert receipt["new_target"] == OLD_TARGET


def test_activation_accepts_exact_loopback_http_readback(tmp_path: Path) -> None:
    root, candidate, stage = _feed(tmp_path)
    output = root / "activation-receipts" / "http.json"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = (root / "current" / "public-bootstrap-index.json").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                *_command(root, candidate, output),
                "--readback-url",
                f"http://127.0.0.1:{server.server_port}/public-bootstrap-index.json",
                "--readback-host",
                "127.0.0.1",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert result.returncode == 0, result.stderr
    assert os.readlink(root / "current") == stage["candidate_target"]
    assert json.loads(output.read_text(encoding="utf-8"))["operation"] == "activate"


@pytest.mark.parametrize("previous", [True, False])
def test_failed_readback_atomically_restores_previous(
    tmp_path: Path, previous: bool
) -> None:
    root, candidate, stage = _feed(tmp_path, previous=previous)
    output = root / "activation-receipts" / "rollback.json"
    result = subprocess.run(
        [
            *_command(root, candidate, output),
            "--readback-command",
            "/usr/bin/false",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "readback_failed_rolled_back" in result.stderr
    if previous:
        assert os.readlink(root / "current") == OLD_TARGET
    else:
        assert not os.path.lexists(root / "current")
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert len(receipt) == 7
    assert receipt["operation"] == "rollback"
    assert receipt["previous_target"] == stage["candidate_target"]
    assert receipt["new_target"] == (OLD_TARGET if previous else None)
    assert receipt["public_readback_sha256"] == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("mutation", ["status", "tamper", "extra"])
def test_activation_rejects_unready_or_incomplete_candidate(
    tmp_path: Path, mutation: str
) -> None:
    root, candidate, stage = _feed(tmp_path)
    if mutation == "status":
        stage["status"] = "awaiting-public-bootstrap-index"
        (candidate / "feed-stage-receipt.json").write_text(
            json.dumps(stage, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    elif mutation == "tamper":
        (candidate / "latest.yml").write_bytes(b"tampered")
    else:
        (candidate / "unreceipted.txt").write_bytes(b"extra")
    output = root / "activation-receipts" / "rejected.json"

    result = subprocess.run(
        [
            *_command(root, candidate, output),
            "--readback-command",
            "/bin/cat",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert os.readlink(root / "current") == OLD_TARGET
    assert not output.exists()


def test_activation_rejects_symlink_candidate(tmp_path: Path) -> None:
    root, candidate, _stage = _feed(tmp_path)
    real = candidate.with_name(candidate.name + "-real")
    candidate.rename(real)
    candidate.symlink_to(real, target_is_directory=True)
    output = root / "activation-receipts" / "rejected.json"

    result = subprocess.run(
        [
            *_command(root, candidate, output),
            "--readback-command",
            "/bin/cat",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "candidate_invalid" in result.stderr
    assert os.readlink(root / "current") == OLD_TARGET
    assert not output.exists()
