from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import threading

import pytest


def _drill_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "drill_v1_windows_signed_candidate.py"
    spec = importlib.util.spec_from_file_location("ecorex_windows_candidate_drill", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_webui_readiness_uses_injected_bearer_without_exposing_it() -> None:
    drill = _drill_module()
    bearer = "runtime-bearer-only-in-memory-1234567890"
    observations: list[dict[str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            observations.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "origin": self.headers.get("Origin"),
                }
            )
            if self.path == "/":
                configuration = json.dumps(
                    {
                        "apiBase": "/api/v1",
                        "bearerToken": bearer,
                        "releaseId": "release-stable-test",
                        "version": "1.0.0",
                    },
                    separators=(",", ":"),
                )
                payload = (
                    "<!doctype html><script>"
                    f"window.__ECOREX_RUNTIME__=Object.freeze({configuration});"
                    'Object.defineProperty(window,"__ECOREX_RUNTIME__",{});'
                    "</script>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path == "/api/v1/bootstrap":
                authorized = self.headers.get("Authorization") == f"Bearer {bearer}"
                payload = b"{}"
                self.send_response(200 if authorized else 401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert drill._get_bootstrap(server.server_port, 2.0) == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert observations == [
        {"path": "/", "authorization": None, "origin": None},
        {
            "path": "/api/v1/bootstrap",
            "authorization": f"Bearer {bearer}",
            "origin": f"http://127.0.0.1:{server.server_port}",
        },
    ]
    assert bearer not in repr({"status": 200})


def test_candidate_assembly_rejects_mutable_python_bytecode(tmp_path: Path) -> None:
    drill = _drill_module()
    core = tmp_path / "core"
    core.mkdir()
    drill._assert_no_runtime_bytecode(core)
    cache = core / "package" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.cpython-311.pyc").write_bytes(b"mutable")
    with pytest.raises(drill.DrillError, match="mutable Python bytecode"):
        drill._assert_no_runtime_bytecode(core)


def test_local_runtime_config_uses_distinct_release_and_rollback_trust_roles() -> None:
    drill = _drill_module()
    release = drill.Ed25519PrivateKey.generate()
    rollback = drill.Ed25519PrivateKey.generate()
    session = drill.Ed25519PrivateKey.generate()

    payload = drill._runtime_config(
        drill._public_key(release),
        drill._public_key(rollback),
        drill._public_key(session),
    )
    parsed = drill.ProductRuntimeConfig.from_bytes(payload)

    assert tuple(parsed.release_public_keys) == (drill.SIGNING_KEY_ID,)
    assert tuple(parsed.rollback_public_keys) == (drill.ROLLBACK_KEY_ID,)
    assert tuple(parsed.session_public_keys) == (drill.SESSION_KEY_ID,)
    assert parsed.release_public_keys[drill.SIGNING_KEY_ID] != (
        parsed.rollback_public_keys[drill.ROLLBACK_KEY_ID]
    )


def test_platform_stage_failure_keeps_only_a_bounded_public_code(
    tmp_path: Path,
) -> None:
    drill = _drill_module()
    output = tmp_path / "stage"
    output.mkdir()
    (output / "stage-failure.json").write_text(
        json.dumps({"status": "failed", "code": "office_format_probe_failed"}),
        encoding="utf-8",
    )
    assert drill._platform_stage_failure_code(
        output,
        b'{"code":"fallback_probe_failed","secret":"must-not-surface"}\n',
    ) == "office_format_probe_failed"

    (output / "stage-failure.json").write_text(
        json.dumps({"status": "failed", "code": "BAD secret value"}),
        encoding="utf-8",
    )
    assert drill._platform_stage_failure_code(
        output,
        b'{"code":"safe_fallback_failed","detail":"not returned"}\n',
    ) == "safe_fallback_failed"
    assert drill._platform_stage_failure_code(output, b"not-json") == (
        "platform_stage_failed"
    )


def test_fault_candidate_excludes_only_cache_and_runtime_site_packages() -> None:
    drill = _drill_module()
    ignored = drill._fault_candidate_ignore(
        str(Path("payload") / "bin" / "Lib"),
        ["site-packages", "encodings", "__pycache__", "module.pyc"],
    )
    assert ignored == {"site-packages", "__pycache__", "module.pyc"}


def test_private_key_scan_is_streaming_and_catches_chunk_boundary(
    tmp_path: Path,
) -> None:
    drill = _drill_module()
    secret = b"s" * 32
    payload = tmp_path / "large-artifact.bin"
    payload.write_bytes(b"a" * (1024 * 1024 - 16) + secret + b"z" * (1024 * 1024))
    with pytest.raises(drill.DrillError, match="private signing key"):
        drill._assert_secret_not_persisted(tmp_path, (secret,))


def test_native_helper_identity_requires_matching_helpers_and_v2_authority(
    tmp_path: Path,
) -> None:
    drill = _drill_module()
    core = tmp_path / "stages/windows-x64/core/bin/ecorex-sandbox-host.exe"
    bootstrap = (
        tmp_path / "stages/windows-x64/bootstrap/bin/ecorex-sandbox-host.exe"
    )
    core.parent.mkdir(parents=True)
    bootstrap.parent.mkdir(parents=True)
    core.write_bytes(b"same-current-native-helper")
    bootstrap.write_bytes(core.read_bytes())
    digest = drill._sha256_file(core)
    receipt = {
        "schema_version": 2,
        "status": "passed",
        "target": "windows-x64",
        "authority_mode": "caller-pinned",
        **{
            field: digest
            for field in (
                "toolchain_manifest_sha256",
                "source_set_sha256",
                "msvc_root_sha256",
                "windows_sdk_root_sha256",
                "include_roots_sha256",
                "library_roots_sha256",
                "library_set_sha256",
                "compiler_sha256",
                "linker_sha256",
                "c1xx_sha256",
                "c2_sha256",
                "runtime_launcher_sha256",
                "sandbox_helper_sha256",
            )
        },
    }

    assert drill._validated_native_helper_sha256(tmp_path, receipt) == digest
    with pytest.raises(drill.DrillError, match="receipt is invalid"):
        drill._validated_native_helper_sha256(
            tmp_path,
            {**receipt, "schema_version": 1},
        )
    bootstrap.write_bytes(b"different-native-helper")
    with pytest.raises(drill.DrillError, match="helpers differ"):
        drill._validated_native_helper_sha256(tmp_path, receipt)


def test_local_windows_drill_cannot_relax_fixed_twenty_four_stage_gate() -> None:
    drill = _drill_module()
    expected = {
        f"{key}-{platform}-{architecture}"
        for platform, architecture in drill._PRODUCTION_TARGETS
        for key in drill._WINDOWS_STAGE_KEYS
    }
    windows = {
        f"{key}-windows-x64" for key in drill._WINDOWS_STAGE_KEYS
    }
    assert len(expected) == 24
    assert len(windows) == 8
    assert len(expected - windows) == 16
    source = Path(drill.__file__).read_text(encoding="utf-8")
    assert "run_bounded_process(" in source
    assert '"fixed_gate_relaxed": False' in source
    assert '"promotion_claimed": False' in source


def test_local_windows_drill_forbids_cross_source_partial_splicing() -> None:
    drill = _drill_module()
    source = Path(drill.__file__).read_text(encoding="utf-8")

    assert 'core_attempts[1]["resume_from"] != 0' in source
    assert '"cross_source_partial_reuse_forbidden": True' in source
    assert 'core_attempts[1]["resume_from"] <= 0' not in source


def test_local_bootstrap_floor_is_release_key_signed_and_canonical(
    tmp_path: Path,
) -> None:
    drill = _drill_module()
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    config = bootstrap / "bootstrap-config.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "public_index_url": "https://localhost/index.json",
                "sandbox_helper_sha256": "a" * 64,
                "release_public_keys": {},
                "publication_public_keys": {},
                "minimum_stable": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    private = drill.Ed25519PrivateKey.generate()
    signer = drill.Ed25519MemorySigner("local-test-key", private)
    receipt = drill._bind_local_bootstrap_minimum(bootstrap, signer)
    value = json.loads(config.read_text(encoding="utf-8"))
    assert receipt["sequence"] == 1
    assert value["minimum_stable"]["sequence"] == 1
    assert value["minimum_stable"]["version"] == "1.0.0"
    assert value["minimum_stable"]["signature"]["key_id"] == "local-test-key"
    assert config.read_text(encoding="utf-8").endswith("\n")
