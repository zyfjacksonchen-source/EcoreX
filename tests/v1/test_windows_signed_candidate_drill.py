from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import threading
import zipfile

import pytest


def _drill_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "drill_v1_windows_signed_candidate.py"
    )
    spec = importlib.util.spec_from_file_location(
        "ecorex_windows_candidate_drill", path
    )
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


def test_runtime_readiness_failure_reports_bounded_process_diagnostics(
    tmp_path: Path,
) -> None:
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
            r"phase=initializing; runtime_failed; runtime_exit_code=2; "
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


def test_drill_reserves_a_non_ephemeral_loopback_port_until_release() -> None:
    drill = _drill_module()
    lease = drill._reserve_loopback_port()
    try:
        assert (
            drill._DRILL_LOOPBACK_PORT_MIN
            <= lease.port
            <= drill._DRILL_LOOPBACK_PORT_MAX
        )
        competing = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError):
                competing.bind(("127.0.0.1", lease.port))
        finally:
            competing.close()
    finally:
        lease.release()

    rebound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        rebound.bind(("127.0.0.1", lease.port))
    finally:
        rebound.close()


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


def test_candidate_cli_defaults_to_v0292_and_accepts_readonly_source() -> None:
    drill = _drill_module()
    parser = drill._parser()

    defaults = parser.parse_args([])
    assert defaults.legacy_source_version == "0.2.9.2"
    assert defaults.legacy_source is None
    selected = parser.parse_args(
        ["--legacy-source-version", "0.3.0", "--legacy-source", "legacy-root"]
    )
    assert selected.legacy_source_version == "0.3.0"
    assert selected.legacy_source == Path("legacy-root")


def test_exact_v0292_fixture_passes_product_activation_migration_gate(
    tmp_path: Path,
) -> None:
    drill = _drill_module()
    legacy = drill._create_released_v0292_fixture(
        Path(drill.__file__).resolve().parents[1],
        tmp_path / "legacy",
        deadline=drill.Deadline.after(60),
    )
    install = tmp_path / "install"
    state = install / "state"
    candidate = install / "slots" / "candidate-v1"
    state.mkdir(parents=True)
    candidate.mkdir(parents=True)
    drill.write_product_migration_plan(
        install,
        legacy["source"],
        source_version="0.2.9.2",
    )
    migration = drill.ProductLegacyMigrationCoordinator(
        install,
        state / drill.TARGET_DATABASE_NAME,
    )

    assert migration.dry_run(candidate, "v0292-signed-gate-dry-run") is True
    assert not (state / drill.TARGET_DATABASE_NAME).exists()
    assert migration.commit(candidate, "v0292-signed-gate-commit") is True
    report = json.loads((state / "migration-report.json").read_text(encoding="utf-8"))
    aggregates = drill._migration_aggregate_evidence(
        source=Path(legacy["source"]),
        database=state / drill.TARGET_DATABASE_NAME,
        report=report,
    )

    assert aggregates == {
        "threads": 2,
        "messages": 2,
        "session_summaries": 2,
        "projects": 1,
        "project_bindings": 1,
        "deleted_session_cache_excluded": 1,
        "deleted_sessions_restored": 0,
        "database_integrity": "ok",
        "deletion_authority_verified": True,
        "aggregate_only": True,
    }


def test_user_selected_legacy_source_is_only_read_and_snapshotted(
    tmp_path: Path,
) -> None:
    drill = _drill_module()
    source = tmp_path / "selected-source"
    source.mkdir()
    (source / "nested").mkdir()
    original = source / "nested" / "user-state.json"
    original.write_text('{"retained":true}\n', encoding="utf-8", newline="\n")
    before = drill.inventory_source(source, source_version="0.2.9.2")

    snapshot = drill._snapshot_legacy_source(
        source,
        tmp_path / "disposable-snapshot",
        source_version="0.2.9.2",
        deadline=drill.Deadline.after(30),
    )

    assert drill.inventory_source(source, source_version="0.2.9.2") == before
    assert snapshot["corpus_mode"] == "user-selected-readonly-snapshot"
    copied = Path(snapshot["source"]) / "nested" / "user-state.json"
    copied.write_text('{"retained":false}\n', encoding="utf-8", newline="\n")
    assert original.read_text(encoding="utf-8") == '{"retained":true}\n'


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path contract")
def test_user_selected_snapshot_supports_paths_beyond_max_path(tmp_path: Path) -> None:
    drill = _drill_module()
    source = tmp_path / "source"
    relative = Path("nested") / ("x" * 110 + ".json")
    original = source / relative
    original.parent.mkdir(parents=True)
    original.write_text('{"retained":true}\n', encoding="utf-8")
    destination = tmp_path / ("snapshot-" + "y" * 100)
    assert len(str(destination / relative)) >= 260

    snapshot = drill._snapshot_legacy_source(
        source,
        destination,
        source_version="0.2.9.2",
        deadline=drill.Deadline.after(30),
    )

    assert (Path(snapshot["source"]) / relative).read_bytes() == original.read_bytes()


