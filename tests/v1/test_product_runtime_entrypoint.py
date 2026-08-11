from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import zipfile
from functools import lru_cache

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
from fastapi.testclient import TestClient

from ecorex import __version__
from ecorex.bootstrap import RUNTIME_RELOAD_EXIT_CODE

from ecorex.connectors import (
    EphemeralEncryptedCredentialVault,
    InMemoryCredentialVault,
    LocalEncryptedCredentialVault,
)
from ecorex.observability.audit import AuditIntegrityError
from ecorex.pack_catalog import (
    CAPABILITY_PACK_PROFILES,
    REQUIRED_CAPABILITY_PACK_IDS,
    capability_pack_profile,
)
from ecorex.protocol import CreateTurnRequest
from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    ReleaseBuilder,
    ReleaseBuildError,
    ReleaseBuildSpec,
    WebBundleBuildInput,
)
from ecorex.runtime.database import SCHEMA_VERSION as RUNTIME_STORAGE_SCHEMA_VERSION
from ecorex.runtime import RuntimeKernel
from ecorex.runtime.storage_migrations import (
    STORAGE_MIGRATION_FILE_NAME,
    StorageMigrationManifest,
)
from ecorex.server import (
    BundleIntegrityError,
    ProductRuntimeConfig,
    ProductRuntimeConfigurationError,
    ProductRuntimeTrustError,
    ServerConfigurationError,
    WebBundleManifest,
    WebFileRecord,
    create_product_app,
    load_product_runtime,
    load_verified_capability_packs,
)
from ecorex.server.cli import (
    ProductRuntimeExitCode,
    _load_product_runtime_for_cli,
    build_product_runtime_server,
    main as product_main,
)
from ecorex.session import (
    BrokerDeviceChallenge,
    BrokerDeviceGrant,
    BrokerPollResult,
    BrokerPollStatus,
    Ed25519SessionLeaseVerifier,
    ManagedSessionLeaseClaims,
    ManagedSessionService,
    SessionLeaseSignature,
    SignedManagedSessionLease,
    token_digest,
)
from ecorex.startup_diagnostics import STARTUP_DIAGNOSTIC_TOKEN_ENV
from ecorex.update import (
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SlotStore,
    SourceKind,
    Ed25519SignatureVerifier,
)
from ecorex.integration import windows_sandbox_security as sandbox_security_module
from ecorex.integration.windows_sandbox_security import WindowsSandboxSlotSecurity
from ecorex.integration.windows_path_identity import windows_invariant_path_key
from ecorex.integration.sandbox import (
    SANDBOX_LAUNCH_PROTOCOL,
    WINDOWS_CPU_RATE_HARD_CAP,
    WINDOWS_JOB_MEMORY_LIMIT_BYTES,
    WINDOWS_PROCESS_MEMORY_LIMIT_BYTES,
    WindowsAppContainerSandboxBackend,
)


ACCESS = "managed-access-token-entrypoint-001"
REFRESH = "managed-refresh-token-entrypoint-001"
ORIGIN = "http://127.0.0.1:8765"


