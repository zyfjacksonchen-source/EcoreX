from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import threading
import time
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ecorex.capabilities import (
    CapabilitySnapshotRepository,
    RuntimeAvailability,
    builtin_capability_registry,
)
from ecorex.connectors import InMemoryCredentialVault, builtin_connector_registry
from ecorex.extensions import (
    EXTENSION_CONTRACT_VERSION,
    ExtensionActionUnavailable,
    ExtensionCompatibility,
    ExtensionDependencyError,
    ExtensionExport,
    ExtensionExportKind,
    ExtensionExposure,
    ExtensionIdempotencyConflict,
    ExtensionKind,
    ExtensionManifest,
    ExtensionManifestError,
    ExtensionProviderRevoked,
    ExtensionService,
    ExtensionSignature,
    ExtensionSource,
    ExtensionTransport,
    ExtensionTrust,
    ExtensionVerificationError,
    LocalSkillBundleStore,
    RuntimeBoundary,
    SQLiteExtensionRepository,
    builtin_extension_manifests,
    register_builtin_extensions,
    register_extension_routes,
    verify_extension_manifest,
    verify_core_extension,
)
from ecorex.update import SignatureEnvelope
from ecorex.runtime import RuntimeSettings, create_app


def _zip(files: dict[str, bytes | str], *, executable: str | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            info = zipfile.ZipInfo(path)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ((0o100755 if path == executable else 0o100644) << 16)
            archive.writestr(info, content)
    return output.getvalue()


def _skill(
    *,
    name: str = "Office helper",
    description: str = "Assists with bounded office work.",
    extra: str = "version: 1.2.3\ntags: [\"office\",\"review\"]",
) -> bytes:
    return (
        f"---\nname: {name}\ndescription: {description}\n{extra}\n---\n\n"
        "Use only capabilities selected by the Runtime policy.\n"
    ).encode()


def _service(tmp_path: Path, **kwargs) -> ExtensionService:
    registry = builtin_capability_registry()
    connectors = builtin_connector_registry()
    return ExtensionService(
        SQLiteExtensionRepository(tmp_path / "runtime.db"),
        runtime_api_version="1.0.0",
        platform="win32",
        architecture="x64",
        known_tool_ids=frozenset(spec.tool_id for spec in registry.all()),
        known_connector_ids=frozenset(
            definition.connector_id for definition in connectors.definitions()
        ),
        known_pack_ids=frozenset({"browser", "image", "sandbox"}),
        local_bundle_store=LocalSkillBundleStore(tmp_path / "extension-cas"),
        **kwargs,
    )


def test_extension_catalog_projection_is_read_only_until_authority_is_frozen(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    def snapshot_count() -> int:
        with service.repository.database.reader() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM extension_catalog_snapshots"
                ).fetchone()[0]
            )

    assert snapshot_count() == 0
    first = service.project_snapshot()
    second = service.project_snapshot()
    assert first == second
    assert snapshot_count() == 0

    app = FastAPI()
    register_extension_routes(app, service)
    response = TestClient(app).get("/api/v1/extensions")
    assert response.status_code == 200
    assert response.json()["snapshot_id"] == first.snapshot_id
    assert snapshot_count() == 0

    frozen = service.snapshot()
    assert frozen == first
    assert snapshot_count() == 1


def test_zip_and_directory_normalize_to_one_cas_revision(tmp_path: Path) -> None:
    files = {
        "SKILL.md": _skill(),
        "references/checklist.txt": b"one\ntwo\n",
        "assets/example.json": b'{"ok":true}',
    }
    source = tmp_path / "source"
    for path, content in files.items():
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())
    store = LocalSkillBundleStore(tmp_path / "cas")
    from_zip = store.ingest_zip(_zip(files))
    from_directory = store.ingest_directory(source)
    assert from_zip == from_directory
    assert store.verify(from_zip.artifact_sha256) == from_zip
    manifest = json.loads(
        (
            tmp_path
            / "cas"
            / "sha256"
            / from_zip.artifact_sha256[:2]
            / from_zip.artifact_sha256
            / "bundle.json"
        ).read_text(encoding="utf-8")
    )
    assert [item["path"] for item in manifest["files"]] == sorted(files)
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])


