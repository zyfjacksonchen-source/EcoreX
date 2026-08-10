from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from ecorex.capabilities import (
    Exposure,
    SandboxLevel,
    ToolExecutionScope,
    ToolInvocationContext,
)
from ecorex.connectors import InMemoryCredentialVault
from ecorex.extensions import SkillReadFact, SkillSearchFact
from ecorex.integration import ImageGenerationToolHandler
from ecorex.observability import AuditIntegrityError
from ecorex.server import (
    BundleIntegrityError,
    ProductServerSettings,
    ServerConfigurationError,
    WebBundleManifest,
    WebFileRecord,
    build_uvicorn_config,
    create_product_app,
)
from ecorex.server.app import _runtime_owner_proof
from ecorex.session import (
    Ed25519SessionLeaseVerifier,
    ManagedSessionLeaseClaims,
    ManagedSessionService,
    SessionLeaseSignature,
    SignedManagedSessionLease,
    token_digest,
)
from ecorex.update import (
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
)


ORIGIN = "http://127.0.0.1:8765"


class _ProductGateway:
    async def stream(self, _request):
        raise AssertionError("idle Product worker must not call the model gateway")
        yield  # pragma: no cover

    async def aclose(self) -> None:
        return None


def _signature(private_key: Ed25519PrivateKey, payload: bytes) -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="test-key",
        value=base64.b64encode(private_key.sign(payload)).decode("ascii"),
    )


def _unsigned_signature() -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="test-key",
        value=base64.b64encode(b"0" * 64).decode("ascii"),
    )


def _write_signed_bundle(tmp_path: Path):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    web_root = tmp_path / "web"
    web_root.mkdir(parents=True)
    javascript = b"document.body.dataset.ready = 'true';\n"
    javascript_sha = hashlib.sha256(javascript).hexdigest()
    javascript_path = f"assets/app.{javascript_sha[:12]}.js"
    (web_root / "assets").mkdir()
    (web_root / javascript_path).write_bytes(javascript)
    index = (
        "<!doctype html><html><head>"
        "<!--__ECOREX_RUNTIME_CONFIG__-->"
        f'<script type="module" src="/{javascript_path}"></script>'
        "</head><body></body></html>"
    ).encode("utf-8")
    (web_root / "index.html").write_bytes(index)
    hub = (
        "<!doctype html><html><head>"
        "<!--__ECOREX_RUNTIME_CONFIG__-->"
        "<title>e-Mate 能力中心</title>"
        "</head><body>Skill Hub</body></html>"
    ).encode("utf-8")
    hub_sha = hashlib.sha256(hub).hexdigest()
    hub_path = f"assets/skill-hub-page.{hub_sha[:12]}.json"
    (web_root / hub_path).write_bytes(hub)

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
        WebFileRecord(
            path=hub_path,
            size_bytes=len(hub),
            sha256=hub_sha,
            immutable=True,
        ),
    )
    build_digest = hashlib.sha256(b"release-build").hexdigest()
    web_manifest = WebBundleManifest(
        schema_version=1,
        release_id="release-1.0.0-stable-001",
        version="1.0.0",
        build_digest=build_digest,
        bundle_sha256=WebBundleManifest.compute_bundle_sha256(files),
        entrypoint="index.html",
        files=files,
        signature=_unsigned_signature(),
    )
    web_manifest = replace(
        web_manifest,
        signature=_signature(private_key, web_manifest.canonical_payload()),
    )
    web_manifest_path = tmp_path / "web-manifest.json"
    web_manifest_path.write_bytes(web_manifest.to_json().encode("utf-8"))
    web_manifest_bytes = web_manifest_path.read_bytes()

    artifact = ReleaseArtifact(
        artifact_id="web-manifest",
        platform="all",
        architecture="all",
        file_name="web-manifest.json",
        size_bytes=len(web_manifest_bytes),
        sha256=hashlib.sha256(web_manifest_bytes).hexdigest(),
        signature=_unsigned_signature(),
    )
    artifact = replace(
        artifact,
        signature=_signature(
            private_key,
            artifact.signed_payload(
                release_id=web_manifest.release_id,
                version=web_manifest.version,
                build_digest=build_digest,
            ),
        ),
    )
    sources = (
        ReleaseSource(
            source_id="mirror",
            kind=SourceKind.GITHUB_CN_MIRROR,
            priority=0,
            base_url="https://mirror.example/releases",
        ),
        ReleaseSource(
            source_id="github",
            kind=SourceKind.GITHUB_RELEASE,
            priority=1,
            base_url="https://github.example/releases",
        ),
        ReleaseSource(
            source_id="cdn",
            kind=SourceKind.ECOREX_CDN,
            priority=2,
            base_url="https://cdn.example/releases",
        ),
    )
    release_manifest = ReleaseManifest(
        schema_version=1,
        release_id=web_manifest.release_id,
        version=web_manifest.version,
        build_digest=build_digest,
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T00:00:00+00:00",
        sources=sources,
        artifacts=(artifact,),
        signature=_unsigned_signature(),
    )
    release_manifest = replace(
        release_manifest,
        signature=_signature(private_key, release_manifest.canonical_payload()),
    )
    release_manifest_path = tmp_path / "release-manifest.json"
    release_manifest_path.write_text(release_manifest.to_json(), encoding="utf-8")
    return {
        "web_root": web_root,
        "javascript": javascript,
        "javascript_path": javascript_path,
        "web_manifest_path": web_manifest_path,
        "release_manifest_path": release_manifest_path,
        "public_keys": {"test-key": public_key},
    }