def test_product_cli_injects_production_signed_pack_adapter_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release blocker: the packaged CLI must not load Packs as metadata only."""

    resolver = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "ecorex.server.cli.create_production_pack_adapter_resolver",
        lambda: resolver,
    )

    def fake_loader(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("ecorex.server.cli.load_product_runtime", fake_loader)

    result = _load_product_runtime_for_cli(host="127.0.0.1", port=8765)

    assert result is not None
    assert captured == {
        "host": "127.0.0.1",
        "port": 8765,
        "pack_adapter_resolver": resolver,
    }


def _logical_database_snapshot(path: Path) -> tuple:
    with sqlite3.connect(path) as connection:
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        )
        result = []
        for table in tables:
            columns = tuple(
                str(row[1])
                for row in connection.execute(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
            )
            order = ", ".join(f'"{column}"' for column in columns)
            rows = connection.execute(
                f'SELECT * FROM "{table}"' + (f" ORDER BY {order}" if order else "")
            ).fetchall()
            result.append((table, columns, tuple(tuple(row) for row in rows)))
        return tuple(result)


def _filesystem_snapshot(root: Path) -> tuple[tuple[str, str, str], ...]:
    records = []
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        relative = path.relative_to(root).as_posix()
        if path.name.endswith(("-shm", "-wal")):
            continue
        if path.is_symlink():
            records.append((relative, "link", str(path.readlink())))
        elif path.is_dir():
            records.append((relative, "directory", ""))
        else:
            records.append(
                (relative, "file", hashlib.sha256(path.read_bytes()).hexdigest())
            )
    return tuple(records)


def test_filesystem_snapshot_ignores_sqlite_transient_sidecars(tmp_path: Path) -> None:
    (tmp_path / "runtime.sqlite3-shm").write_bytes(b"transient")
    (tmp_path / "runtime.sqlite3-wal").write_bytes(b"transient")

    assert _filesystem_snapshot(tmp_path) == ()


def _startup_filesystem_snapshot(
    install_root: Path,
) -> tuple[tuple[str, str, str], ...]:
    """Ignore SQLite page/WAL bytes while retaining every other file and directory."""

    return tuple(
        record
        for record in _filesystem_snapshot(install_root)
        if not record[0].startswith("state/runtime.sqlite3")
    )


def _startup_convergence_snapshot(path: Path) -> tuple:
    selected = {
        "connector_definitions",
        "extension_catalog_snapshots",
        "extension_meta",
        "extension_revisions",
        "extension_states",
        "managed_session_state",
        "output_preferences",
        "runtime_permission_state",
        "runtime_snapshots",
        "runtime_update_state",
    }
    return tuple(
        record for record in _logical_database_snapshot(path) if record[0] in selected
    )


def _public(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _placeholder(key_id: str) -> SignatureEnvelope:
    return SignatureEnvelope(
        "ed25519", key_id, base64.b64encode(b"0" * 64).decode("ascii")
    )


def _sign(private: Ed25519PrivateKey, key_id: str, payload: bytes) -> SignatureEnvelope:
    return SignatureEnvelope(
        "ed25519", key_id, base64.b64encode(private.sign(payload)).decode("ascii")
    )


def _sources() -> tuple[ReleaseSource, ...]:
    return (
        ReleaseSource(
            "mirror",
            SourceKind.GITHUB_CN_MIRROR,
            0,
            "https://mirror.example/releases",
        ),
        ReleaseSource(
            "github",
            SourceKind.GITHUB_RELEASE,
            1,
            "https://github.example/releases",
        ),
        ReleaseSource("cdn", SourceKind.ECOREX_CDN, 2, "https://cdn.example/releases"),
    )


def _web(
    release_private: Ed25519PrivateKey,
    *,
    release_id: str,
    build_digest: str,
) -> tuple[bytes, bytes, str, bytes]:
    javascript = b"document.body.dataset.ready='true';\n"
    javascript_sha = hashlib.sha256(javascript).hexdigest()
    javascript_path = f"assets/app.{javascript_sha[:16]}.js"
    index = (
        "<!doctype html><html><head>"
        "<!--__ECOREX_RUNTIME_CONFIG__-->"
        f'<script type="module" src="/{javascript_path}"></script>'
        '</head><body><div id="root"></div></body></html>'
    ).encode("utf-8")
    files = (
        WebFileRecord(
            path="index.html",
            size_bytes=len(index),
            sha256=hashlib.sha256(index).hexdigest(),
            immutable=False,
        ),
        WebFileRecord(
            path=javascript_path,
            size_bytes=len(javascript),
            sha256=javascript_sha,
            immutable=True,
        ),
    )
    unsigned = WebBundleManifest(
        schema_version=1,
        release_id=release_id,
        version=__version__,
        build_digest=build_digest,
        bundle_sha256=WebBundleManifest.compute_bundle_sha256(files),
        entrypoint="index.html",
        files=files,
        signature=_placeholder("release-key"),
    )
    manifest = replace(
        unsigned,
        signature=_sign(release_private, "release-key", unsigned.canonical_payload()),
    )
    return index, javascript, javascript_path, manifest.to_json().encode("utf-8")


def _config(
    release_public: bytes,
    session_public: bytes,
    *,
    platform: str = "windows",
    architecture: str = "x64",
) -> bytes:
    raw = {
        "schema_version": 1,
        "identity": {
            "version": __version__,
            "platform": platform,
            "architecture": architecture,
        },
        "paths": {
            "database": "state/runtime.sqlite3",
            "web_root": "web",
            "web_manifest": "web-manifest.json",
            "workspace_roots": ["workspace"],
        },
        "release_public_keys": {
            "release-key": base64.b64encode(release_public).decode("ascii")
        },
        "rollback_public_keys": {
            "rollback-key": base64.b64encode(session_public).decode("ascii")
        },
        "session_public_keys": {
            "session-key": base64.b64encode(session_public).decode("ascii")
        },
        "gateway": {
            "endpoint": "https://gateway.example/v1/responses",
            "allowed_hosts": ["gateway.example"],
        },
        "device_authorization": {
            "base_url": "https://identity.example",
            "allowed_hosts": ["identity.example"],
            "client_id": "ecorex-product",
            "timeout_seconds": 20,
            "supervisor_poll_seconds": 1,
        },
        "update": {
            "release_feed_endpoint": "https://control.example/api/v1/releases/latest",
            "signal_endpoint": "wss://control.example/api/v1/client/updates/ws",
            "control_plane_hosts": ["control.example"],
            "artifact_hosts": [
                "cdn.example",
                "github.example",
                "mirror.example",
            ],
            "channel": "stable",
            "poll_interval_seconds": 300,
        },
        "share": None,
        "image_orchestration": None,
        "audit": None,
        "tracing": None,
        "connectors": None,
        "capability_packs": [],
    }
    return json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_core(
    path: Path,
    *,
    config: bytes,
    index: bytes,
    javascript: bytes,
    javascript_path: str,
    web_manifest: bytes,
    platform: str = "windows",
    launcher: bytes = b"packaged-runtime",
    sandbox_helper: bytes | None = None,
) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        entries = {
            ("bin/ecorex.exe" if platform == "windows" else "bin/ecorex"): launcher,
            "runtime-config.json": config,
            STORAGE_MIGRATION_FILE_NAME: StorageMigrationManifest.current(
                RUNTIME_STORAGE_SCHEMA_VERSION
            ).to_bytes(),
            "web-manifest.json": web_manifest,
            "web/index.html": index,
            f"web/{javascript_path}": javascript,
        }
        if sandbox_helper is not None:
            entries["bin/ecorex-sandbox-host.exe"] = sandbox_helper
        skills = Path(__file__).resolve().parents[2] / "skills"
        entries.update(
            {
                f"skills/{path.relative_to(skills).as_posix()}": path.read_bytes()
                for path in skills.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".pyo"}
            }
        )
        for name, payload in sorted(entries.items()):
            info = zipfile.ZipInfo(name)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.external_attr = (
                0o755
                if name
                in {
                    "bin/ecorex.exe",
                    "bin/ecorex",
                    "bin/ecorex-sandbox-host.exe",
                }
                else 0o644
            ) << 16
            archive.writestr(info, payload)


@lru_cache(maxsize=1)
def _built_windows_test_native() -> tuple[bytes, bytes]:
    if os.name != "nt":
        raise RuntimeError("Windows native test build is Windows-only")
    output = Path(tempfile.mkdtemp(prefix="ecorex-test-native-"))
    build = Path(__file__).resolve().parents[2] / (
        "platform-staging/native/windows/build.ps1"
    )
    native_root = build.parent
    source_names = (
        "ecorex_launcher.cpp",
        "ecorex_sandbox_host.cpp",
        "ecorex_sandbox_security.cpp",
        "ecorex_sandbox_process.cpp",
        "ecorex_sandbox_host_internal.h",
    )
    source_binding = "\0".join(
        f"{name}={hashlib.sha256((native_root / name).read_bytes()).hexdigest()}"
        for name in sorted(source_names)
    ).encode("utf-8")
    toolchain_manifest = native_root / "toolchain-manifest.json"
    powershell = (
        Path(os.environ["SYSTEMROOT"])
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        not in {
            "CL",
            "_CL_",
            "LINK",
            "_LINK_",
            "LIB",
            "LIBPATH",
            "INCLUDE",
            "CL_MPCOUNT",
            "USEENV",
            "LINK_REPRO",
            "LINK_FULLPATHRSP",
            "PSMODULEPATH",
        }
    }
    command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(build),
        "-OutputDirectory",
        str(output),
        "-SourceDirectory",
        str(native_root),
        "-ToolchainManifest",
        str(toolchain_manifest),
        "-ExpectedToolchainManifestSha256",
        hashlib.sha256(toolchain_manifest.read_bytes()).hexdigest(),
        "-ExpectedSourceSetSha256",
        hashlib.sha256(source_binding).hexdigest(),
    ]
    if os.environ.get("ECOREX_GITHUB_HOSTED_WINDOWS_NATIVE_COMPATIBILITY") == "1":
        command.append("-GitHubHostedCompatibility")
    subprocess.run(
        command,
        check=True,
        timeout=600,
        env=environment,
    )
    return (
        (output / "ecorex.exe").read_bytes(),
        (output / "ecorex-sandbox-host.exe").read_bytes(),
    )


def _stage_product(tmp_path: Path, *, config_mutator=None):
    target_platform = "windows" if os.name == "nt" else "macos"
    target_architecture = "x64"
    install_root = tmp_path / "install"
    (install_root / "state").mkdir(parents=True)
    (install_root / "workspace").mkdir()
    release_private = Ed25519PrivateKey.generate()
    session_private = Ed25519PrivateKey.generate()
    release_public = _public(release_private)
    session_public = _public(session_private)
    slot_id = "slot-product-entrypoint"
    release_id = "release-stable-entrypoint"
    build_digest = hashlib.sha256(b"entrypoint-release").hexdigest()
    index, javascript, javascript_path, web_manifest = _web(
        release_private,
        release_id=release_id,
        build_digest=build_digest,
    )
    config = _config(
        release_public,
        session_public,
        platform=target_platform,
        architecture=target_architecture,
    )
    if config_mutator is not None:
        raw_config = json.loads(config)
        config_mutator(raw_config)
        config = json.dumps(
            raw_config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    launcher_bytes = b"packaged-runtime"
    sandbox_helper_bytes: bytes | None = None
    if target_platform == "windows":
        launcher_bytes, sandbox_helper_bytes = _built_windows_test_native()
    package = tmp_path / f"core-{target_platform}-{target_architecture}.zip"
    _write_core(
        package,
        config=config,
        index=index,
        javascript=javascript,
        javascript_path=javascript_path,
        web_manifest=web_manifest,
        platform=target_platform,
        launcher=launcher_bytes,
        sandbox_helper=sandbox_helper_bytes,
    )
    placeholder = _placeholder("release-key")
    core_unsigned = ReleaseArtifact(
        artifact_id=f"core-{target_platform}-{target_architecture}",
        platform=target_platform,
        architecture=target_architecture,
        file_name=package.name,
        size_bytes=package.stat().st_size,
        sha256=hashlib.sha256(package.read_bytes()).hexdigest(),
        signature=placeholder,
    )
    core = replace(
        core_unsigned,
        signature=_sign(
            release_private,
            "release-key",
            core_unsigned.signed_payload(
                release_id=release_id,
                version=__version__,
                build_digest=build_digest,
            ),
        ),
    )
    web_unsigned = ReleaseArtifact(
        artifact_id="web-manifest",
        platform="all",
        architecture="all",
        file_name="web-manifest.json",
        size_bytes=len(web_manifest),
        sha256=hashlib.sha256(web_manifest).hexdigest(),
        signature=placeholder,
    )
    web_artifact = replace(
        web_unsigned,
        signature=_sign(
            release_private,
            "release-key",
            web_unsigned.signed_payload(
                release_id=release_id,
                version=__version__,
                build_digest=build_digest,
            ),
        ),
    )
    manifest_unsigned = ReleaseManifest(
        schema_version=1,
        release_id=release_id,
        version=__version__,
        build_digest=build_digest,
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T00:00:00+00:00",
        sources=_sources(),
        artifacts=(core, web_artifact),
        signature=placeholder,
    )
    manifest = replace(
        manifest_unsigned,
        signature=_sign(
            release_private,
            "release-key",
            manifest_unsigned.canonical_payload(),
        ),
    )
    slots = SlotStore(install_root)
    stage_options = {}
    if target_platform == "windows":
        assert sandbox_helper_bytes is not None
        bootstrap_bin = install_root / "bootstrap/bin"
        bootstrap_bin.mkdir(parents=True)
        bootstrap_helper = bootstrap_bin / "ecorex-sandbox-host.exe"
        bootstrap_helper.write_bytes(sandbox_helper_bytes)
        sandbox_security = WindowsSandboxSlotSecurity(
            install_root,
            bootstrap_helper,
            expected_helper_sha256=hashlib.sha256(sandbox_helper_bytes).hexdigest(),
        )
        stage_options = {
            "payload_preparer": lambda slot, payload: sandbox_security.prepare(
                slot, payload, package, manifest, core
            ),
            "payload_attester": lambda slot, payload, preparation: (
                sandbox_security.attest(
                    slot, payload, package, manifest, core, preparation
                )
            ),
            "payload_cleanup": lambda slot, payload, preparation: (
                sandbox_security.cleanup_failed(
                    slot, payload, manifest, core, preparation
                )
            ),
        }
    slot_path = slots.stage(
        package,
        slot_id=slot_id,
        manifest=manifest,
        artifact=core,
        **stage_options,
    )
    slots.switch_to(slot_id)
    slots.mark_known_good(slot_id)
    return {
        "install_root": install_root,
        "payload": slot_path / "payload",
        "slot_path": slot_path,
        "package": package,
        "manifest": manifest,
        "artifact": core,
        "config": slot_path / "payload/runtime-config.json",
        "database": install_root / "state/runtime.sqlite3",
        "release_private": release_private,
        "session_private": session_private,
        "session_public": session_public,
        "platform": target_platform,
        "architecture": target_architecture,
        "artifact_id": core.artifact_id,
    }


def _session_lease(
    product: dict,
    *,
    now: datetime | None = None,
    expires_at: datetime | None = None,
) -> SignedManagedSessionLease:
    now = now or datetime.now(UTC).replace(microsecond=0)
    claims = ManagedSessionLeaseClaims(
        lease_id="lease-entrypoint",
        account_id="account-entrypoint",
        organization_id="organization-entrypoint",
        display_name="EcoreX Product User",
        roles=("member",),
        model_allowlist=("ecorex-chat", "gpt-image-2"),
        quota={"model_tokens": 100_000},
        admin_denies=(),
        issued_at=now - timedelta(minutes=1),
        expires_at=expires_at or now + timedelta(hours=71),
        revision=1,
        access_token_sha256=token_digest(ACCESS),
        refresh_token_sha256=token_digest(REFRESH),
    )
    private = product["session_private"]
    return SignedManagedSessionLease(
        claims=claims,
        signature=SessionLeaseSignature(
            "ed25519",
            "session-key",
            base64.b64encode(private.sign(claims.canonical_payload())).decode("ascii"),
        ),
    )


def _install_session(product: dict, vault: InMemoryCredentialVault) -> None:
    lease = _session_lease(product)
    ManagedSessionService(
        product["database"],
        vault=vault,
        verifier=Ed25519SessionLeaseVerifier(
            {"session-key": product["session_public"]}
        ),
    ).install(
        lease,
        access_token=ACCESS,
        refresh_token=REFRESH,
        client_request_id="install-session-entrypoint",
    )


def _loader(
    product: dict,
    vault: InMemoryCredentialVault,
    *,
    device_broker_factory=None,
    reload_requester_factory=None,
):
    def load(*, host: str, port: int):
        options = {}
        if device_broker_factory is not None:
            options["device_broker_factory"] = device_broker_factory
        if reload_requester_factory is not None:
            options["reload_requester_factory"] = reload_requester_factory
        target_platform = product.get("platform")
        target_architecture = product.get("architecture")
        if not isinstance(target_platform, str) or not isinstance(
            target_architecture, str
        ):
            identity = ProductRuntimeConfig.from_bytes(
                (Path(product["payload"]) / "runtime-config.json").read_bytes()
            ).identity
            target_platform = identity.platform
            target_architecture = identity.architecture
        return load_product_runtime(
            payload_root=product["payload"],
            host=host,
            port=port,
            environment={"ECOREX_BOOTSTRAPPED": "1", "ECOREX_GATEWAY_TOKEN": "ignored"},
            vault_factory=lambda: vault,
            host_platform=target_platform,
            host_architecture=target_architecture,
            **options,
        )

    return load


class _DeviceBroker:
    def __init__(self, product: dict) -> None:
        self.product = product
        self.device_code = "device-secret-never-returned-to-webui"
        self.close_count = 0

    async def begin(self, *, idempotency_key: str) -> BrokerDeviceChallenge:
        assert idempotency_key.startswith("device-begin:")
        return BrokerDeviceChallenge(
            provider_flow_id="provider-flow-entrypoint",
            device_code=self.device_code,
            user_code="ECORE-X1",
            verification_url="https://identity.example/activate",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            poll_interval_seconds=1,
        )

    async def poll(
        self,
        *,
        provider_flow_id: str,
        device_code: str,
        idempotency_key: str,
    ) -> BrokerPollResult:
        assert provider_flow_id == "provider-flow-entrypoint"
        assert device_code == self.device_code
        assert idempotency_key.startswith("device-poll:")
        return BrokerPollResult(
            BrokerPollStatus.AUTHORIZED,
            grant=BrokerDeviceGrant(
                _session_lease(self.product),
                ACCESS,
                REFRESH,
            ),
        )

    async def refresh(
        self,
        *,
        lease_id: str,
        refresh_token: str,
        idempotency_key: str,
    ) -> BrokerDeviceGrant:
        assert lease_id
        assert refresh_token == REFRESH
        assert idempotency_key.startswith("session-refresh:")
        return BrokerDeviceGrant(_session_lease(self.product), ACCESS, REFRESH)

    async def aclose(self) -> None:
        self.close_count += 1


class _GatewayResource:
    def __init__(self) -> None:
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1


class _ReloadRequester:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def request(self, identity: str | None = None) -> bool:
        assert identity is not None
        self.requests.append(identity)
        return True


def test_product_loader_post_migration_composition_is_projection_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecorex.server.config as product_config_module

    product = _stage_product(tmp_path)
    vault = InMemoryCredentialVault()
    original_session = product_config_module.ManagedSessionService
    boundary: dict[str, tuple] = {}

    def projection_session(*args, **kwargs):
        assert kwargs.get("initialize") is False
        boundary["database"] = _logical_database_snapshot(product["database"])
        boundary["filesystem"] = _filesystem_snapshot(product["install_root"])
        return original_session(*args, **kwargs)

    monkeypatch.setattr(
        product_config_module,
        "ManagedSessionService",
        projection_session,
    )
    composition = _loader(product, vault)(host="127.0.0.1", port=8765)
    try:
        assert _logical_database_snapshot(product["database"]) == boundary["database"]
        assert _filesystem_snapshot(product["install_root"]) == boundary["filesystem"]
        assert composition.update.service.startup_converged is False
    finally:
        composition.close_unstarted()


def test_product_critical_entry_remains_projection_only_for_process_lifetime(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path)
    kernel = RuntimeKernel(product["database"])
    thread = kernel.create_thread()
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="制造产品入口只读保护样本"),
    )
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE turns SET status='completed' WHERE turn_id=?",
            (created.turn.turn_id,),
        )

    vault = InMemoryCredentialVault()
    composition = _loader(product, vault)(host="127.0.0.1", port=8765)
    database_before = _logical_database_snapshot(product["database"])
    filesystem_before = _startup_filesystem_snapshot(product["install_root"])
    try:
        app = create_product_app(composition.server_settings)
        assert app.state.runtime_execution_gate.snapshot().status == "critical"
        assert composition.managed_session.startup_converged is False
        assert composition.device_authorization.startup_converged is False
        assert composition.update.service.startup_converged is False
        assert app.state.extension_service.startup_converged is False
        assert _logical_database_snapshot(product["database"]) == database_before
        assert (
            _startup_filesystem_snapshot(product["install_root"]) == filesystem_before
        )

        with TestClient(app, base_url=ORIGIN) as client:
            response = client.get(
                "/api/v1/system/health?technical=true",
                headers={"Authorization": (f"Bearer {app.state.runtime_bearer_token}")},
            )
            assert response.status_code == 200
            assert response.json()["overall"] == "critical"
            assert _logical_database_snapshot(product["database"]) == database_before
            assert (
                _startup_filesystem_snapshot(product["install_root"])
                == filesystem_before
            )
        assert _logical_database_snapshot(product["database"]) == database_before
        assert (
            _startup_filesystem_snapshot(product["install_root"]) == filesystem_before
        )
    finally:
        composition.close_unstarted()


def test_product_healthy_entry_converges_external_authorities_once(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path)
    vault = InMemoryCredentialVault()

    first = _loader(product, vault)(host="127.0.0.1", port=8765)
    try:
        first_app = create_product_app(first.server_settings)
        assert first_app.state.runtime_execution_gate.snapshot().healthy
        assert first.managed_session.startup_converged is True
        assert first.device_authorization.startup_converged is True
        assert first.server_settings.update_service is None
        assert first.update.service.startup_converged is False
        assert first_app.state.extension_service.startup_converged is True
        converged_database = _startup_convergence_snapshot(product["database"])
        converged_filesystem = _startup_filesystem_snapshot(product["install_root"])
    finally:
        first.close_unstarted()

    second = _loader(product, vault)(host="127.0.0.1", port=8765)
    try:
        second_app = create_product_app(second.server_settings)
        assert second_app.state.runtime_execution_gate.snapshot().healthy
        assert second.managed_session.startup_converged is True
        assert second.device_authorization.startup_converged is True
        assert second.server_settings.update_service is None
        assert second.update.service.startup_converged is False
        assert second_app.state.extension_service.startup_converged is True
        assert _startup_convergence_snapshot(product["database"]) == converged_database
        assert (
            _startup_filesystem_snapshot(product["install_root"])
            == converged_filesystem
        )
    finally:
        second.close_unstarted()


def test_acceptance_preview_keeps_session_model_and_image_but_disables_business_mutators(
    tmp_path: Path,
) -> None:
    def configure(raw: dict) -> None:
        raw["image_orchestration"] = {
            "root_url": "https://images.example/api/v1/images",
            "allowed_hosts": ["images.example"],
        }
        raw["share"] = {
            "endpoint": "https://share.example/api/v1/shares",
            "allowed_hosts": ["share.example"],
            "public_hosts": ["share.example"],
        }

    product = _stage_product(tmp_path, config_mutator=configure)

    def reject_platform_vault() -> InMemoryCredentialVault:
        raise AssertionError("acceptance preview must not access the platform vault")

    composition = load_product_runtime(
        payload_root=product["payload"],
        host="127.0.0.1",
        port=18765,
        environment={
            "ECOREX_BOOTSTRAPPED": "1",
            "ECOREX_RUNTIME_ACCEPTANCE_PREVIEW": "1",
            "ECOREX_RUNTIME_ACCEPTANCE_VAULT_KEY": "A" * 43,
        },
        vault_factory=reject_platform_vault,
        host_platform=product["platform"],
        host_architecture=product["architecture"],
    )
    try:
        settings = composition.server_settings
        assert settings.acceptance_preview is True
        assert isinstance(
            composition.managed_session.vault,
            EphemeralEncryptedCredentialVault,
        )
        assert settings.model_gateway is not None
        assert settings.image_orchestration_client is not None
        assert settings.managed_session_refresh_service is not None
        assert (
            create_product_app(settings).state.managed_session_refresh_supervisor
            is not None
        )
        assert settings.update_service is None
        assert settings.share_publisher is None
        assert settings.first_install_registration_recorder is None
        assert settings.first_install_runtime_ready_recorder is None
    finally:
        composition.close_unstarted()

    with pytest.raises(ProductRuntimeTrustError, match="acceptance mode"):
        load_product_runtime(
            payload_root=product["payload"],
            environment={
                "ECOREX_BOOTSTRAPPED": "1",
                "ECOREX_RUNTIME_ACCEPTANCE_PREVIEW": "attacker",
                "ECOREX_RUNTIME_ACCEPTANCE_VAULT_KEY": "A" * 43,
            },
            vault_factory=reject_platform_vault,
            host_platform=product["platform"],
            host_architecture=product["architecture"],
        )


def test_unsigned_macos_product_uses_local_vault_without_platform_keychain(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("macOS product composition is not built on Windows")
    product = _stage_product(tmp_path)

    composition = load_product_runtime(
        payload_root=product["payload"],
        environment={"ECOREX_BOOTSTRAPPED": "1"},
        host_platform=product["platform"],
        host_architecture=product["architecture"],
    )
    try:
        assert isinstance(composition.managed_session.vault, LocalEncryptedCredentialVault)
        assert (product["database"].parent / ".credential-vault.key").exists()
    finally:
        composition.close_unstarted()


def test_real_signed_slot_builds_product_app_and_uvicorn_config(tmp_path: Path) -> None:
    product = _stage_product(tmp_path)
    vault = InMemoryCredentialVault()
    _install_session(product, vault)

    server = build_product_runtime_server(
        host="127.0.0.1",
        port=8765,
        runtime_loader=_loader(product, vault),
    )

    assert server.uvicorn_config.host == "127.0.0.1"
    assert server.uvicorn_config.port == 8765
    assert server.uvicorn_config.access_log is False
    assert server.composition.server_settings.model_gateway.credentials is (
        server.composition.managed_session
    )
    assert server.composition.server_settings.update_service is None
    assert server.composition.update.feed.client.is_closed is True
    assert server.composition.update.fetcher.client.is_closed is True
    assert server.composition.update.signal_source._closed is True
    assert server.composition.server_settings.device_authorization_service is (
        server.composition.device_authorization
    )
    assert (
        server.composition.server_settings.first_install_registration_recorder.__self__
        is server.composition.update.coordinator
    )
    assert (
        server.composition.server_settings.first_install_runtime_ready_recorder.__self__
        is server.composition.update.coordinator
    )
    assert (
        server.composition.session_reload_requester._exit_code
        == RUNTIME_RELOAD_EXIT_CODE
    )
    assert server.composition.slot.slot_id == "slot-product-entrypoint"
    assert server.app.state.product_runtime_composition is server.composition
    assert "ignored" not in repr(server.composition)

    async def assert_no_legacy_update_tasks() -> None:
        async with server.app.router.lifespan_context(server.app):
            assert not any(
                task.get_name().startswith("ecorex-update")
                for task in asyncio.all_tasks()
            )

    asyncio.run(assert_no_legacy_update_tasks())


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer path contract")
@pytest.mark.parametrize("failure_phase", ("before-write-through", "after-write-through"))
def test_windows_helper_store_fault_boundary_never_publishes_a_missing_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    install_root = tmp_path / failure_phase / "install"
    bootstrap_bin = install_root / "bootstrap/bin"
    bootstrap_bin.mkdir(parents=True)
    helper = bootstrap_bin / "ecorex-sandbox-host.exe"
    helper.write_bytes(b"MZ-helper-durability-boundary")
    digest = hashlib.sha256(helper.read_bytes()).hexdigest()
    final = (
        install_root
        / "bootstrap"
        / "helpers"
        / digest
        / "ecorex-sandbox-host.exe"
    )
    real_durable_replace = sandbox_security_module._durable_replace

    class SimulatedPowerLoss(BaseException):
        pass

    def fail_at_boundary(source, destination, *, replace_existing):
        assert replace_existing is False
        if failure_phase == "before-write-through":
            raise OSError("simulated durable rename failure")
        real_durable_replace(
            source,
            destination,
            replace_existing=replace_existing,
        )
        raise SimulatedPowerLoss

    monkeypatch.setattr(
        sandbox_security_module,
        "_durable_replace",
        fail_at_boundary,
    )
    if failure_phase == "before-write-through":
        with pytest.raises(
            sandbox_security_module.WindowsSandboxSecurityError,
            match="immutable store",
        ):
            WindowsSandboxSlotSecurity(
                install_root,
                helper,
                expected_helper_sha256=digest,
            )
        assert not final.exists()
    else:
        with pytest.raises(SimulatedPowerLoss):
            WindowsSandboxSlotSecurity(
                install_root,
                helper,
                expected_helper_sha256=digest,
            )
        assert hashlib.sha256(final.read_bytes()).hexdigest() == digest

    monkeypatch.setattr(
        sandbox_security_module,
        "_durable_replace",
        real_durable_replace,
    )
    if failure_phase == "after-write-through":
        restarted = WindowsSandboxSlotSecurity.for_provision_digest(
            install_root,
            digest,
        )
        assert restarted.bootstrap_helper == final.resolve(strict=True)
    assert not tuple(
        (install_root / "bootstrap/helpers").glob(f".{digest}.*")
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer path contract")
def test_windows_helper_rotation_keeps_cold_restart_and_rollback_attestable(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path / "helper-rotation")
    install_root = product["install_root"]
    slots = SlotStore(install_root)
    slot = product["slot_path"]
    marker = slots.marker("slot-product-entrypoint")
    receipt = dict(marker["security_provision"])
    manifest = slots.release_manifest("slot-product-entrypoint")
    artifact = manifest.artifact(marker["artifact_id"])
    old_digest = str(receipt["provision_helper_sha256"])
    mutable_helper = install_root / "bootstrap/bin/ecorex-sandbox-host.exe"
    retained_old = (
        install_root
        / "bootstrap"
        / "helpers"
        / old_digest
        / "ecorex-sandbox-host.exe"
    )
    assert retained_old.is_file()
    assert hashlib.sha256(retained_old.read_bytes()).hexdigest() == old_digest

    # A PE overlay changes the cryptographic identity while preserving a
    # runnable helper, matching an actual signed helper rebuild/rotation.
    rotated_bytes = mutable_helper.read_bytes() + b"\0ecorex-helper-rotation-v2"
    rotated_digest = hashlib.sha256(rotated_bytes).hexdigest()
    mutable_helper.write_bytes(rotated_bytes)
    rotated = WindowsSandboxSlotSecurity(
        install_root,
        mutable_helper,
        expected_helper_sha256=rotated_digest,
    )
    assert rotated.bootstrap_helper != retained_old

    # A cold Runtime start and a subsequent rollback resolve the exact helper
    # recorded by the old slot, not the now-rotated compatibility copy.
    restarted_old = WindowsSandboxSlotSecurity.for_provision_digest(
        install_root,
        old_digest,
        release_id=manifest.release_id,
    )
    assert restarted_old.bootstrap_helper == retained_old.resolve(strict=True)
    assert restarted_old.validate(slot, manifest, artifact, receipt) is True
    restarted_new = WindowsSandboxSlotSecurity.for_provision_digest(
        install_root,
        rotated_digest,
    )
    assert restarted_new.bootstrap_helper == (
        install_root
        / "bootstrap"
        / "helpers"
        / rotated_digest
        / "ecorex-sandbox-host.exe"
    ).resolve(strict=True)

    restarted_old.cleanup_slot(slot, manifest, artifact, receipt)


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer recovery contract")
def test_windows_helper_rotation_recovers_interrupted_target_provision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = _stage_product(tmp_path / "helper-rotation-recovery")
    install_root = product["install_root"]
    slots = SlotStore(install_root)
    old_marker = slots.marker("slot-product-entrypoint")
    old_receipt = dict(old_marker["security_provision"])
    old_digest = str(old_receipt["provision_helper_sha256"])
    mutable_helper = install_root / "bootstrap/bin/ecorex-sandbox-host.exe"
    target_bytes = mutable_helper.read_bytes() + b"\0ecorex-helper-recovery-v2"
    target_digest = hashlib.sha256(target_bytes).hexdigest()
    target_version = (
        install_root
        / "bootstrap"
        / "versions"
        / "release-stable-helper-recovery"
        / "bin"
    )
    target_version.mkdir(parents=True)
    target_helper = target_version / "ecorex-sandbox-host.exe"
    target_helper.write_bytes(target_bytes)
    target_security = WindowsSandboxSlotSecurity(
        install_root,
        target_helper,
        expected_helper_sha256=target_digest,
    )
    candidate = install_root / "slots/slot-interrupted-target"
    payload = candidate / "payload"
    payload.mkdir(parents=True)
    original_invoke = target_security._invoke

    def crash_after_native_provision(helper, operation, **kwargs):
        receipt = original_invoke(helper, operation, **kwargs)
        if operation == "provision":
            raise KeyboardInterrupt("simulated power loss after native provision")
        return receipt

    monkeypatch.setattr(target_security, "_invoke", crash_after_native_provision)
    with pytest.raises(KeyboardInterrupt, match="simulated power loss"):
        target_security.prepare(
            candidate,
            payload,
            product["package"],
            product["manifest"],
            product["artifact"],
        )
    preparation = json.loads(
        (candidate / ".sandbox-security-preparation.json").read_text(
            encoding="utf-8"
        )
    )
    assert preparation["schema_version"] == 2
    assert preparation["provision_helper_sha256"] == target_digest
    mutable_helper.write_bytes(target_bytes)

    # The restarted old Runtime is bound to the prior helper, but cleanup
    # resolves the interrupted target's helper from its durable digest.
    restarted_old = WindowsSandboxSlotSecurity.for_provision_digest(
        install_root,
        old_digest,
        release_id=product["manifest"].release_id,
    )
    restarted_old.cleanup_abandoned(candidate)
    assert not (candidate / ".sandbox-security-preparation.json").exists()
    assert payload.is_dir() and not any(payload.iterdir())

    restarted_old.cleanup_slot(
        product["slot_path"],
        product["manifest"],
        product["artifact"],
        old_receipt,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer path contract")
def test_windows_sandbox_receipt_matches_invariant_unicode_path_identity(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path / "Straße-STRAẞE-Iİıi-École-中日韩-MiXeD")
    install_root = product["install_root"]
    slots = SlotStore(install_root)
    slot = product["slot_path"]
    marker = slots.marker("slot-product-entrypoint")
    receipt = dict(marker["security_provision"])
    workspace = install_root / "workspace"
    expected_permission_domain = hashlib.sha256(
        windows_invariant_path_key(workspace).encode("utf-8")
    ).hexdigest()
    manifest = slots.release_manifest("slot-product-entrypoint")
    artifact = manifest.artifact(marker["artifact_id"])
    bootstrap_helper = install_root / "bootstrap/bin/ecorex-sandbox-host.exe"
    security = WindowsSandboxSlotSecurity(
        install_root,
        bootstrap_helper,
        expected_helper_sha256=hashlib.sha256(
            bootstrap_helper.read_bytes()
        ).hexdigest(),
    )
    security.cleanup_slot(
        slot,
        manifest,
        artifact,
        receipt,
    )

    assert receipt["permission_domain_sha256"] == expected_permission_domain
    assert receipt["contract"] == "windows-appcontainer-stable-provision-v4"


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer ACL contract")
@pytest.mark.parametrize(
    "mutation",
    ("read_root_write", "child_deny_read", "workspace_child_deny_write"),
)
def test_windows_retained_slot_validation_rejects_acl_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    product = _stage_product(tmp_path / mutation)
    install_root = product["install_root"]
    slots = SlotStore(install_root)
    slot = product["slot_path"]
    payload = product["payload"]
    marker = slots.marker("slot-product-entrypoint")
    receipt = dict(marker["security_provision"])
    manifest = slots.release_manifest("slot-product-entrypoint")
    artifact = manifest.artifact(marker["artifact_id"])
    bootstrap_helper = install_root / "bootstrap/bin/ecorex-sandbox-host.exe"
    security = WindowsSandboxSlotSecurity(
        install_root,
        bootstrap_helper,
        expected_helper_sha256=hashlib.sha256(
            bootstrap_helper.read_bytes()
        ).hexdigest(),
    )
    workspaces = (install_root / "workspace",)
    read_roots = (payload,)
    icacls = Path(os.environ["SYSTEMROOT"]) / "System32/icacls.exe"
    sid = receipt["appcontainer_sid"]
    try:
        workspace_child = workspaces[0] / "mutable-user-output.txt"
        if mutation == "workspace_child_deny_write":
            workspace_child.write_text("legitimate mutable output", encoding="utf-8")
        assert security.validate(slot, manifest, artifact, receipt) is True
        command = (
            (
                str(icacls),
                str(payload),
                "/grant:r",
                f"*{sid}:(OI)(CI)(RX,W)",
            )
            if mutation == "read_root_write"
            else (
                str(icacls),
                str(
                    workspace_child
                    if mutation == "workspace_child_deny_write"
                    else payload / "runtime-config.json"
                ),
                "/deny",
                f"*{sid}:({'W' if mutation == 'workspace_child_deny_write' else 'R'})",
            )
        )
        changed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        assert changed.returncode == 0, changed.stdout.decode(errors="replace")
        assert security.validate(slot, manifest, artifact, receipt) is False
    finally:
        security._invoke(
            bootstrap_helper,
            "unprovision-slot",
            slot=slot,
            read_roots=read_roots,
            workspaces=workspaces,
            slot_digest=manifest.build_digest,
            timeout_seconds=120,
        )
        security._invoke(
            bootstrap_helper,
            "unprovision-domain",
            slot=slot,
            read_roots=read_roots,
            workspaces=workspaces,
            slot_digest=manifest.build_digest,
            timeout_seconds=120,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer reparse contract")
def test_windows_retained_slot_validation_rejects_workspace_reparse_child(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path / "workspace-reparse")
    install_root = product["install_root"]
    slots = SlotStore(install_root)
    slot = product["slot_path"]
    payload = product["payload"]
    marker = slots.marker("slot-product-entrypoint")
    receipt = dict(marker["security_provision"])
    manifest = slots.release_manifest("slot-product-entrypoint")
    artifact = manifest.artifact(marker["artifact_id"])
    bootstrap_helper = install_root / "bootstrap/bin/ecorex-sandbox-host.exe"
    security = WindowsSandboxSlotSecurity(
        install_root,
        bootstrap_helper,
        expected_helper_sha256=hashlib.sha256(
            bootstrap_helper.read_bytes()
        ).hexdigest(),
    )
    workspaces = (install_root / "workspace",)
    read_roots = (payload,)
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    linked = workspaces[0] / "linked-output"
    junction = False
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        created = subprocess.run(
            [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(linked),
                str(outside),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        if created.returncode != 0:
            security.cleanup_slot(slot, manifest, artifact, receipt)
            pytest.skip(
                "workspace symlink/junction creation is unavailable: "
                f"{error}; {created.stdout.decode(errors='replace')}"
            )
        junction = True
    try:
        assert security.validate(slot, manifest, artifact, receipt) is False
    finally:
        if junction:
            linked.rmdir()
        else:
            linked.unlink()
        security._invoke(
            bootstrap_helper,
            "unprovision-slot",
            slot=slot,
            read_roots=read_roots,
            workspaces=workspaces,
            slot_digest=manifest.build_digest,
            timeout_seconds=120,
        )
        security._invoke(
            bootstrap_helper,
            "unprovision-domain",
            slot=slot,
            read_roots=read_roots,
            workspaces=workspaces,
            slot_digest=manifest.build_digest,
            timeout_seconds=120,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer probe contract")
def test_windows_native_probe_is_canonical_and_python_backend_ready(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path)
    install_root = product["install_root"]
    slots = SlotStore(install_root)
    slot = product["slot_path"]
    marker = slots.marker("slot-product-entrypoint")
    receipt = dict(marker["security_provision"])
    manifest = slots.release_manifest("slot-product-entrypoint")
    artifact = manifest.artifact(marker["artifact_id"])
    helper = product["payload"] / "bin/ecorex-sandbox-host.exe"
    bootstrap_helper = install_root / "bootstrap/bin/ecorex-sandbox-host.exe"
    workspace_roots = (install_root / "workspace",)
    workspace_digest = hashlib.sha256(
        "\0".join(str(root) for root in workspace_roots).encode("utf-8")
    ).hexdigest()
    expected_value = {
        "backend": "windows-appcontainer",
        "cpu_rate_hard_cap": WINDOWS_CPU_RATE_HARD_CAP,
        "filesystem_read_scoped": True,
        "filesystem_write_scoped": True,
        "job_memory_limit_bytes": WINDOWS_JOB_MEMORY_LIMIT_BYTES,
        "network_denied": True,
        "process_memory_limit_bytes": WINDOWS_PROCESS_MEMORY_LIMIT_BYTES,
        "process_tree_contained": True,
        "protocol": SANDBOX_LAUNCH_PROTOCOL,
        "workspace_roots_sha256": workspace_digest,
    }
    expected_bytes = json.dumps(
        expected_value, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    security = WindowsSandboxSlotSecurity(
        install_root,
        bootstrap_helper,
        expected_helper_sha256=hashlib.sha256(
            bootstrap_helper.read_bytes()
        ).hexdigest(),
    )
    try:
        command = (
            str(helper),
            "probe",
            "--protocol",
            SANDBOX_LAUNCH_PROTOCOL,
            "--workspace-digest",
            workspace_digest,
            "--workspace",
            str(workspace_roots[0]),
        )
        for _ in range(3):
            direct = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            assert direct.returncode == 0
            assert direct.stderr == b""
            assert direct.stdout == expected_bytes
        backend = WindowsAppContainerSandboxBackend(
            helper,
            expected_sha256=hashlib.sha256(helper.read_bytes()).hexdigest(),
            security_receipt=receipt,
        )
        probe = backend.probe(
            workspace_roots=workspace_roots,
            python_executable=product["payload"] / "python/python.exe",
            artifact_path=product["payload"] / "runtime-config.json",
        )
        assert probe.complete is True
        assert probe.reason == "ready"
    finally:
        security.cleanup_slot(slot, manifest, artifact, receipt)


def test_release_builder_embeds_the_signed_web_payload_in_product_core(
    tmp_path: Path,
) -> None:
    target_platform = "windows" if os.name == "nt" else "macos"
    target_architecture = "x64"
    artifact_id = f"core-{target_platform}-{target_architecture}"
    launcher_name = "bin/ecorex.exe" if target_platform == "windows" else "bin/ecorex"
    release_private = Ed25519PrivateKey.generate()
    session_private = Ed25519PrivateKey.generate()
    core = tmp_path / "core"
    (core / "bin").mkdir(parents=True)
    (core / launcher_name).write_bytes(
        _built_windows_test_native()[0]
        if target_platform == "windows"
        else b"packaged-product-runtime"
    )
    if target_platform == "windows":
        (core / "bin/ecorex-sandbox-host.exe").write_bytes(
            _built_windows_test_native()[1]
        )
    (core / "runtime-config.json").write_bytes(
        _config(
            _public(release_private),
            _public(session_private),
            platform=target_platform,
            architecture=target_architecture,
        )
    )
    shutil.copytree(Path(__file__).resolve().parents[2] / "skills", core / "skills")
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    javascript = b"document.body.dataset.product='ready';\n"
    digest = hashlib.sha256(javascript).hexdigest()
    javascript_path = f"assets/app.{digest[:16]}.js"
    (dist / javascript_path).write_bytes(javascript)
    (dist / "index.html").write_text(
        "<!doctype html><html><head>"
        "<!--__ECOREX_RUNTIME_CONFIG__-->"
        f'<script type="module" src="/{javascript_path}"></script>'
        "</head><body></body></html>",
        encoding="utf-8",
    )
    result = ReleaseBuilder(Ed25519MemorySigner("release-key", release_private)).build(
        ReleaseBuildSpec(
            channel=ReleaseChannel.STABLE,
            created_at="2026-07-10T00:00:00+00:00",
            sources=_sources(),
            artifacts=(
                ArtifactBuildInput(
                    source_dir=core,
                    kind=ArtifactKind.CORE,
                    platform=target_platform,
                    architecture=target_architecture,
                    executable_paths=(launcher_name,),
                    product_runtime=True,
                ),
            ),
            web_bundle=WebBundleBuildInput(dist),
            dependency_lock_sha256=hashlib.sha256(
                (
                    Path(__file__).resolve().parents[2]
                    / "requirements/locks/manifest.json"
                ).read_bytes()
            ).hexdigest(),
        ),
        tmp_path / "release",
    )

    package = result.artifact_paths[artifact_id]
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        embedded_manifest = archive.read("web-manifest.json")
        embedded_config = archive.read("runtime-config.json")
    assert {
        launcher_name,
        "runtime-config.json",
        STORAGE_MIGRATION_FILE_NAME,
        "web-manifest.json",
        "web/index.html",
        f"web/{javascript_path}",
    } <= names
    assert embedded_manifest == result.artifact_paths["web-manifest"].read_bytes()
    assert ProductRuntimeConfig.from_bytes(embedded_config).identity.version == __version__
    assert all(
        "channel/web" not in name and "dist-electron" not in name for name in names
    )

    install_root = tmp_path / "installed"
    (install_root / "state").mkdir(parents=True)
    (install_root / "workspace").mkdir()
    slots = SlotStore(install_root)
    slot_id = "slot-release-builder-e2e"
    artifact = result.manifest.artifact(artifact_id)
    stage_options = {}
    if target_platform == "windows":
        helper_bytes = _built_windows_test_native()[1]
        bootstrap_bin = install_root / "bootstrap/bin"
        bootstrap_bin.mkdir(parents=True)
        bootstrap_helper = bootstrap_bin / "ecorex-sandbox-host.exe"
        bootstrap_helper.write_bytes(helper_bytes)
        sandbox_security = WindowsSandboxSlotSecurity(
            install_root,
            bootstrap_helper,
            expected_helper_sha256=hashlib.sha256(helper_bytes).hexdigest(),
        )
        stage_options = {
            "payload_preparer": lambda slot, payload: sandbox_security.prepare(
                slot, payload, package, result.manifest, artifact
            ),
            "payload_attester": lambda slot, payload, preparation: (
                sandbox_security.attest(
                    slot, payload, package, result.manifest, artifact, preparation
                )
            ),
            "payload_cleanup": lambda slot, payload, preparation: (
                sandbox_security.cleanup_failed(
                    slot, payload, result.manifest, artifact, preparation
                )
            ),
        }
    slot_path = slots.stage(
        package,
        slot_id=slot_id,
        manifest=result.manifest,
        artifact=artifact,
        **stage_options,
    )
    slots.switch_to(slot_id)
    slots.mark_known_good(slot_id)
    product = {
        "payload": slot_path / "payload",
        "database": install_root / "state/runtime.sqlite3",
        "session_private": session_private,
        "session_public": _public(session_private),
        "platform": target_platform,
        "architecture": target_architecture,
        "artifact_id": artifact_id,
    }
    vault = InMemoryCredentialVault()
    _install_session(product, vault)
    server = build_product_runtime_server(
        host="127.0.0.1",
        port=9876,
        runtime_loader=_loader(product, vault),
    )
    assert server.app.version == __version__
    assert server.uvicorn_config.port == 9876
    assert server.composition.slot.manifest == result.manifest


def test_release_builder_blocks_implicit_or_web_less_product_core(
    tmp_path: Path,
) -> None:
    release_private = Ed25519PrivateKey.generate()
    session_private = Ed25519PrivateKey.generate()
    core = tmp_path / "core"
    (core / "bin").mkdir(parents=True)
    (core / "bin/ecorex.exe").write_bytes(b"runtime")
    (core / "runtime-config.json").write_bytes(
        _config(_public(release_private), _public(session_private))
    )
    signer = Ed25519MemorySigner("release-key", release_private)
    implicit = ArtifactBuildInput(
        source_dir=core,
        kind=ArtifactKind.CORE,
        platform="windows",
        architecture="x64",
        executable_paths=("bin/ecorex.exe",),
    )
    with pytest.raises(ReleaseBuildError, match="explicit product_runtime"):
        ReleaseBuilder(signer).build(
            ReleaseBuildSpec(
                channel=ReleaseChannel.STABLE,
                created_at="2026-07-10T00:00:00+00:00",
                sources=_sources(),
                artifacts=(implicit,),
            ),
            tmp_path / "implicit-release",
        )
    explicit = replace(implicit, product_runtime=True)
    with pytest.raises(ReleaseBuildError, match="signed React bundle"):
        ReleaseBuilder(signer).build(
            ReleaseBuildSpec(
                channel=ReleaseChannel.STABLE,
                created_at="2026-07-10T00:00:00+00:00",
                sources=_sources(),
                artifacts=(explicit,),
            ),
            tmp_path / "web-less-release",
        )


def test_missing_session_starts_unauthenticated_with_model_execution_closed(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path)
    vault = InMemoryCredentialVault()
    server = build_product_runtime_server(
        host="127.0.0.1",
        port=8765,
        runtime_loader=_loader(product, vault),
    )
    assert server.composition.managed_session is not None
    assert server.composition.device_authorization is not None
    assert server.app.state.runtime_settings.require_managed_session is True
    assert server.app.state.model_worker_supervisor is None
    assert server.app.state.channel_runtime_dispatcher is None
    assert server.app.state.channel_self_service.adapters == {}


def test_expired_signed_session_keeps_local_shell_but_not_model_worker(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path)
    vault = InMemoryCredentialVault()
    issued = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3)
    lease = _session_lease(
        product,
        now=issued,
        expires_at=issued + timedelta(hours=1),
    )
    ManagedSessionService(
        product["database"],
        vault=vault,
        verifier=Ed25519SessionLeaseVerifier(
            {"session-key": product["session_public"]}
        ),
        clock=lambda: issued + timedelta(minutes=5),
    ).install(
        lease,
        access_token=ACCESS,
        refresh_token=REFRESH,
        client_request_id="install-expired-at-runtime-session",
    )
    server = build_product_runtime_server(
        host="127.0.0.1",
        port=8765,
        runtime_loader=_loader(product, vault),
    )
    bootstrap = TestClient(server.app, base_url=ORIGIN).get(
        "/api/v1/bootstrap",
        headers={"Authorization": f"Bearer {server.app.state.runtime_bearer_token}"},
    )
    assert bootstrap.status_code == 200
    assert bootstrap.json()["login"]["authenticated"] is False
    assert bootstrap.json()["model_service"]["state"] == "unavailable"
    assert server.app.state.model_worker_supervisor is None


def test_first_start_bootstrap_device_login_and_same_slot_reload_contract(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path)
    vault = InMemoryCredentialVault()
    broker = _DeviceBroker(product)
    reload_requester = _ReloadRequester()
    server = build_product_runtime_server(
        host="127.0.0.1",
        port=8765,
        runtime_loader=_loader(
            product,
            vault,
            device_broker_factory=lambda _settings: broker,
            reload_requester_factory=lambda: reload_requester,
        ),
    )
    client = TestClient(server.app, base_url=ORIGIN)
    bearer = server.app.state.runtime_bearer_token
    authorization = {"Authorization": f"Bearer {bearer}"}
    bootstrap = client.get("/api/v1/bootstrap", headers=authorization)
    assert bootstrap.status_code == 200
    bootstrap_payload = bootstrap.json()
    assert bootstrap_payload["login"]["authenticated"] is False
    assert bootstrap_payload["login_service"] == {"state": "ready", "reason": None}
    assert bootstrap_payload["model_service"] == {
        "state": "unavailable",
        "reason": "managed_session_unavailable",
    }
    csrf = bootstrap_payload["csrf_token"]
    mutation_headers = {
        **authorization,
        "Origin": ORIGIN,
        "X-EcoreX-CSRF": csrf,
    }
    started = client.post(
        "/api/v1/session/device",
        json={"client_request_id": "first-product-device-login"},
        headers=mutation_headers,
    )
    assert started.status_code == 202
    assert ACCESS not in started.text
    assert REFRESH not in started.text
    assert broker.device_code not in started.text
    flow_id = started.json()["flow_id"]
    polled = client.post(
        f"/api/v1/session/device/{flow_id}/poll",
        json={"client_request_id": "first-product-device-poll"},
        headers=mutation_headers,
    )
    assert polled.status_code == 200
    assert polled.json()["status"] == "authorized"
    assert polled.json()["restart_required"] is True
    assert polled.json()["restart_scheduled"] is True
    assert reload_requester.requests == [
        f"session-login:{polled.json()['session_generation']}"
    ]
    assert server.composition.managed_session.snapshot().account_id == (
        "account-entrypoint"
    )


def test_tampered_config_and_non_loopback_endpoint_fail_closed(tmp_path: Path) -> None:
    product = _stage_product(tmp_path)
    vault = InMemoryCredentialVault()
    _install_session(product, vault)
    original = product["config"].read_bytes()
    product["config"].write_bytes(original + b" ")
    with pytest.raises(ProductRuntimeTrustError):
        _loader(product, vault)(host="127.0.0.1", port=8765)
    product["config"].write_bytes(original)
    with pytest.raises(ProductRuntimeConfigurationError, match="loopback"):
        _loader(product, vault)(host="0.0.0.0", port=8765)


def test_invalid_release_signature_fails_before_session_or_network_use(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path)
    vault = InMemoryCredentialVault()
    _install_session(product, vault)
    manifest_path = product["slot_path"] / "release-manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["signature"]["value"] = base64.b64encode(b"X" * 64).decode("ascii")
    manifest_path.write_text(
        json.dumps(raw, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(ProductRuntimeTrustError):
        _loader(product, vault)(host="127.0.0.1", port=8765)


def test_config_rejects_credentials_unknown_fields_and_unsafe_paths(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path)
    raw = json.loads(product["config"].read_text(encoding="utf-8"))
    raw["gateway"]["bearer_token"] = "must-never-be-accepted"
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ProductRuntimeConfigurationError, match="credential"):
        ProductRuntimeConfig.from_bytes(payload)

    raw = json.loads(product["config"].read_text(encoding="utf-8"))
    raw["paths"]["database"] = "../outside.sqlite3"
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ProductRuntimeConfigurationError, match="relative path"):
        ProductRuntimeConfig.from_bytes(payload)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    (
        (
            "release_feed_endpoint",
            "https://control.example/ecorex-agent/api/v1/releases/eligible",
            "canonical Control Plane release path",
        ),
        (
            "signal_endpoint",
            "wss://control.example/ecorex-agent/api/v1/updates/events",
            "canonical Control Plane update signal path",
        ),
    ),
)
def test_config_rejects_noncanonical_update_paths(
    tmp_path: Path,
    key: str,
    value: str,
    message: str,
) -> None:
    product = _stage_product(tmp_path)
    raw = json.loads(product["config"].read_text(encoding="utf-8"))
    raw["update"][key] = value
    payload = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(ProductRuntimeConfigurationError, match=message):
        ProductRuntimeConfig.from_bytes(payload)


def test_signed_share_service_is_composed_with_managed_credentials(
    tmp_path: Path,
) -> None:
    def configure(raw: dict) -> None:
        raw["share"] = {
            "endpoint": "https://control.example/api/v1/shares",
            "allowed_hosts": ["control.example"],
            "public_hosts": ["share.example"],
        }

    product = _stage_product(tmp_path, config_mutator=configure)
    composition = _loader(product, InMemoryCredentialVault())(
        host="127.0.0.1",
        port=8765,
    )
    try:
        publisher = composition.share_publisher
        assert publisher is not None
        assert publisher.credentials is composition.managed_session
        assert composition.server_settings.share_publisher is publisher
        assert composition.server_settings.share_public_hosts == frozenset(
            {"share.example"}
        )
        assert composition.config.share is not None
        assert composition.config.to_bytes() == product["config"].read_bytes()
    finally:
        composition.close_unstarted()
    assert publisher.client.is_closed is True


def test_signed_audit_service_is_composed_and_owned_by_product_lifespan(
    tmp_path: Path,
) -> None:
    def configure(raw: dict) -> None:
        raw["audit"] = {
            "endpoint": "https://control.example/api/v1/audit/records",
            "allowed_hosts": ["control.example"],
            "dispatch_seconds": 2,
            "raw_retention_days": 14,
            "aggregate_retention_days": 90,
        }

    product = _stage_product(tmp_path, config_mutator=configure)
    vault = InMemoryCredentialVault()
    _install_session(product, vault)
    server = build_product_runtime_server(
        host="127.0.0.1",
        port=8765,
        runtime_loader=_loader(product, vault),
    )
    publisher = server.composition.audit_publisher
    assert publisher is not None
    assert publisher.session is server.composition.managed_session
    assert publisher.endpoint == "https://control.example/api/v1/audit/records"
    assert server.composition.server_settings.audit_publisher is publisher
    assert server.composition.server_settings.audit_dispatch_seconds == 2
    assert server.composition.server_settings.audit_raw_retention_days == 14
    assert server.composition.server_settings.audit_aggregate_retention_days == 90
    assert server.composition.config.to_bytes() == product["config"].read_bytes()

    with TestClient(server.app, base_url=ORIGIN):
        assert publisher.client.is_closed is False
    assert publisher.client.is_closed is True


def test_signed_otlp_trace_service_is_composed_and_owned_by_product_lifespan(
    tmp_path: Path,
) -> None:
    def configure(raw: dict) -> None:
        raw["tracing"] = {
            "endpoint": "https://otel.example/v1/traces",
            "allowed_hosts": ["otel.example"],
            "dispatch_seconds": 3,
            "max_spans_per_batch": 48,
            "max_request_bytes": 524288,
            "retention_days": 7,
        }

    product = _stage_product(tmp_path, config_mutator=configure)
    vault = InMemoryCredentialVault()
    _install_session(product, vault)
    server = build_product_runtime_server(
        host="127.0.0.1",
        port=8765,
        runtime_loader=_loader(product, vault),
    )
    exporter = server.composition.trace_exporter
    assert exporter is not None
    assert exporter.session is server.composition.managed_session
    assert exporter.endpoint == "https://otel.example/v1/traces"
    assert server.composition.server_settings.trace_exporter is exporter
    assert server.composition.server_settings.trace_dispatch_seconds == 3
    assert server.composition.server_settings.trace_max_spans_per_batch == 48
    assert server.composition.server_settings.trace_max_request_bytes == 524288
    assert server.composition.server_settings.trace_retention_days == 7
    assert server.composition.config.to_bytes() == product["config"].read_bytes()

    with TestClient(server.app, base_url=ORIGIN):
        assert exporter.client.is_closed is False
    assert exporter.client.is_closed is True


def test_signed_stable_connectors_are_composed_and_closed_by_product_lifespan(
    tmp_path: Path,
) -> None:
    def configure(raw: dict) -> None:
        raw["connectors"] = {
            "endpoint": "https://connectors.example/api/v1/connectors",
            "allowed_hosts": ["connectors.example"],
            "enabled_connectors": ["feishu", "tencent-docs"],
        }

    product = _stage_product(tmp_path, config_mutator=configure)
    vault = InMemoryCredentialVault()
    _install_session(product, vault)
    server = build_product_runtime_server(
        host="127.0.0.1",
        port=8765,
        runtime_loader=_loader(product, vault),
    )
    adapters = server.composition.connector_adapters
    assert set(adapters) == {"feishu", "tencent-docs"}
    assert server.composition.server_settings.connector_adapters == adapters
    assert all(
        adapter.session is server.composition.managed_session
        for adapter in adapters.values()
    )
    assert server.composition.config.to_bytes() == product["config"].read_bytes()

    with TestClient(server.app, base_url=ORIGIN):
        assert all(not adapter.client.is_closed for adapter in adapters.values())
    assert all(adapter.client.is_closed for adapter in adapters.values())


def test_optional_service_endpoints_are_fixed_https_443_contracts(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path)
    base = json.loads(product["config"].read_text(encoding="utf-8"))
    base["share"] = {
        "endpoint": "https://control.example:8443/api/v1/shares",
        "allowed_hosts": ["control.example"],
        "public_hosts": ["share.example"],
    }
    with pytest.raises(ProductRuntimeConfigurationError, match="HTTPS"):
        ProductRuntimeConfig.from_bytes(
            json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
        )

    base = json.loads(product["config"].read_text(encoding="utf-8"))
    base["image_orchestration"] = {
        "root_url": "https://images.example/api/v1/free-form",
        "allowed_hosts": ["images.example"],
    }
    with pytest.raises(ProductRuntimeConfigurationError, match="unified v1 image root"):
        ProductRuntimeConfig.from_bytes(
            json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
        )

    base = json.loads(product["config"].read_text(encoding="utf-8"))
    base["retouch"] = {
        "endpoint": "https://images.example/v1/image/retouch",
        "allowed_hosts": ["images.example"],
    }
    with pytest.raises(ProductRuntimeConfigurationError, match="missing or unknown"):
        ProductRuntimeConfig.from_bytes(
            json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
        )

    base = json.loads(product["config"].read_text(encoding="utf-8"))
    base["audit"] = {
        "endpoint": "https://control.example/api/v1/audit/free-form",
        "allowed_hosts": ["control.example"],
        "dispatch_seconds": 5,
        "raw_retention_days": 30,
        "aggregate_retention_days": 180,
    }
    with pytest.raises(ProductRuntimeConfigurationError, match="audit ingestion"):
        ProductRuntimeConfig.from_bytes(
            json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
        )

    base["audit"]["endpoint"] = "https://control.example/api/v1/audit/records"
    base["audit"]["raw_retention_days"] = 31
    with pytest.raises(ProductRuntimeConfigurationError, match="raw retention"):
        ProductRuntimeConfig.from_bytes(
            json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
        )

    base = json.loads(product["config"].read_text(encoding="utf-8"))
    base["tracing"] = {
        "endpoint": "https://otel.example/v1/metrics",
        "allowed_hosts": ["otel.example"],
        "dispatch_seconds": 5,
        "max_spans_per_batch": 64,
        "max_request_bytes": 1048576,
        "retention_days": 7,
    }
    with pytest.raises(ProductRuntimeConfigurationError, match="/v1/traces"):
        ProductRuntimeConfig.from_bytes(
            json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
        )

    base["tracing"]["endpoint"] = "https://otel.example/v1/traces"
    base["tracing"]["max_request_bytes"] = 1024
    with pytest.raises(ProductRuntimeConfigurationError, match="request size"):
        ProductRuntimeConfig.from_bytes(
            json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
        )

    base = json.loads(product["config"].read_text(encoding="utf-8"))
    base["connectors"] = {
        "endpoint": "https://connectors.example/api/v1/free-form",
        "allowed_hosts": ["connectors.example"],
        "enabled_connectors": ["feishu"],
    }
    with pytest.raises(ProductRuntimeConfigurationError, match="connector root"):
        ProductRuntimeConfig.from_bytes(
            json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
        )

    base["connectors"]["endpoint"] = "https://connectors.example/api/v1/connectors"
    base["connectors"]["enabled_connectors"] = ["unknown"]
    with pytest.raises(ProductRuntimeConfigurationError, match="stable connector"):
        ProductRuntimeConfig.from_bytes(
            json.dumps(base, sort_keys=True, separators=(",", ":")).encode()
        )


def test_configured_capability_pack_requires_verified_artifact_and_trusted_adapter(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path)
    raw = json.loads(product["config"].read_text(encoding="utf-8"))
    raw["capability_packs"] = [
        {
            "pack_id": pack_id,
            "manifest": (
                f"capability-packs/{pack_id}/ecorex-capability-pack-"
                f"{pack_id}-windows-x64-{__version__}.json"
            ),
            "artifact": (
                f"capability-packs/{pack_id}/ecorex-capability-pack-"
                f"{pack_id}-windows-x64-{__version__}.zip"
            ),
        }
        for pack_id in REQUIRED_CAPABILITY_PACK_IDS
    ]
    parsed = ProductRuntimeConfig.from_bytes(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    )
    with pytest.raises(
        ProductRuntimeConfigurationError, match="trusted product adapter"
    ):
        load_verified_capability_packs(
            parsed,
            install_root=product["install_root"],
            verifier=Ed25519SignatureVerifier(parsed.release_public_keys),
            platform=product["platform"],
            architecture=product["architecture"],
            workspace_roots=(product["install_root"],),
            runtime_payload_root=product["payload"],
            resolver=None,
        )


def test_capability_pack_profiles_are_exact_and_share_one_catalog() -> None:
    assert CAPABILITY_PACK_PROFILES["minimal"] == ()
    assert CAPABILITY_PACK_PROFILES["workspace"] == ("sandbox",)
    assert CAPABILITY_PACK_PROFILES["full_offline"] == REQUIRED_CAPABILITY_PACK_IDS
    assert capability_pack_profile(()) == "minimal"
    assert capability_pack_profile(("sandbox",)) == "workspace"
    assert capability_pack_profile(REQUIRED_CAPABILITY_PACK_IDS) == "full_offline"
    assert capability_pack_profile(("image",)) is None


def test_config_or_ancestor_link_is_rejected(tmp_path: Path) -> None:
    product = _stage_product(tmp_path)
    linked = tmp_path / "payload-link"
    try:
        linked.symlink_to(product["payload"], target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    with pytest.raises(ProductRuntimeTrustError):
        load_product_runtime(
            payload_root=linked,
            environment={"ECOREX_BOOTSTRAPPED": "1"},
            vault_factory=InMemoryCredentialVault,
            host_platform=product["platform"],
            host_architecture=product["architecture"],
        )


def test_cli_contract_has_no_credential_arguments_and_redacts_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as rejected:
        product_main(["serve", "--bearer-token", "plaintext-process-secret"])
    assert rejected.value.code == 2
    assert "plaintext-process-secret" not in capsys.readouterr().err

    def unavailable(**_kwargs):
        raise ProductRuntimeTrustError(
            "native vault error contained plaintext-process-secret"
        )

    monkeypatch.setattr("ecorex.server.cli.build_product_runtime_server", unavailable)
    result = product_main(["serve", "--host", "127.0.0.1", "--port", "8765"])
    captured = capsys.readouterr()
    assert result == int(ProductRuntimeExitCode.TRUST_FAILURE)
    assert "plaintext-process-secret" not in captured.err

    def invalid_configuration(**_kwargs):
        raise ProductRuntimeConfigurationError(
            "native provider contained plaintext-configuration-secret",
            stage_code="update_runtime",
        )

    monkeypatch.setattr(
        "ecorex.server.cli.build_product_runtime_server", invalid_configuration
    )
    result = product_main(["serve", "--host", "127.0.0.1", "--port", "8765"])
    captured = capsys.readouterr()
    assert result == int(ProductRuntimeExitCode.CONFIGURATION)
    assert "e-Mate startup stage: update_runtime" in captured.err
    assert "plaintext-configuration-secret" not in captured.err


def test_cli_emits_only_safe_nonce_bound_startup_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = _stage_product(tmp_path)
    diagnostic_root = Path(product["install_root"]) / ".runtime-startup"
    diagnostic_root.mkdir()
    token = "A" * 43
    monkeypatch.chdir(product["payload"])
    monkeypatch.setenv(STARTUP_DIAGNOSTIC_TOKEN_ENV, token)

    def invalid_configuration(**_kwargs):
        raise ProductRuntimeConfigurationError(
            "native provider contained plaintext-configuration-secret",
            stage_code="capability_pack_binding",
        )

    monkeypatch.setattr(
        "ecorex.server.cli.build_product_runtime_server", invalid_configuration
    )

    result = product_main(["serve", "--host", "127.0.0.1", "--port", "8765"])

    assert result == int(ProductRuntimeExitCode.CONFIGURATION)
    diagnostic = json.loads(
        (diagnostic_root / f"{token}.json").read_text(encoding="utf-8")
    )
    assert diagnostic == {
        "schema_version": 1,
        "stage": "capability_pack_binding",
        "token": token,
    }


def test_failed_product_composition_closes_every_owned_transport_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecorex.server.config as product_config_module

    product = _stage_product(tmp_path)
    vault = InMemoryCredentialVault()
    broker = _DeviceBroker(product)
    gateway = _GatewayResource()
    captured_update = []
    original_build_update = product_config_module._build_update

    def build_update(*args, **kwargs):
        result = original_build_update(*args, **kwargs)
        captured_update.append(result)
        return result

    monkeypatch.setattr(product_config_module, "_build_update", build_update)
    monkeypatch.setattr(
        product_config_module,
        "ManagedModelGatewayClient",
        lambda *_args, **_kwargs: gateway,
    )

    def fail_reload_requester():
        raise RuntimeError("native failure plaintext-cleanup-secret")

    with pytest.raises(
        ProductRuntimeConfigurationError,
        match="dependency composition failed",
    ) as failure:
        load_product_runtime(
            payload_root=product["payload"],
            host="127.0.0.1",
            port=8765,
            environment={"ECOREX_BOOTSTRAPPED": "1"},
            vault_factory=lambda: vault,
            device_broker_factory=lambda _settings: broker,
            reload_requester_factory=fail_reload_requester,
            host_platform=product["platform"],
            host_architecture=product["architecture"],
        )
    assert "plaintext-cleanup-secret" not in str(failure.value)
    assert failure.value.stage_code == "session_reload_requester"
    assert broker.close_count == 1
    assert gateway.close_count == 1
    assert len(captured_update) == 1
    update = captured_update[0]
    assert update.feed.client.is_closed is True
    assert update.fetcher.client.is_closed is True
    assert update.signal_source._closed is True


def test_second_bundle_verification_failure_closes_completed_composition(
    tmp_path: Path,
) -> None:
    product = _stage_product(tmp_path)
    vault = InMemoryCredentialVault()
    broker = _DeviceBroker(product)
    reload_requester = _ReloadRequester()
    captured = []

    def race_loader(*, host: str, port: int):
        composition = _loader(
            product,
            vault,
            device_broker_factory=lambda _settings: broker,
            reload_requester_factory=lambda: reload_requester,
        )(host=host, port=port)
        captured.append(composition)
        index = Path(composition.server_settings.web_root) / "index.html"
        index.write_bytes(index.read_bytes() + b" ")
        return composition

    with pytest.raises(BundleIntegrityError):
        build_product_runtime_server(
            host="127.0.0.1",
            port=8765,
            runtime_loader=race_loader,
        )
    assert len(captured) == 1
    composition = captured[0]
    assert broker.close_count == 1
    assert composition.gateway.client.is_closed is True
    assert composition.update.feed.client.is_closed is True
    assert composition.update.fetcher.client.is_closed is True
    assert composition.update.signal_source._closed is True
    composition.close_unstarted()
    assert broker.close_count == 1


class _StartupStageComposition:
    def __init__(self) -> None:
        self.server_settings = object()
        self.close_count = 0
        self.transfer_count = 0

    def close_unstarted(self) -> None:
        self.close_count += 1

    def transfer_to_app(self) -> None:
        self.transfer_count += 1


def test_product_server_normalizes_runtime_composition_value_error() -> None:
    def invalid_loader(**_kwargs):
        raise ValueError("native-runtime-composition-secret")

    with pytest.raises(ProductRuntimeConfigurationError) as failure:
        build_product_runtime_server(
            host="127.0.0.1",
            port=8765,
            runtime_loader=invalid_loader,
        )

    assert failure.value.stage_code == "runtime_composition"
    assert "native-runtime-composition-secret" not in str(failure.value)


def test_product_server_sets_signed_timezone_authority_before_runtime_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[tuple[str, object]] = []

    monkeypatch.setattr(
        "ecorex.server.cli.zoneinfo.reset_tzpath",
        lambda paths: order.append(("timezone", paths)),
    )

    def invalid_loader(**_kwargs):
        order.append(("runtime", None))
        raise ValueError("expected-after-timezone-authority")

    with pytest.raises(ProductRuntimeConfigurationError):
        build_product_runtime_server(
            host="127.0.0.1",
            port=8765,
            runtime_loader=invalid_loader,
        )

    assert order == [("timezone", ()), ("runtime", None)]


@pytest.mark.parametrize("error_type", (ValueError, RuntimeError))
def test_product_server_normalizes_application_composition_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    composition = _StartupStageComposition()

    def invalid_app(_settings):
        raise error_type("native-application-composition-secret")

    monkeypatch.setattr("ecorex.server.cli.create_product_app", invalid_app)
    with pytest.raises(ProductRuntimeConfigurationError) as failure:
        build_product_runtime_server(
            host="127.0.0.1",
            port=8765,
            runtime_loader=lambda **_kwargs: composition,
        )

    assert failure.value.stage_code == "application_composition"
    assert "native-application-composition-secret" not in str(failure.value)
    assert composition.close_count == 1
    assert composition.transfer_count == 0


def test_product_server_preserves_fixed_application_substage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _StartupStageComposition()

    def invalid_app(_settings):
        raise ServerConfigurationError(
            "native-runtime-registration-secret",
            stage_code="runtime_registration",
        )

    monkeypatch.setattr("ecorex.server.cli.create_product_app", invalid_app)
    with pytest.raises(ProductRuntimeConfigurationError) as failure:
        build_product_runtime_server(
            host="127.0.0.1",
            port=8765,
            runtime_loader=lambda **_kwargs: composition,
        )

    assert failure.value.stage_code == "runtime_registration"
    assert "native-runtime-registration-secret" not in str(failure.value)
    assert composition.close_count == 1
    assert composition.transfer_count == 0


def test_product_server_quarantines_only_unreadable_observability_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = _StartupStageComposition()
    second = _StartupStageComposition()
    first.server_settings = SimpleNamespace(database_path=tmp_path / "runtime.sqlite3")
    second.server_settings = SimpleNamespace(database_path=tmp_path / "runtime.sqlite3")
    app = SimpleNamespace(state=SimpleNamespace())
    loaded = iter((first, second))
    recovered: list[Path] = []

    def loader(**_kwargs):
        return next(loaded)

    calls = 0

    def create_after_recovery(_settings):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AuditIntegrityError("stored audit payload authentication failed")
        return app

    monkeypatch.setattr("ecorex.server.cli.create_product_app", create_after_recovery)
    monkeypatch.setattr(
        "ecorex.server.cli.quarantine_unreadable_observability",
        lambda path: recovered.append(Path(path)),
    )
    monkeypatch.setattr(
        "ecorex.server.cli.build_uvicorn_config",
        lambda _app, _settings: object(),
    )

    server = build_product_runtime_server(
        host="127.0.0.1",
        port=8765,
        runtime_loader=loader,
    )

    assert server.app is app
    assert recovered == [tmp_path / "runtime.sqlite3"]
    assert first.close_count == 1
    assert first.transfer_count == 0
    assert second.close_count == 0
    assert second.transfer_count == 1


def test_product_server_normalizes_http_configuration_before_ownership_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = _StartupStageComposition()
    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr("ecorex.server.cli.create_product_app", lambda _settings: app)

    def invalid_http_config(_app, _settings):
        raise ValueError("native-http-configuration-secret")

    monkeypatch.setattr(
        "ecorex.server.cli.build_uvicorn_config",
        invalid_http_config,
    )
    with pytest.raises(ProductRuntimeConfigurationError) as failure:
        build_product_runtime_server(
            host="127.0.0.1",
            port=8765,
            runtime_loader=lambda **_kwargs: composition,
        )

    assert failure.value.stage_code == "http_server_configuration"
    assert "native-http-configuration-secret" not in str(failure.value)
    assert composition.close_count == 1
    assert composition.transfer_count == 0


def test_cli_classifies_uvicorn_bind_failure_without_echoing_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "ecorex.server.cli.build_product_runtime_server",
        lambda **_kwargs: SimpleNamespace(uvicorn_config=object()),
    )

    class FailingServer:
        def __init__(self, _config) -> None:
            pass

        def run(self) -> None:
            raise SystemExit("uvicorn-native-plaintext-secret")

    monkeypatch.setattr("ecorex.server.cli.uvicorn.Server", FailingServer)
    result = product_main(["serve", "--host", "127.0.0.1", "--port", "8765"])
    captured = capsys.readouterr()
    assert result == int(ProductRuntimeExitCode.CONFIGURATION)
    assert "e-Mate startup stage: http_server_bind" in captured.err
    assert "uvicorn-native-plaintext-secret" not in captured.err