def test_script_skill_requires_a_normalized_runtime_manifest_and_reports_configuration(tmp_path: Path) -> None:
    runtime_manifest = json.dumps({
        "schema_version": 1,
        "runtime": "python",
        "entrypoint": "scripts/main.py",
        "environment": ["OFFICE_API_KEY"],
        "network_domains": [],
        "external_commands": [],
        "effects": ["read"],
    }, sort_keys=True, separators=(",", ":"))
    payload = _zip(
        {
            "SKILL.md": _skill(),
            "skill-runtime.json": runtime_manifest,
            "scripts/main.py": "import json\nprint(json.dumps({'ok': True}))\n",
        },
        executable="scripts/main.py",
    )
    service = _service(tmp_path, credential_vault=InMemoryCredentialVault())
    staged = service.install_local_skill_zip(
        payload,
        extension_id="local.script-helper",
        expected_revision=0,
        client_request_id="install:script-helper:1",
    )
    assert staged.readiness == "needs_configuration"
    assert staged.requirements == ("environment:OFFICE_API_KEY",)
    configure = next(action for action in staged.actions if action.action_id == "configure")
    assert configure.enabled is True
    app = FastAPI()
    register_extension_routes(app, service)
    response = TestClient(app).post(
        f"/api/v1/extensions/{staged.extension_id}/configure",
        json={
            "values": {"OFFICE_API_KEY": "secret-value"},
            "expected_revision": staged.revision,
            "client_request_id": "configure:script-helper:1",
        },
    )
    assert response.status_code == 200
    assert "secret-value" not in response.text
    configured = service.projection(staged.extension_id)
    assert configured.readiness == "unsupported"
    assert configured.requirements == ("controlled_runner_unavailable",)
    serialized = json.dumps(
        [event.payload for event in service.repository.events()],
        ensure_ascii=False,
    )
    assert "secret-value" not in serialized

    with pytest.raises(ExtensionManifestError, match="skill-runtime.json"):
        LocalSkillBundleStore(tmp_path / "other-cas").ingest_zip(
            _zip(
                {
                    "SKILL.md": _skill(),
                    "scripts/main.py": "print('undeclared')\n",
                },
                executable="scripts/main.py",
            )
        )

    node_manifest = json.dumps(
        {
            "schema_version": 1,
            "runtime": "node",
            "entrypoint": "scripts/main.mjs",
            "environment": [],
            "network_domains": [],
            "external_commands": [],
            "effects": ["read"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    LocalSkillBundleStore(tmp_path / "node-cas").ingest_zip(
        _zip(
            {
                "SKILL.md": _skill(),
                "skill-runtime.json": node_manifest,
                "scripts/main.mjs": "console.log(JSON.stringify({ok:true}))\n",
            }
        )
    )


@pytest.mark.parametrize(
    "files",
    [
        {"skill.md": _skill()},
        {"SKILL.md": _skill(), "../escape.txt": b"x"},
        {"SKILL.md": _skill(), "scripts/run.txt": b"x"},
        {"SKILL.md": _skill(), "resources/run.py": b"print(1)"},
        {"SKILL.md": _skill(), "A.txt": b"x", "a.txt": b"y"},
        {"SKILL.md": _skill(extra="version: 1.0.0\ncommand: calc.exe")},
    ],
)
def test_local_bundle_rejects_non_static_or_ambiguous_content(
    tmp_path: Path, files: dict[str, bytes]
) -> None:
    with pytest.raises(ExtensionManifestError):
        LocalSkillBundleStore(tmp_path / "cas").ingest_zip(_zip(files))


def test_local_bundle_rejects_executable_and_symlink(tmp_path: Path) -> None:
    store = LocalSkillBundleStore(tmp_path / "cas")
    with pytest.raises(ExtensionManifestError):
        store.ingest_zip(
            _zip(
                {"SKILL.md": _skill(), "references/help.txt": b"x"},
                executable="references/help.txt",
            )
        )
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_bytes(_skill())
    link = source / "linked.txt"
    try:
        link.symlink_to(source / "SKILL.md")
    except OSError:
        pytest.skip("the test account cannot create a symlink")
    with pytest.raises(ExtensionManifestError):
        store.ingest_directory(source)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode is not represented on Windows")
def test_posix_executable_declared_script_normalizes_like_zip(tmp_path: Path) -> None:
    runtime = json.dumps(
        {
            "schema_version": 1,
            "runtime": "python",
            "entrypoint": "scripts/main.py",
            "environment": [],
            "network_domains": [],
            "external_commands": [],
            "effects": ["read"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    files = {
        "SKILL.md": _skill(),
        "skill-runtime.json": runtime,
        "scripts/main.py": "print('ok')\n",
    }
    source = tmp_path / "source"
    for name, content in files.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())
    (source / "scripts/main.py").chmod(0o755)
    store = LocalSkillBundleStore(tmp_path / "cas")

    assert store.ingest_directory(source) == store.ingest_zip(
        _zip(files, executable="scripts/main.py")
    )

    forbidden = source / "references/help.txt"
    forbidden.parent.mkdir()
    forbidden.write_text("help", encoding="utf-8")
    forbidden.chmod(0o755)
    with pytest.raises(ExtensionManifestError, match="declared Skill script formats"):
        store.ingest_directory(source)


def test_local_skill_lifecycle_is_restart_safe_and_cas_fenced(tmp_path: Path) -> None:
    payload = _zip({"SKILL.md": _skill()})
    service = _service(tmp_path)
    staged = service.install_local_skill_zip(
        payload,
        extension_id="local.office-helper",
        expected_revision=0,
        client_request_id="install:local:1",
    )
    assert staged.status == "staged"
    enabled = asyncio.run(
        service.enable(
            staged.extension_id,
            expected_revision=staged.revision,
            client_request_id="enable:local:1",
        )
    )
    assert enabled.status == "enabled"
    restarted = _service(tmp_path)
    assert restarted.projection(staged.extension_id).status == "enabled"
    assert restarted.enabled_export_ids(ExtensionExportKind.SKILL) == {
        "local.office-helper"
    }

    manifest = restarted.repository.manifest(enabled.active_revision_id or "")
    cas_file = (
        tmp_path
        / "extension-cas"
        / "sha256"
        / manifest.artifact_sha256[:2]
        / manifest.artifact_sha256
        / "files"
        / "SKILL.md"
    )
    cas_file.write_bytes(b"tampered")
    snapshot = restarted.snapshot()
    availability = restarted.apply_availability(
        RuntimeAvailability(platform="win32"), snapshot
    )
    assert availability.installed_packs == frozenset()
    with pytest.raises(ExtensionVerificationError):
        restarted._reverify_revision(manifest)


def test_extension_generation_and_uninstall_tombstone_are_durable(tmp_path: Path) -> None:
    service = _service(tmp_path)
    staged = service.install_local_skill_zip(
        _zip({"SKILL.md": _skill()}),
        extension_id="local.uninstallable",
        expected_revision=0,
        client_request_id="install:uninstallable:1",
    )
    enabled = asyncio.run(
        service.enable(
            staged.extension_id,
            expected_revision=staged.revision,
            client_request_id="enable:uninstallable:1",
        )
    )
    before = service.snapshot()
    assert before.extension_generation == 2

    removed = service.uninstall(
        enabled.extension_id,
        expected_revision=enabled.revision,
        client_request_id="uninstall:uninstallable:1",
    )
    assert removed.status == "uninstalled"
    assert removed.active_revision_id is None
    assert service.project_snapshot().extension_generation == 3
    assert service.enabled_export_ids(ExtensionExportKind.SKILL) == set()
    assert service.repository.events(after_seq=2)[0].event_type == "extension.uninstalled"

    restarted = _service(tmp_path)
    assert restarted.projection(enabled.extension_id).status == "uninstalled"
    assert restarted.repository.generation() == 3
    assert restarted.import_legacy_skill_states() == 0
    with pytest.raises(ExtensionProviderRevoked):
        restarted.assert_export_invocable(
            before.snapshot_id,
            export_kind=ExtensionExportKind.SKILL,
            export_id=enabled.extension_id,
            expected_revision_id=enabled.active_revision_id,
        )


def test_extension_preflight_and_sync_probe_never_stall_event_loop(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    staged = service.install_local_skill_zip(
        _zip({"SKILL.md": _skill()}),
        extension_id="local.responsive",
        expected_revision=0,
        client_request_id="install:responsive:1",
    )

    def slow_probe(_manifest):
        time.sleep(0.15)
        return True

    service.bind_health_probe(staged.extension_id, slow_probe)

    async def scenario():
        started = asyncio.get_running_loop().time()
        pending = asyncio.create_task(
            service.enable(
                staged.extension_id,
                expected_revision=staged.revision,
                client_request_id="enable:responsive:1",
            )
        )
        await asyncio.sleep(0.02)
        loop_delay = asyncio.get_running_loop().time() - started
        result = await pending
        return loop_delay, result

    loop_delay, enabled = asyncio.run(scenario())
    assert loop_delay < 0.1
    assert enabled.status == "enabled"


def test_extension_probe_timeout_is_bounded_and_candidate_stays_inactive(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        health_probe_timeout_seconds=0.05,
        max_concurrent_health_probes=1,
    )
    staged = service.install_local_skill_zip(
        _zip({"SKILL.md": _skill()}),
        extension_id="local.slow-probe",
        expected_revision=0,
        client_request_id="install:slow-probe:1",
    )
    entered = threading.Event()
    release = threading.Event()

    def blocked_probe(_manifest):
        entered.set()
        release.wait(2)
        return True

    service.bind_health_probe(staged.extension_id, blocked_probe)

    async def scenario():
        started = asyncio.get_running_loop().time()
        result = await service.enable(
            staged.extension_id,
            expected_revision=staged.revision,
            client_request_id="enable:slow-probe:1",
        )
        elapsed = asyncio.get_running_loop().time() - started
        release.set()
        await asyncio.sleep(0.05)
        return elapsed, result

    elapsed, failed = asyncio.run(scenario())
    assert entered.is_set()
    assert elapsed < 0.5
    assert failed.status != "enabled"
    assert failed.last_error_code == "health_probe_timeout"


def test_legacy_import_can_be_staged_but_never_enabled_or_runtime_bound(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    with service.repository.database.transaction() as connection:
        connection.execute(
            "INSERT INTO skill_states("
            "skill_id,name,enabled,source,activation_status,metadata_json"
            ") VALUES (?,?,?,?,?,?)",
            (
                "old-1",
                "Old Skill",
                0,
                "local",
                "pending_contract_validation",
                "{}",
            ),
        )
    assert service.import_legacy_skill_states() == 1
    projection = service.catalog()[0]
    assert service.import_legacy_skill_states() == 1
    assert service.catalog()[0] == projection
    assert projection.source == "legacy_import"
    assert projection.actions[0].disabled_reason == "legacy_revalidation_required"
    with pytest.raises(ExtensionActionUnavailable, match="legacy_revalidation_required"):
        asyncio.run(
            service.enable(
                projection.extension_id,
                expected_revision=projection.revision,
                client_request_id="enable:legacy:1",
            )
        )


class _EvidenceVerifier:
    def __init__(self, accepted: set[str]) -> None:
        self.accepted = accepted

    def verify(self, _payload: bytes, signature: SignatureEnvelope) -> bool:
        if signature.value not in self.accepted:
            raise RuntimeError("revoked")
        return True


def _signed_manifest(signature_byte: int, *, tool_id: str = "read") -> ExtensionManifest:
    return ExtensionManifest(
        schema_version=1,
        contract_version=EXTENSION_CONTRACT_VERSION,
        extension_id="publisher.office-tools",
        version="1.0.0",
        kind=ExtensionKind.TOOL_PROVIDER,
        display_name="Published office tools",
        description="A publisher-verified provider.",
        artifact_sha256="b" * 64,
        source=ExtensionSource.SIGNED_RELEASE,
        trust=ExtensionTrust.VERIFIED_PUBLISHER,
        runtime_boundary=RuntimeBoundary.MANAGED_ADAPTER,
        transport=ExtensionTransport.NONE,
        compatibility=ExtensionCompatibility(
            runtime_api="=1.0.0", platforms=(), architectures=()
        ),
        dependencies=(),
        conflicts=(),
        exports=(
            ExtensionExport(
                export_id=tool_id,
                kind=ExtensionExportKind.TOOL,
                exposure=ExtensionExposure.DEFERRED,
                permission_effects=("read",),
            ),
        ),
        supported_protocol_versions=(),
        upstream_metadata=None,
        signature=ExtensionSignature(
            algorithm="ed25519",
            key_id="publisher-key",
            value=base64.b64encode(bytes([signature_byte]) * 64).decode(),
        ),
    )


def test_revision_identity_ignores_signature_rotation_and_evidence_reverifies(
    tmp_path: Path,
) -> None:
    first = _signed_manifest(1)
    rotated = _signed_manifest(2)
    assert first.revision_id == rotated.revision_id
    verifier = _EvidenceVerifier({first.signature.value, rotated.signature.value})
    service = _service(tmp_path, signature_verifier=verifier)
    service.install_verified(
        verify_extension_manifest(
            first,
            verifier=verifier,
            runtime_api_version="1.0.0",
            platform="win32",
            architecture="x64",
        ),
        expected_revision=0,
        client_request_id="install:signed:1",
    )
    service.install_verified(
        verify_extension_manifest(
            rotated,
            verifier=verifier,
            runtime_api_version="1.0.0",
            platform="win32",
            architecture="x64",
        ),
        expected_revision=1,
        client_request_id="install:signed:2",
    )
    assert len(service.repository.revisions(first.extension_id)) == 1
    assert len(service.repository.signature_evidence(first.revision_id)) == 2
    verifier.accepted = {rotated.signature.value}
    service._reverify_revision(first)
    verifier.accepted.clear()
    with pytest.raises(ExtensionVerificationError):
        service._reverify_revision(first)


def test_unknown_tool_export_and_idempotency_reuse_fail_closed(tmp_path: Path) -> None:
    verifier = _EvidenceVerifier({_signed_manifest(3).signature.value})
    service = _service(tmp_path, signature_verifier=verifier)
    unknown = _signed_manifest(3, tool_id="made-up-tool")
    verified = verify_extension_manifest(
        unknown,
        verifier=verifier,
        runtime_api_version="1.0.0",
        platform="win32",
        architecture="x64",
    )
    with pytest.raises(ExtensionDependencyError, match="unknown exact tool ID"):
        service.install_verified(
            verified,
            expected_revision=0,
            client_request_id="install:unknown:1",
        )

    local = _zip({"SKILL.md": _skill()})
    service.install_local_skill_zip(
        local,
        extension_id="local.one",
        expected_revision=0,
        client_request_id="same-request",
    )
    with pytest.raises(ExtensionIdempotencyConflict):
        service.install_local_skill_zip(
            local,
            extension_id="local.two",
            expected_revision=0,
            client_request_id="same-request",
        )


def test_builtin_catalog_availability_and_provider_revocation(tmp_path: Path) -> None:
    registry = builtin_capability_registry()
    connectors = builtin_connector_registry()
    service = _service(tmp_path)
    declarations = builtin_extension_manifests(
        product_version="1.0.0",
        core_build_digest="a" * 64,
        runtime_api_version="1.0.0",
        platform="win32",
        architecture="x64",
        capability_registry=registry,
        connector_registry=connectors,
        installed_pack_ids=frozenset({"image"}),
    )
    register_builtin_extensions(service, declarations)
    core_skill = replace(
        declarations[0].manifest,
        extension_id="ecorex.core.skill",
        kind=ExtensionKind.SKILL,
        runtime_boundary=RuntimeBoundary.DECLARATIVE,
        exports=(
            ExtensionExport(
                export_id="core.skill",
                kind=ExtensionExportKind.SKILL,
                exposure=ExtensionExposure.DEFERRED,
                permission_effects=(),
            ),
        ),
    )
    assert service._user_disable_disabled_reason(core_skill) is None
    snapshot = service.snapshot()
    availability = service.apply_availability(
        RuntimeAvailability(
            platform="win32",
            installed_packs=frozenset({"image"}),
            connected_connectors=frozenset({"feishu"}),
        ),
        snapshot,
    )
    assert availability.installed_packs == {"image"}
    assert availability.connected_connectors == {"feishu"}
    service.assert_tool_invocable(snapshot.snapshot_id, "imagegen")
    core = service.projection("ecorex.core.tools")
    assert core.category == "system"
    assert core.icon_key == "system"
    core_disable = next(action for action in core.actions if action.action_id == "disable")
    assert core_disable.enabled is False
    assert core_disable.disabled_reason == "extension_required_by_product"
    with pytest.raises(ExtensionActionUnavailable, match="extension_required_by_product"):
        service.disable(
            core.extension_id,
            expected_revision=core.revision,
            client_request_id="disable:core-tools:blocked",
        )
    pack = service.projection("ecorex.pack.image")
    service.disable(
        pack.extension_id,
        expected_revision=pack.revision,
        client_request_id="disable:image-pack:1",
    )
    with pytest.raises(ExtensionProviderRevoked):
        service.assert_tool_invocable(snapshot.snapshot_id, "imagegen")


def test_extension_api_is_thin_and_never_accepts_a_host_path(tmp_path: Path) -> None:
    service = _service(tmp_path)
    app = FastAPI()
    register_extension_routes(app, service)
    client = TestClient(app)
    payload = _zip({"SKILL.md": _skill()})
    installed = client.post(
        "/api/v1/extensions/local-skills",
        json={
            "extension_id": "local.api-skill",
            "bundle_base64": base64.b64encode(payload).decode(),
            "expected_revision": 0,
            "client_request_id": "install:api:1",
        },
    )
    assert installed.status_code == 201
    body = installed.json()
    assert body["extension"]["source"] == "local_bundle"
    assert body["extensions"]["snapshot_id"].startswith("ext_")
    assert client.get("/api/v1/extensions").json() == body["extensions"]
    rejected = client.post(
        "/api/v1/extensions/local-skills",
        json={
            "extension_id": "local.path",
            "directory": "C:/unsafe",
            "expected_revision": 0,
            "client_request_id": "install:path:1",
        },
    )
    assert rejected.status_code == 422


def test_mcp_runtime_binding_uses_only_the_stable_exact_protocol(tmp_path: Path) -> None:
    service = _service(tmp_path)
    digest = "c" * 64
    manifest = ExtensionManifest(
        schema_version=1,
        contract_version=EXTENSION_CONTRACT_VERSION,
        extension_id="ecorex.mcp.office",
        version="1.0.0",
        kind=ExtensionKind.MCP_SERVER,
        display_name="Office MCP",
        description="A Core-bound MCP adapter.",
        artifact_sha256=digest,
        source=ExtensionSource.CORE_BUNDLE,
        trust=ExtensionTrust.BUILTIN,
        runtime_boundary=RuntimeBoundary.MANAGED_ADAPTER,
        transport=ExtensionTransport.STREAMABLE_HTTP,
        compatibility=ExtensionCompatibility(
            runtime_api="=1.0.0", platforms=(), architectures=()
        ),
        dependencies=(),
        conflicts=(),
        exports=(
            ExtensionExport(
                export_id="ecorex.mcp.office",
                kind=ExtensionExportKind.MCP_SERVER,
                exposure=ExtensionExposure.DEFERRED,
                permission_effects=("network", "read"),
            ),
        ),
        supported_protocol_versions=("2025-11-25",),
        upstream_metadata=None,
        signature=ExtensionSignature(
            algorithm="core-slot-sha256",
            key_id="core-slot-v1",
            value=digest,
        ),
    )
    service.register_runtime_bound(
        verify_core_extension(
            manifest,
            runtime_api_version="1.0.0",
            platform="win32",
            architecture="x64",
        )
    )
    state = service.repository.require_state(manifest.extension_id)
    assert state.negotiated_protocol_version == "2025-11-25"
    assert state.catalog_digest is not None and len(state.catalog_digest) == 64
    invalid = manifest.to_dict()
    invalid["supported_protocol_versions"] = ["2024-11-05"]
    with pytest.raises(ExtensionManifestError, match="unsupported or draft protocol"):
        ExtensionManifest.from_dict(invalid)


def test_runtime_binding_accepts_changed_catalog_for_a_new_signed_revision(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    def verified(version: str, digest: str, effects: tuple[str, ...]):
        manifest = ExtensionManifest(
            schema_version=1,
            contract_version=EXTENSION_CONTRACT_VERSION,
            extension_id="ecorex.core.tools",
            version=version,
            kind=ExtensionKind.TOOL_PROVIDER,
            display_name="Core tools",
            description="A Core-bound tool provider.",
            artifact_sha256=digest,
            source=ExtensionSource.CORE_BUNDLE,
            trust=ExtensionTrust.BUILTIN,
            runtime_boundary=RuntimeBoundary.MANAGED_ADAPTER,
            transport=ExtensionTransport.NONE,
            compatibility=ExtensionCompatibility(
                runtime_api="=1.0.0", platforms=(), architectures=()
            ),
            dependencies=(),
            conflicts=(),
            exports=(
                ExtensionExport(
                    export_id="read",
                    kind=ExtensionExportKind.TOOL,
                    exposure=ExtensionExposure.DIRECT,
                    permission_effects=effects,
                ),
            ),
            supported_protocol_versions=(),
            upstream_metadata=None,
            signature=ExtensionSignature(
                algorithm="core-slot-sha256",
                key_id="core-slot-v1",
                value=digest,
            ),
        )
        return verify_core_extension(
            manifest,
            runtime_api_version="1.0.0",
            platform="win32",
            architecture="x64",
        )

    first = verified("1.0.0", "c" * 64, ("read",))
    service.register_runtime_bound(first)
    first_state = service.repository.require_state(first.manifest.extension_id)

    second = verified("1.0.1", "d" * 64, ("read", "write"))
    service.register_runtime_bound(second)
    second_state = service.repository.require_state(second.manifest.extension_id)

    assert second_state.active_revision_id == second.manifest.revision_id
    assert second_state.active_revision_id != first_state.active_revision_id
    assert second_state.catalog_digest != first_state.catalog_digest


def test_runtime_bootstrap_turn_job_and_revocation_share_one_extension_snapshot(
    tmp_path: Path,
) -> None:
    registry = builtin_capability_registry()
    connectors = builtin_connector_registry()
    service = _service(tmp_path)
    register_builtin_extensions(
        service,
        builtin_extension_manifests(
            product_version="1.0.0",
            core_build_digest="d" * 64,
            runtime_api_version="1.0.0",
            platform="win32",
            architecture="x64",
            capability_registry=registry,
            connector_registry=connectors,
            installed_pack_ids=frozenset({"image"}),
        ),
    )
    client = TestClient(
        create_app(
            settings=RuntimeSettings(
                database_path=tmp_path / "runtime.db",
                runtime_bearer_token="runtime-token-extension-test-00000000",
                csrf_token="csrf-token-extension-test-0000000000",
                webui_origins=("http://testserver",),
                platform="win32",
                architecture="x64",
                installed_capability_packs=frozenset({"image"}),
                extension_service=service,
            )
        )
    )
    auth = {"Authorization": "Bearer runtime-token-extension-test-00000000"}
    mutation = {
        **auth,
        "Origin": "http://testserver",
        "X-EcoreX-CSRF": "csrf-token-extension-test-0000000000",
    }
    bootstrap = client.get("/api/v1/bootstrap", headers=auth).json()
    snapshot_id = bootstrap["extensions"]["snapshot_id"]
    assert {item["extension_id"] for item in bootstrap["extensions"]["items"]} >= {
        "ecorex.core.tools",
        "ecorex.pack.image",
    }
    thread = client.post(
        "/api/v1/threads",
        headers=mutation,
        json={"title": "Extension fence", "client_request_id": "thread:extension:1"},
    ).json()
    accepted = client.post(
        f"/api/v1/threads/{thread['thread_id']}/turns",
        headers=mutation,
        json={
            "input": "请生成一张图片",
            "client_message_id": "message:extension:1",
            "metadata": {},
        },
    )
    assert accepted.status_code == 202
    turn_id = accepted.json()["turn"]["turn_id"]
    events = client.get(
        f"/api/v1/threads/{thread['thread_id']}/events?after_seq=0",
        headers=auth,
    ).json()["events"]
    turn_events = [event for event in events if event["turn_id"] == turn_id]
    assert turn_events
    assert {event["extension_snapshot_id"] for event in turn_events} == {snapshot_id}
    with service.repository.database.reader() as connection:
        context = connection.execute(
            "SELECT extension_snapshot_id FROM job_runtime_contexts "
            "WHERE job_id = ?",
            (accepted.json()["job"]["job_id"],),
        ).fetchone()
    assert context["extension_snapshot_id"] == snapshot_id

    image_pack = next(
        item
        for item in bootstrap["extensions"]["items"]
        if item["extension_id"] == "ecorex.pack.image"
    )
    assert image_pack["category"] == "image_media"
    assert image_pack["icon_key"] == "image"
    disabled = client.post(
        "/api/v1/extensions/ecorex.pack.image/disable",
        headers=mutation,
        json={
            "expected_revision": image_pack["revision"],
            "client_request_id": "disable:image:runtime:1",
        },
    )
    assert disabled.status_code == 200
    with pytest.raises(ExtensionProviderRevoked):
        service.assert_tool_invocable(snapshot_id, "imagegen")


def test_skill_metadata_cannot_claim_a_reserved_core_tool_reference(
    tmp_path: Path,
) -> None:
    registry = builtin_capability_registry()
    connectors = builtin_connector_registry()
    service = _service(tmp_path)
    register_builtin_extensions(
        service,
        builtin_extension_manifests(
            product_version="1.0.0",
            core_build_digest="e" * 64,
            runtime_api_version="1.0.0",
            platform="win32",
            architecture="x64",
            capability_registry=registry,
            connector_registry=connectors,
            installed_pack_ids=frozenset({"image"}),
        ),
    )
    staged = service.install_local_skill_zip(
        _zip({"SKILL.md": _skill(name="imagegen")}),
        extension_id="local.colliding-skill",
        expected_revision=0,
        client_request_id="install:reserved-skill:1",
    )
    asyncio.run(
        service.enable(
            staged.extension_id,
            expected_revision=staged.revision,
            client_request_id="enable:reserved-skill:1",
        )
    )
    token = "runtime-token-reserved-skill-000000"
    csrf = "csrf-token-reserved-skill-000000000"
    client = TestClient(
        create_app(
            settings=RuntimeSettings(
                database_path=tmp_path / "runtime.db",
                runtime_bearer_token=token,
                csrf_token=csrf,
                webui_origins=("http://testserver",),
                platform="win32",
                architecture="x64",
                installed_capability_packs=frozenset({"image"}),
                capability_handlers={
                    "imagegen": lambda arguments, context: {"ok": True},
                },
                extension_service=service,
            )
        )
    )
    auth = {"Authorization": f"Bearer {token}"}
    mutation = {
        **auth,
        "Origin": "http://testserver",
        "X-EcoreX-CSRF": csrf,
    }

    def plan_for(message: str, client_message_id: str):
        thread = client.post(
            "/api/v1/threads",
            headers=mutation,
            json={"client_request_id": f"thread:{client_message_id}"},
        ).json()
        accepted = client.post(
            f"/api/v1/threads/{thread['thread_id']}/turns",
            headers=mutation,
            json={"input": message, "client_message_id": client_message_id},
        )
        assert accepted.status_code == 202
        event = next(
            item
            for item in client.get(
                f"/api/v1/threads/{thread['thread_id']}/events?after_seq=0",
                headers=auth,
            ).json()["events"]
            if item["event_type"] == "turn.accepted"
        )
        return CapabilitySnapshotRepository(tmp_path / "runtime.db").get(
            event["capability_snapshot_id"]
        )

    colliding = plan_for("使用 imagegen 生成一张图片", "message:reserved-skill:1")
    image = colliding.decision("imagegen")
    skill_read = colliding.decision("skill_read")
    assert image is not None and "explicit_reference" in image.reason_codes
    assert skill_read is not None and "explicit_reference" not in skill_read.reason_codes
    assert colliding.unresolved_explicit == (
        "reserved-skill-reference:imagegen",
    )

    unambiguous = plan_for(
        "使用 local.colliding-skill",
        "message:reserved-skill:2",
    )
    selected_skill = unambiguous.decision("skill_read")
    assert selected_skill is not None
    assert "explicit_reference" not in selected_skill.reason_codes
    assert unambiguous.unresolved_explicit == ()
