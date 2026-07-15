from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import threading
import zipfile

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


def test_runtime_readiness_failure_reports_bounded_process_diagnostics(tmp_path: Path) -> None:
    drill = _drill_module()
    result = drill.BootstrapRunResult(
        exit_code=70,
        reason=drill.BootstrapReason.RUNTIME_FAILED,
        launches=1,
        requested_restarts=0,
        launched_slots=("slot-private-identity",),
        runtime_exit_code=2,
    )

    with pytest.raises(
        drill.DrillError,
        match=(
            r"runtime_failed; runtime_exit_code=2; "
            r"runtime_startup_stage=unavailable; launches=1; requested_restarts=0$"
        ),
    ) as failure:
        drill._wait_for_full_runtime(
            tmp_path,
            expected_slot="slot-private-identity",
            port=65534,
            deadline=drill.Deadline.after(1),
            bootstrap_results=(result,),
            bootstrap_failures=(),
        )

    assert "slot-private-identity" not in str(failure.value)


def test_candidate_deadlines_separate_total_stage_and_runtime_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drill = _drill_module()
    now = 1_000.0
    monkeypatch.setattr(drill.time, "monotonic", lambda: now)

    ceremony = drill.Deadline.after(drill.DEFAULT_TIMEOUT_SECONDS)
    ceremony.enter("runtime restart")
    runtime = ceremony.bounded(drill._RUNTIME_READY_TIMEOUT_SECONDS)
    platform = ceremony.bounded(drill._PLATFORM_STAGE_TIMEOUT_SECONDS)

    assert drill.DEFAULT_TIMEOUT_SECONDS == drill._MAX_TIMEOUT_SECONDS == 5_400.0
    assert runtime.expires_at == now + 15 * 60
    assert runtime.stage == "runtime restart"
    assert platform.expires_at == now + 50 * 60
    assert platform.expires_at < ceremony.expires_at

    near_total = drill.Deadline(now + 60, stage="near total deadline")
    assert near_total.bounded(15 * 60).expires_at == now + 60
    with pytest.raises(ValueError, match="must be positive"):
        ceremony.bounded(0)


def test_candidate_cli_exposes_the_real_bounded_default() -> None:
    drill = _drill_module()
    parser = drill._parser()

    assert parser.parse_args([]).timeout_seconds == 5_400.0
    help_text = " ".join(parser.format_help().split())
    assert "between 45 and 5400 seconds" in help_text
    assert "each Runtime readiness window is bounded to 900 seconds" in help_text


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


def test_fault_candidate_rewrites_directory_or_zipimport_entrypoint(
    tmp_path: Path,
) -> None:
    drill = _drill_module()
    directory_core = tmp_path / "directory-core"
    directory_entrypoint = directory_core / drill._FAULT_ENTRYPOINT_MEMBER
    directory_entrypoint.parent.mkdir(parents=True)
    directory_entrypoint.write_text("raise SystemExit(0)\n", encoding="utf-8")

    assert drill._inject_fault_runtime_entrypoint(directory_core) == "directory"
    assert directory_entrypoint.read_bytes() == drill._FAULT_ENTRYPOINT_PAYLOAD

    archive_core = tmp_path / "archive-core"
    archive = archive_core / "bin/pack-python/python311.zip"
    archive.parent.mkdir(parents=True)
    (archive.parent / "python.exe").write_bytes(b"product-python")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr(drill._FAULT_ENTRYPOINT_MEMBER, b"raise SystemExit(0)\n")
        output.writestr("ecorex/__init__.py", b"__version__ = '1.0.0'\n")
    (archive_core / "pack-python.json").write_bytes(
        drill.build_pack_python_manifest(
            archive_core,
            platform=drill.TARGET_PLATFORM,
            architecture=drill.TARGET_ARCHITECTURE,
        )
    )
    _interpreter, before = drill.resolve_pack_python(
        archive_core,
        platform=drill.TARGET_PLATFORM,
        architecture=drill.TARGET_ARCHITECTURE,
    )

    assert drill._inject_fault_runtime_entrypoint(archive_core) == "zipimport"
    drill._rebind_fault_pack_python(archive_core)
    _interpreter, after = drill.resolve_pack_python(
        archive_core,
        platform=drill.TARGET_PLATFORM,
        architecture=drill.TARGET_ARCHITECTURE,
    )
    assert after.closure_sha256 != before.closure_sha256
    with zipfile.ZipFile(archive) as output:
        assert output.namelist() == [
            drill._FAULT_ENTRYPOINT_MEMBER,
            "ecorex/__init__.py",
        ]
        assert output.read(drill._FAULT_ENTRYPOINT_MEMBER) == (
            drill._FAULT_ENTRYPOINT_PAYLOAD
        )
        assert output.read("ecorex/__init__.py") == b"__version__ = '1.0.0'\n"


def test_fault_candidate_rejects_missing_or_ambiguous_entrypoint(
    tmp_path: Path,
) -> None:
    drill = _drill_module()
    missing = tmp_path / "missing"
    missing.mkdir()
    with pytest.raises(drill.DrillError, match="entrypoint is ambiguous"):
        drill._inject_fault_runtime_entrypoint(missing)

    ambiguous = tmp_path / "ambiguous"
    for name in ("python311.zip", "python312.zip"):
        archive = ambiguous / "bin/pack-python" / name
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            output.writestr(drill._FAULT_ENTRYPOINT_MEMBER, b"raise SystemExit(0)\n")
    with pytest.raises(drill.DrillError, match="entrypoint is ambiguous"):
        drill._inject_fault_runtime_entrypoint(ambiguous)

    unsafe = tmp_path / "unsafe"
    archive = unsafe / "bin/pack-python/python311.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("ecorex//server/__main__.py", b"raise SystemExit(0)\n")
    with pytest.raises(drill.DrillError, match="import archive is invalid"):
        drill._inject_fault_runtime_entrypoint(unsafe)


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
    assert '"fault_preflight_exit_code": 70' in source
    assert '"pack_python_manifest_rebound": True' in source
    assert "local workstation drill" in source
    assert "dirty-worktree drill" not in source


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