def _settings(
    tmp_path: Path,
    signed: dict,
    *,
    secret_factory=None,
    runtime_owner_nonce: str | None = None,
):
    return ProductServerSettings(
        database_path=tmp_path / "runtime.db",
        web_root=signed["web_root"],
        release_manifest_path=signed["release_manifest_path"],
        web_manifest_path=signed["web_manifest_path"],
        trusted_public_keys=signed["public_keys"],
        builtin_skill_root=Path(__file__).resolve().parents[2] / "skills",
        host="127.0.0.1",
        port=8765,
        secret_factory=secret_factory,
        allow_unmanaged_session_for_testing=True,
        workspace_roots=(tmp_path,),
        runtime_owner_nonce=runtime_owner_nonce,
    )


def _installed_managed_session(
    tmp_path: Path,
) -> tuple[ManagedSessionService, SignedManagedSessionLease]:
    session_key = Ed25519PrivateKey.generate()
    session_public = session_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    now = datetime.now(UTC).replace(microsecond=0)
    access_token = "product-managed-access-token"
    refresh_token = "product-managed-refresh-token"
    claims = ManagedSessionLeaseClaims(
        lease_id="product-session-lease",
        account_id="product-account",
        organization_id="product-organization",
        display_name="产品用户",
        roles=("member",),
        model_allowlist=("ecorex-chat",),
        quota={"managed_requests": 50},
        admin_denies=("shell",),
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        revision=1,
        access_token_sha256=token_digest(access_token),
        refresh_token_sha256=token_digest(refresh_token),
    )
    lease = SignedManagedSessionLease(
        claims=claims,
        signature=SessionLeaseSignature(
            algorithm="ed25519",
            key_id="product-session-key",
            value=base64.b64encode(session_key.sign(claims.canonical_payload())).decode(
                "ascii"
            ),
        ),
    )
    service = ManagedSessionService(
        tmp_path / "runtime.db",
        vault=InMemoryCredentialVault(),
        verifier=Ed25519SessionLeaseVerifier({"product-session-key": session_public}),
    )
    service.install(
        lease,
        access_token=access_token,
        refresh_token=refresh_token,
        client_request_id="product-managed-login",
    )
    return service, lease