def test_deleted_cache_gate_uses_the_product_database_candidate_order(
    tmp_path: Path,
) -> None:
    drill = _drill_module()
    source = tmp_path / "legacy"
    first = source / "sessions" / "conversations.db"
    second = source / "conversations.db"
    first.parent.mkdir(parents=True)
    for database, session_id in ((first, "canonical-first"), (second, "later-copy")):
        connection = drill.sqlite3.connect(database)
        try:
            connection.executescript(
                """
                CREATE TABLE sessions(
                    session_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    last_active REAL NOT NULL
                );
                CREATE TABLE messages(
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(session_id, seq)
                );
                """
            )
            connection.execute(
                "INSERT INTO sessions(session_id,created_at,last_active) VALUES(?,1,1)",
                (session_id,),
            )
            connection.commit()
        finally:
            connection.close()
    ui = source / ".ecorex" / "ui-state.json"
    ui.parent.mkdir()
    ui.write_text(
        json.dumps(
            {
                "sessionTitles": {
                    "canonical-first": "live",
                    "later-copy": "cache-only under product authority",
                }
            }
        ),
        encoding="utf-8",
    )

    assert drill._cache_only_session_ids(source) == {"later-copy"}


def test_migration_failure_does_not_replace_the_current_runtime_slot(
    tmp_path: Path,
) -> None:
    drill = _drill_module()
    from tests.v1.test_update_coordinator import (
        AcceptingTestVerifier,
        _fetcher,
        _manifest,
        _package,
    )

    install = tmp_path / "install"
    baseline_payload = _package("1.0.0")
    baseline_manifest = _manifest("1.0.0", baseline_payload)
    baseline = drill.InstallCoordinator(
        install,
        fetcher=_fetcher(tmp_path / "baseline-sources", baseline_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        host_platform="windows",
        host_architecture="x64",
        bootstrap_health_confirmation=False,
    )
    baseline_prepared = baseline.prepare_update(baseline_manifest, "core-windows-x64")
    assert (
        baseline.activate(baseline_prepared.transaction_id).state
        is drill.InstallState.COMPLETED
    )
    current_before = baseline.slots.pointers().current
    assert current_before is not None

    legacy = drill._create_released_v0292_fixture(
        Path(drill.__file__).resolve().parents[1],
        tmp_path / "legacy",
        deadline=drill.Deadline.after(60),
    )
    (install / "state").mkdir(exist_ok=True)
    drill.write_product_migration_plan(
        install,
        legacy["source"],
        source_version="0.2.9.2",
    )
    migration = drill.ProductLegacyMigrationCoordinator(
        install,
        install / "state" / drill.TARGET_DATABASE_NAME,
    )
    update_payload = _package("1.0.1")
    update_manifest = _manifest("1.0.1", update_payload)

    def mutate_then_commit(slot: Path, transaction_id: str) -> bool:
        (Path(legacy["source"]) / "late-user-write.txt").write_text(
            "source changed after dry-run\n", encoding="utf-8"
        )
        return migration.commit(slot, transaction_id)

    updater = drill.InstallCoordinator(
        install,
        fetcher=_fetcher(tmp_path / "update-sources", update_payload),
        verifier=AcceptingTestVerifier(),
        health_checker=lambda _slot: True,
        migration_dry_run=lambda slot: migration.dry_run(slot),
        migration_prepare=mutate_then_commit,
        host_platform="windows",
        host_architecture="x64",
        bootstrap_health_confirmation=False,
    )
    prepared = updater.prepare_update(update_manifest, "core-windows-x64")

    failed = updater.activate(prepared.transaction_id)

    assert failed.state is drill.InstallState.FAILED
    assert failed.error == "ProductMigrationError"
    assert updater.slots.pointers().current == current_before
    assert not (install / "state" / drill.TARGET_DATABASE_NAME).exists()


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
    assert (
        parsed.release_public_keys[drill.SIGNING_KEY_ID]
        != (parsed.rollback_public_keys[drill.ROLLBACK_KEY_ID])
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
    assert (
        drill._platform_stage_failure_code(
            output,
            b'{"code":"fallback_probe_failed","secret":"must-not-surface"}\n',
        )
        == "office_format_probe_failed"
    )

    (output / "stage-failure.json").write_text(
        json.dumps({"status": "failed", "code": "BAD secret value"}),
        encoding="utf-8",
    )
    assert (
        drill._platform_stage_failure_code(
            output,
            b'{"code":"safe_fallback_failed","detail":"not returned"}\n',
        )
        == "safe_fallback_failed"
    )
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
    bootstrap = tmp_path / "stages/windows-x64/bootstrap/bin/ecorex-sandbox-host.exe"
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
    windows = {f"{key}-windows-x64" for key in drill._WINDOWS_STAGE_KEYS}
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
    expected_sequence = drill.stable_pointer_sequence(drill.__version__)
    assert receipt["sequence"] == expected_sequence
    assert value["minimum_stable"]["sequence"] == expected_sequence
    assert value["minimum_stable"]["version"] == drill.__version__
    assert value["minimum_stable"]["signature"]["key_id"] == "local-test-key"
    assert config.read_text(encoding="utf-8").endswith("\n")