def test_product_app_serves_verified_bundle_and_same_origin_runtime(tmp_path):
    signed = _write_signed_bundle(tmp_path)
    secrets = iter(
        [
            "x</script><img src=x onerror=alert(1)>" + "b" * 32,
            "c" * 64,
        ]
    )
    app = create_product_app(
        _settings(tmp_path, signed, secret_factory=lambda _bytes: next(secrets))
    )
    capability_service = app.state.runtime_composition.capability_service
    assert set(capability_service.handlers) == {
        "read",
        "skill_search",
        "skill_read",
        "skill_run",
        "task_list",
        "tool_search",
        "tool_describe",
        "connector_search",
        "connector_describe",
        "connector_read",
        "connector_write",
        "artifact_read",
        "input_attachment_read",
    }
    connector_runtime = app.state.runtime_composition.connector_agent_runtime
    artifact_runtime = app.state.runtime_composition.artifact_read_runtime
    input_attachment_runtime = (
        app.state.runtime_composition.input_attachment_read_runtime
    )
    assert connector_runtime is not None
    assert artifact_runtime is not None
    assert input_attachment_runtime is not None
    for tool_id in (
        "connector_search",
        "connector_describe",
        "connector_read",
        "connector_write",
    ):
        assert capability_service.handlers[tool_id].__self__ is connector_runtime
    assert capability_service.handlers["artifact_read"].__self__ is artifact_runtime
    assert (
        capability_service.handlers["input_attachment_read"].__self__
        is input_attachment_runtime
    )
    assert app.state.runtime_composition.availability.installed_packs == frozenset()
    assert app.state.runtime_composition.availability.disabled_tools == {
        "cdp": "verified_handler_not_installed",
        "fetch": "verified_handler_not_installed",
        "imagegen": "verified_handler_not_installed",
        "ocr": "input_attachment_ocr_runtime_not_bound",
        "shell": "verified_handler_not_installed",
        "vision": "verified_handler_not_installed",
    }
    specs = capability_service.registry.all()
    assert len(specs) == 19
    handlers = set(capability_service.handlers)
    pack_bound = {spec.tool_id for spec in specs if spec.required_packs}
    assert {spec.tool_id for spec in specs} == handlers | pack_bound
    for spec in specs:
        if spec.default_exposure is Exposure.DIRECT:
            assert spec.tool_id in handlers
            assert spec.tool_id not in (
                app.state.runtime_composition.availability.disabled_tools
            )
    client = TestClient(app, base_url=ORIGIN)

    index = client.get("/")
    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-store"
    assert "'nonce-" in index.headers["content-security-policy"]
    assert "window.__ECOREX_RUNTIME__" in index.text
    assert "<img src=x onerror=alert(1)>" not in index.text
    assert "\\u003c/script\\u003e" in index.text
    assert "csrf" not in index.text.casefold()
    second_index = client.get("/")
    assert (
        second_index.headers["content-security-policy"]
        != index.headers["content-security-policy"]
    )
    assert client.head("/").content == b""
    skill_hub = client.get("/ecorex-agent/skills/")
    assert skill_hub.status_code == 200
    assert "e-Mate 能力中心" in skill_hub.text
    assert "__ECOREX_RUNTIME_CONFIG__" not in skill_hub.text
    assert skill_hub.headers["content-security-policy"] != index.headers[
        "content-security-policy"
    ]

    bearer = app.state.runtime_bearer_token
    bootstrap = client.get(
        "/api/v1/bootstrap",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert bootstrap.status_code == 200
    csrf = bootstrap.json()["csrf_token"]
    created = client.post(
        "/api/v1/threads",
        json={"client_request_id": "server-test"},
        headers={
            "Authorization": f"Bearer {bearer}",
            "Origin": ORIGIN,
            "X-EcoreX-CSRF": csrf,
        },
    )
    assert created.status_code == 201

    for persisted in (path for path in tmp_path.rglob("*") if path.is_file()):
        payload = persisted.read_bytes()
        assert bearer.encode("utf-8") not in payload
        assert csrf.encode("utf-8") not in payload

    asset = client.get(f"/{signed['javascript_path']}")
    assert asset.content == signed["javascript"]
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert asset.headers["x-content-type-options"] == "nosniff"
    assert asset.headers["etag"]


def test_product_composes_message_channels_with_the_agent_runtime(tmp_path: Path) -> None:
    signed = _write_signed_bundle(tmp_path)
    managed_session, _lease = _installed_managed_session(tmp_path)
    app = create_product_app(
        replace(
            _settings(tmp_path, signed),
            model_gateway=_ProductGateway(),
            managed_session_service=managed_session,
            connector_vault=managed_session.vault,
        )
    )
    service = app.state.channel_self_service
    catalog = {item["channel_id"]: item for item in service.catalog()["items"]}
    channel_ids = {
        "dingtalk",
        "discord",
        "feishu",
        "qq",
        "slack",
        "telegram",
        "wecom_bot",
        "wechat_kf",
        "wechatcom_app",
        "wechatmp_service",
        "weixin",
    }

    assert set(service.adapters) == channel_ids
    assert all(
        catalog[channel_id]["adapter_available"] for channel_id in channel_ids
    )
    assert all(catalog[channel_id]["instance"] is None for channel_id in channel_ids)
    assert catalog["wechatmp"]["adapter_available"] is False
    assert catalog["wechatmp"]["unavailable_reason"] == "passive_runtime_unavailable"
    assert not any(catalog["wechatmp"]["actions"].values())
    assert app.state.channel_runtime_dispatcher is not None
    assert all(
        service.adapters[channel_id].health().health.value == "disabled"
        for channel_id in channel_ids
    )
    with TestClient(app, base_url=ORIGIN):
        assert service.adapters["telegram"].health().health.value == "disabled"


def test_installed_payload_builtin_skill_search_read_run_chain(tmp_path: Path) -> None:
    signed = _write_signed_bundle(tmp_path)
    builtin_skill_root = tmp_path / "payload" / "skills"
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "skills",
        builtin_skill_root,
    )
    app = create_product_app(
        replace(
            _settings(tmp_path, signed),
            builtin_skill_root=builtin_skill_root,
        )
    )
    service = app.state.extension_service
    runtime = app.state.runtime_composition.skill_runtime
    snapshot = service.snapshot()
    scope = ToolExecutionScope(
        job_id="job-installed-skill",
        thread_id="thread-installed-skill",
        turn_id="turn-installed-skill",
        execution_batch_id="batch-installed-skill",
    )
    context = ToolInvocationContext(
        invocation_id="invoke-installed-skill",
        capability_snapshot_id="cap-installed-skill",
        policy_snapshot_id="policy-installed-skill",
        tool_id="skill_search",
        idempotency_key=None,
        approved=True,
        effective_sandbox=SandboxLevel.WORKSPACE_WRITE,
        execution_scope=scope,
    )
    runtime.snapshot_resolver = lambda _scope: snapshot.snapshot_id
    runtime.turn_intent_resolver = lambda _scope: "Use office-presentations"
    handlers = runtime.handlers()

    search_arguments = {"query": "PowerPoint", "limit": 10}
    search = asyncio.run(handlers["skill_search"](search_arguments, context))
    assert [item["name"] for item in search["skills"]] == ["office-presentations"]
    discovery_id = search["skills"][0]["discovery_id"]
    search_digest = hashlib.sha256(
        json.dumps(
            search,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    runtime.search_fact_resolver = lambda *_args: SkillSearchFact(
        "search-installed-skill",
        search_arguments,
        search,
        search_digest,
    )

    read_arguments = {"discovery_id": discovery_id}
    read = asyncio.run(
        handlers["skill_read"](
            read_arguments,
            replace(context, tool_id="skill_read"),
        )
    )
    assert "PowerPoint" in read["instructions"]
    read_digest = hashlib.sha256(
        json.dumps(
            read,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    runtime.read_fact_resolver = lambda *_args: SkillReadFact(
        "read-installed-skill",
        read_arguments,
        read,
        read_digest,
    )

    class NativeRunner:
        def supports(self, skill) -> bool:
            return skill.extension_id == "skill.office-presentations"

        async def run(self, skill, parameters, context, *, state_fence):
            state_fence()
            return {"skill": skill.name, "parameters": dict(parameters)}

    runtime.bind_native_runner(NativeRunner())
    result = asyncio.run(
        handlers["skill_run"](
            {"discovery_id": discovery_id, "parameters": {"title": "Deck"}},
            replace(context, tool_id="skill_run"),
        )
    )
    assert result["result"] == {
        "skill": "office-presentations",
        "parameters": {"title": "Deck"},
    }


def test_acceptance_preview_is_visible_and_blocks_external_mutations(
    tmp_path,
    monkeypatch,
):
    signed = _write_signed_bundle(tmp_path)
    settings = replace(
        _settings(tmp_path, signed),
        acceptance_preview=True,
        model_gateway=_ProductGateway(),
        connector_vault=InMemoryCredentialVault(),
    )
    monkeypatch.setattr(
        "ecorex.server.app.ProjectWorkspaceAuthority",
        lambda _database: (_ for _ in ()).throw(
            AssertionError("preview must not authorize saved external project roots")
        ),
    )
    app = create_product_app(settings)
    telegram = next(
        item
        for item in app.state.channel_self_service.catalog()["items"]
        if item["channel_id"] == "telegram"
    )
    assert telegram["adapter_available"] is False
    assert app.state.channel_runtime_dispatcher is None
    with TestClient(app, base_url=ORIGIN) as client:
        index = client.get("/")
        assert '"mode":"acceptance-preview"' in index.text
        bearer = app.state.runtime_bearer_token
        blocked = client.post(
            "/api/v1/update/check",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == (
            "acceptance_preview_external_mutation_blocked"
        )

        login = client.post(
            "/api/v1/session/login",
            json={
                "identifier": "preview@example.com",
                "password": "not-a-real-secret",
                "client_request_id": "preview-login-policy-check",
            },
            headers={
                "Authorization": f"Bearer {bearer}",
                "Origin": ORIGIN,
                "X-EcoreX-CSRF": app.state.csrf_token,
            },
        )
        assert login.status_code == 405
        assert login.json().get("code") != (
            "acceptance_preview_external_mutation_blocked"
        )

        bootstrap = client.get(
            "/api/v1/bootstrap",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        csrf = bootstrap.json()["csrf_token"]
        created = client.post(
            "/api/v1/threads",
            json={"client_request_id": "preview-local-write"},
            headers={
                "Authorization": f"Bearer {bearer}",
                "Origin": ORIGIN,
                "X-EcoreX-CSRF": csrf,
            },
        )
        assert created.status_code == 201


def test_product_app_labels_runtime_registration_failure(tmp_path, monkeypatch):
    signed = _write_signed_bundle(tmp_path)

    def invalid_registration(**_kwargs):
        raise RuntimeError("native-runtime-registration-secret")

    monkeypatch.setattr("ecorex.server.app.register_runtime", invalid_registration)
    with pytest.raises(ServerConfigurationError) as failure:
        create_product_app(_settings(tmp_path, signed))

    assert failure.value.stage_code == "runtime_registration"
    assert "native-runtime-registration-secret" not in str(failure.value)


def test_product_app_preserves_unreadable_observability_signal(tmp_path, monkeypatch):
    signed = _write_signed_bundle(tmp_path)

    def unreadable_observability(**_kwargs):
        raise AuditIntegrityError("stored audit payload authentication failed")

    monkeypatch.setattr("ecorex.server.app.register_runtime", unreadable_observability)
    with pytest.raises(AuditIntegrityError, match="payload authentication failed"):
        create_product_app(_settings(tmp_path, signed))


def test_product_app_redacts_other_audit_integrity_failures(tmp_path, monkeypatch):
    signed = _write_signed_bundle(tmp_path)

    def invalid_observability(**_kwargs):
        raise AuditIntegrityError("native-observability-secret")

    monkeypatch.setattr("ecorex.server.app.register_runtime", invalid_observability)
    with pytest.raises(ServerConfigurationError) as failure:
        create_product_app(_settings(tmp_path, signed))

    assert failure.value.stage_code == "runtime_registration"
    assert "native-observability-secret" not in str(failure.value)


def test_runtime_owner_endpoint_proves_process_secret_without_disclosing_it(tmp_path):
    signed = _write_signed_bundle(tmp_path)
    nonce = "A" * 43
    challenge = "B" * 43
    app = create_product_app(_settings(tmp_path, signed, runtime_owner_nonce=nonce))
    client = TestClient(app, base_url=ORIGIN)

    missing = client.get("/api/v1/runtime-owner")
    wrong = client.get(
        "/api/v1/runtime-owner",
        headers={"X-EcoreX-Owner-Nonce": "B" * 43},
    )
    accepted = client.get(
        "/api/v1/runtime-owner",
        headers={"X-EcoreX-Owner-Challenge": challenge},
    )

    assert missing.status_code == 404
    assert wrong.status_code == 404
    assert accepted.status_code == 204
    assert accepted.headers["x-ecorex-runtime-owner"] == _runtime_owner_proof(
        nonce, challenge
    )
    assert nonce not in accepted.headers.values()
    assert accepted.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "tool_id",
    (
        "connector_search",
        "connector_describe",
        "connector_read",
        "connector_write",
        "artifact_read",
    ),
)
def test_product_app_refuses_to_replace_core_connector_or_artifact_handler(
    tmp_path,
    tool_id,
):
    signed = _write_signed_bundle(tmp_path)
    settings = replace(
        _settings(tmp_path, signed),
        capability_handlers={tool_id: lambda _arguments: {"replaced": True}},
    )

    with pytest.raises(
        ServerConfigurationError,
        match="Runtime API could not be composed",
    ) as failure:
        create_product_app(settings)
    assert failure.value.stage_code == "runtime_registration"


def test_product_app_disables_imagegen_when_managed_image_service_is_absent(
    tmp_path,
):
    signed = _write_signed_bundle(tmp_path)
    settings = replace(
        _settings(tmp_path, signed),
        capability_handlers={"imagegen": ImageGenerationToolHandler()},
    )
    app = create_product_app(settings)
    availability = app.state.runtime_composition.availability
    assert "imagegen" in app.state.runtime_composition.capability_service.handlers
    assert availability.disabled_tools["imagegen"] == (
        "managed_image_orchestration_not_configured"
    )


def test_product_app_rejects_legacy_feishu_cli_as_a_core_tool(tmp_path):
    signed = _write_signed_bundle(tmp_path)
    settings = replace(
        _settings(tmp_path, signed),
        capability_handlers={"feishu_cli": lambda _arguments: {}},
    )
    with pytest.raises(ValueError, match="unknown tool"):
        create_product_app(settings)


def test_product_settings_inject_cloud_authoritative_session(tmp_path):
    signed = _write_signed_bundle(tmp_path)
    service, lease = _installed_managed_session(tmp_path)
    settings = replace(
        _settings(tmp_path, signed),
        managed_session_service=service,
        connector_vault=service.vault,
        allow_unmanaged_session_for_testing=False,
    )
    app = create_product_app(settings)
    client = TestClient(app, base_url=ORIGIN)
    bootstrap = client.get(
        "/api/v1/bootstrap",
        headers={"Authorization": f"Bearer {app.state.runtime_bearer_token}"},
    )
    assert bootstrap.status_code == 200
    assert bootstrap.json()["login"]["account_id"] == "product-account"
    assert bootstrap.json()["login"]["display_name"] == "产品用户"
    assert bootstrap.json()["policy_lease"]["lease_id"] == lease.claims.lease_id


def test_spa_fallback_does_not_swallow_api_or_unknown_files(tmp_path):
    signed = _write_signed_bundle(tmp_path)
    app = create_product_app(_settings(tmp_path, signed))
    client = TestClient(app, base_url=ORIGIN)
    assert client.get("/threads/example").status_code == 200
    missing_api = client.get("/api/v1/not-a-route")
    assert missing_api.status_code == 401
    bearer = app.state.runtime_bearer_token
    missing_api = client.get(
        "/api/v1/not-a-route",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert missing_api.status_code == 404
    assert missing_api.headers["content-type"].startswith("application/json")
    assert client.get("/API/v1/not-a-route").status_code == 404
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/.env").status_code == 404
    assert client.get("/%2e%2e/secret.txt").status_code == 404
    assert client.get("/assets/%2e%2e/index.html").status_code == 404


def test_host_header_and_non_loopback_launcher_are_rejected(tmp_path):
    signed = _write_signed_bundle(tmp_path)
    settings = _settings(tmp_path, signed)
    app = create_product_app(settings)
    client = TestClient(app, base_url=ORIGIN)
    invalid_host = client.get("/", headers={"Host": "attacker.example"})
    assert invalid_host.status_code == 400
    assert invalid_host.headers["x-content-type-options"] == "nosniff"
    duplicate_host = client.get(
        "/",
        headers=[("Host", settings.authority), ("Host", "attacker.example")],
    )
    assert duplicate_host.status_code == 400
    config = build_uvicorn_config(app, settings)
    assert config.host == "127.0.0.1"
    assert config.port == 8765
    assert config.proxy_headers is False
    assert config.access_log is False
    with pytest.raises(ValueError, match="loopback"):
        replace(settings, host="0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        replace(settings, host="127.0.0.2")


def test_startup_fails_closed_for_tampered_bundle_or_web_manifest(tmp_path):
    signed = _write_signed_bundle(tmp_path)
    javascript_path = signed["web_root"] / signed["javascript_path"]
    javascript_path.write_bytes(b"x" * len(signed["javascript"]))
    with pytest.raises(BundleIntegrityError, match="SHA-256"):
        create_product_app(_settings(tmp_path, signed))

    signed = _write_signed_bundle(tmp_path / "second")
    signed["web_manifest_path"].write_bytes(
        signed["web_manifest_path"].read_bytes() + b" "
    )
    with pytest.raises(BundleIntegrityError, match="web manifest"):
        create_product_app(_settings(tmp_path / "second", signed))


def test_unlisted_web_root_file_is_not_accepted(tmp_path):
    signed = _write_signed_bundle(tmp_path)
    (signed["web_root"] / "stale.js").write_text("stale", encoding="utf-8")
    with pytest.raises(BundleIntegrityError, match="unlisted"):
        create_product_app(_settings(tmp_path, signed))


def test_startup_rejects_tampered_release_manifest(tmp_path):
    signed = _write_signed_bundle(tmp_path)
    raw = json.loads(signed["release_manifest_path"].read_text(encoding="utf-8"))
    raw["version"] = "1.0.1"
    signed["release_manifest_path"].write_text(
        json.dumps(raw),
        encoding="utf-8",
    )
    with pytest.raises(BundleIntegrityError, match="release manifest"):
        create_product_app(_settings(tmp_path, signed))


def test_startup_rejects_manifest_path_symlinks(tmp_path):
    signed = _write_signed_bundle(tmp_path)
    link = tmp_path / "release-link.json"
    try:
        link.symlink_to(signed["release_manifest_path"])
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    settings = replace(_settings(tmp_path, signed), release_manifest_path=link)
    with pytest.raises(BundleIntegrityError, match="regular non-link"):
        create_product_app(settings)
