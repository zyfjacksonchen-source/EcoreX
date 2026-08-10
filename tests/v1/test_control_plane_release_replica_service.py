from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
import httpx
import pytest

from ecorex._version import __version__
from ecorex.control_plane.release_replica import (
    CDNReleaseReplicaService,
    CloudReleaseReplicaAuditSink,
    EnvironmentRotatingReleaseReplicaTokenVerifier,
    RELEASE_REPLICA_TOKEN_CURRENT_ENV,
    RELEASE_REPLICA_TOKEN_NEXT_ENV,
    ReleaseReplicaServiceError,
    create_cdn_release_replica_router,
)
from ecorex.control_plane.audit import CloudAuditRepository
from ecorex.control_plane.audit_schema import migrate_cloud_audit_database
from ecorex.control_plane.models import ControlPrincipal
from ecorex.deployment import cloud_sidecar as cloud_deployment
from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    ReleaseBuilder,
    ReleaseBuildSpec,
)
from ecorex.release.candidate import PACK_TOOLS
from ecorex.update import (
    Ed25519SignatureVerifier,
    ReleaseChannel,
    ReleaseSource,
    SourceKind,
)


TOKEN_CURRENT = "c" * 48
TOKEN_NEXT = "n" * 48
PUBLIC_ROOT = "https://dl.ecoremedia.net/ecorex-agent/releases"
PRODUCT_VERSION = __version__
RELEASE_NAMESPACE = f"v{PRODUCT_VERSION}"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class AuditSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, **values: Any) -> None:
        encoded = json.dumps(values, default=str, sort_keys=True)
        assert TOKEN_CURRENT not in encoded
        assert TOKEN_NEXT not in encoded
        assert "\\" not in encoded
        self.events.append(values)


class StreamingRequest:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks

    async def stream(self):
        for chunk in self.chunks:
            yield chunk


def _built_release(tmp_path: Path):
    source = tmp_path / "source"
    (source / "bin").mkdir(parents=True)
    (source / "bin" / "ecorex.exe").write_bytes(b"signed ecorex product")
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    spec = ReleaseBuildSpec(
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-16T08:00:00+00:00",
        release_scoped_sources=True,
        sources=(
            ReleaseSource(
                "github-cn",
                SourceKind.GITHUB_CN_MIRROR,
                0,
                "https://ghproxy.net/https://github.com/acme/ecorex/releases/download",
            ),
            ReleaseSource(
                "github",
                SourceKind.GITHUB_RELEASE,
                1,
                "https://github.com/acme/ecorex/releases/download",
            ),
            ReleaseSource(
                "cdn",
                SourceKind.ECOREX_CDN,
                2,
                f"{PUBLIC_ROOT}/{RELEASE_NAMESPACE}",
            ),
        ),
        artifacts=(
            ArtifactBuildInput(
                source_dir=source,
                kind=ArtifactKind.CORE,
                platform="windows",
                architecture="x64",
                executable_paths=("bin/ecorex.exe",),
            ),
        ),
    )
    built = ReleaseBuilder(Ed25519MemorySigner("release-key", private)).build(
        spec, tmp_path / "release"
    )
    return built, public


def _service(tmp_path: Path, public: bytes, *, maximum: int = 1024 * 1024):
    root = tmp_path / "replica"
    root.mkdir()
    namespace_root = root / RELEASE_NAMESPACE
    namespace_root.mkdir()
    (namespace_root / "stable").mkdir()
    (namespace_root / "canary").mkdir()
    environment = {
        RELEASE_REPLICA_TOKEN_CURRENT_ENV: TOKEN_CURRENT,
        RELEASE_REPLICA_TOKEN_NEXT_ENV: TOKEN_NEXT,
    }
    audit = AuditSink()
    service = CDNReleaseReplicaService(
        storage_root=root.resolve(),
        public_root=PUBLIC_ROOT,
        release_namespace=RELEASE_NAMESPACE,
        product_version=PRODUCT_VERSION,
        verifier=Ed25519SignatureVerifier({"release-key": public}),
        token_verifier=EnvironmentRotatingReleaseReplicaTokenVerifier(environment),
        audit_sink=audit,
        max_asset_bytes=maximum,
    )
    app = FastAPI()
    app.include_router(create_cdn_release_replica_router(service))
    return service, app, audit, environment


def _headers(release_id: str, name: str, payload: bytes, token: str = TOKEN_CURRENT):
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Length": str(len(payload)),
        "X-EcoreX-Size": str(len(payload)),
        "X-EcoreX-SHA256": digest,
        "Idempotency-Key": f"cdn:{release_id}:{name}:{digest}",
    }


async def _upload_all(client: httpx.AsyncClient, built, token: str = TOKEN_CURRENT):
    release_id = built.manifest.release_id
    responses = []
    for path in sorted(built.output_dir.iterdir(), key=lambda item: item.name):
        payload = path.read_bytes()
        responses.append(
            await client.put(
                f"/api/v1/releases/{release_id}/replicas/cdn/assets/{path.name}",
                headers=_headers(release_id, path.name, payload, token),
                content=payload,
            )
        )
    return responses


@pytest.mark.anyio
async def test_recipe_manifest_source_finalizes_through_production_replica(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    input_root = tmp_path / "candidate-input"
    for platform, architecture in (
        ("windows", "x64"),
        ("macos", "arm64"),
        ("macos", "x64"),
    ):
        target = f"{platform}-{architecture}"
        (input_root / "stages" / target / "core").mkdir(parents=True)
        (input_root / "stages" / target / "bootstrap").mkdir(parents=True)
        for pack_id in PACK_TOOLS:
            (input_root / "stages" / target / "packs" / pack_id).mkdir(
                parents=True
            )
        receipts = input_root / "receipts" / target
        receipts.mkdir(parents=True)
        for name in ("core", "bootstrap", *PACK_TOOLS):
            (receipts / f"{name}.json").write_text("{}", encoding="utf-8")

    recipe_path = input_root / "candidate-recipe.json"
    assembled = subprocess.run(
        (
            sys.executable,
            str(repository / "scripts" / "assemble-v1-candidate-recipe.py"),
            "--input-root",
            str(input_root),
            "--output",
            str(recipe_path),
            "--channel",
            "stable",
            "--created-at",
            "2026-07-17T00:00:00+00:00",
            "--repository",
            "acme/ecorex",
        ),
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "ECOREX_RELEASE_MIRROR_BASE_URL": (
                "https://ghproxy.net/https://github.com/"
                "acme/ecorex/releases/download"
            ),
            "ECOREX_RELEASE_CDN_BASE_URL": (
                f"{PUBLIC_ROOT}/{RELEASE_NAMESPACE}"
            ),
        },
    )
    assert assembled.returncode == 0, assembled.stderr.decode(errors="replace")
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    assert recipe["sources"][2]["base_url"] == (
        f"{PUBLIC_ROOT}/{RELEASE_NAMESPACE}"
    )

    source = tmp_path / "source"
    (source / "bin").mkdir(parents=True)
    (source / "bin" / "ecorex.exe").write_bytes(b"signed ecorex product")
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    sources = tuple(
        ReleaseSource(
            str(item["source_id"]),
            SourceKind(str(item["kind"])),
            priority,
            str(item["base_url"]),
        )
        for priority, item in enumerate(recipe["sources"])
    )
    built = ReleaseBuilder(Ed25519MemorySigner("release-key", private)).build(
        ReleaseBuildSpec(
            channel=ReleaseChannel.STABLE,
            created_at=str(recipe["created_at"]),
            release_scoped_sources=True,
            sources=sources,
            artifacts=(
                ArtifactBuildInput(
                    source_dir=source,
                    kind=ArtifactKind.CORE,
                    platform="windows",
                    architecture="x64",
                    executable_paths=("bin/ecorex.exe",),
                ),
            ),
        ),
        tmp_path / "release",
    )
    release_id = built.manifest.release_id
    assert built.manifest.sources[2].base_url == (
        f"{PUBLIC_ROOT}/{RELEASE_NAMESPACE}/{release_id}"
    )

    service, app, _audit, _environment = _service(tmp_path, public)
    manifest_digest = hashlib.sha256(built.manifest_path.read_bytes()).hexdigest()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await _upload_all(client, built)
        assert {response.status_code for response in responses} == {201}
        body = json.dumps({"manifest_sha256": manifest_digest}).encode()
        finalized = await client.post(
            f"/api/v1/releases/{release_id}/replicas/cdn/finalize",
            headers={
                **_finalize_headers(release_id, manifest_digest),
                "Content-Length": str(len(body)),
                "Content-Type": "application/json",
            },
            content=body,
        )
    assert finalized.status_code == 200, finalized.text
    assert (
        service.namespace_root / ReleaseChannel.STABLE.value / release_id
    ).is_dir()


def _finalize_headers(release_id: str, digest: str, token: str = TOKEN_CURRENT):
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": f"finalize:cdn:{release_id}:{digest}",
    }


@pytest.mark.anyio
async def test_cdn_replica_upload_finalize_retry_rotation_and_durable_audit(
    tmp_path: Path,
) -> None:
    built, public = _built_release(tmp_path)
    service, app, audit, environment = _service(tmp_path, public)
    release_id = built.manifest.release_id
    manifest_digest = hashlib.sha256(built.manifest_path.read_bytes()).hexdigest()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await _upload_all(client, built)
        assert {response.status_code for response in responses} == {201}
        body = json.dumps({"manifest_sha256": manifest_digest}).encode()
        finalized = await client.post(
            f"/api/v1/releases/{release_id}/replicas/cdn/finalize",
            headers={
                **_finalize_headers(release_id, manifest_digest, TOKEN_NEXT),
                "Content-Length": str(len(body)),
                "Content-Type": "application/json",
            },
            content=body,
        )
        assert finalized.status_code == 200, finalized.text
        assert finalized.json() == {
            "release_id": release_id,
            "source_id": "cdn",
            "state": "ready",
            "manifest_sha256": manifest_digest,
        }
        # Simulate completion of current -> next rotation without restarting.
        environment[RELEASE_REPLICA_TOKEN_CURRENT_ENV] = TOKEN_NEXT
        environment[RELEASE_REPLICA_TOKEN_NEXT_ENV] = ""
        replay = await client.post(
            f"/api/v1/releases/{release_id}/replicas/cdn/finalize",
            headers={
                **_finalize_headers(release_id, manifest_digest, TOKEN_NEXT),
                "Content-Length": str(len(body)),
                "Content-Type": "application/json",
            },
            content=body,
        )
        assert replay.status_code == 200
        rejected = await client.post(
            f"/api/v1/releases/{release_id}/replicas/cdn/finalize",
            headers={
                **_finalize_headers(release_id, manifest_digest, TOKEN_CURRENT),
                "Content-Length": str(len(body)),
                "Content-Type": "application/json",
            },
            content=body,
        )
        assert rejected.status_code == 401

    published = service.namespace_root / "stable" / release_id
    assert published.is_dir()
    assert not (service.namespace_root / "stable" / f".{release_id}.staging").exists()
    expected = {path.name for path in built.output_dir.iterdir()} | {".ready.json"}
    assert {path.name for path in published.iterdir()} == expected
    assert stat.S_IMODE(published.stat().st_mode) & 0o055 == 0o055
    assert {event["event_type"] for event in audit.events} == {
        "release.replica.asset.ready",
        "release.replica.finalized",
    }


@pytest.mark.anyio
async def test_concurrent_same_asset_is_idempotent_and_conflict_never_overwrites(
    tmp_path: Path,
) -> None:
    built, public = _built_release(tmp_path)
    _service_instance, app, _audit, _environment = _service(tmp_path, public)
    path = built.manifest_path
    payload = path.read_bytes()
    release_id = built.manifest.release_id
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async def send(data: bytes):
            return await client.put(
                f"/api/v1/releases/{release_id}/replicas/cdn/assets/{path.name}",
                headers=_headers(release_id, path.name, data),
                content=data,
            )

        first, second = await asyncio.gather(send(payload), send(payload))
        assert sorted((first.status_code, second.status_code)) == [200, 201]
        conflict = await send(payload + b"different")
        assert conflict.status_code == 409


@pytest.mark.anyio
async def test_finalize_and_upload_are_serialized_and_retry_converges(
    tmp_path: Path,
) -> None:
    built, public = _built_release(tmp_path)
    _service_instance, app, _audit, _environment = _service(tmp_path, public)
    release_id = built.manifest.release_id
    manifest_digest = hashlib.sha256(built.manifest_path.read_bytes()).hexdigest()
    paths = sorted(built.output_dir.iterdir(), key=lambda item: item.name)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in paths[:-1]:
            payload = path.read_bytes()
            response = await client.put(
                f"/api/v1/releases/{release_id}/replicas/cdn/assets/{path.name}",
                headers=_headers(release_id, path.name, payload),
                content=payload,
            )
            assert response.status_code == 201
        last = paths[-1]
        last_payload = last.read_bytes()
        body = json.dumps({"manifest_sha256": manifest_digest}).encode()
        upload, finalize = await asyncio.gather(
            client.put(
                f"/api/v1/releases/{release_id}/replicas/cdn/assets/{last.name}",
                headers=_headers(release_id, last.name, last_payload),
                content=last_payload,
            ),
            client.post(
                f"/api/v1/releases/{release_id}/replicas/cdn/finalize",
                headers={
                    **_finalize_headers(release_id, manifest_digest),
                    "Content-Length": str(len(body)),
                    "Content-Type": "application/json",
                },
                content=body,
            ),
        )
        assert upload.status_code in {200, 201}
        if finalize.status_code != 200:
            assert finalize.status_code == 409
            finalize = await client.post(
                f"/api/v1/releases/{release_id}/replicas/cdn/finalize",
                headers={
                    **_finalize_headers(release_id, manifest_digest),
                    "Content-Length": str(len(body)),
                    "Content-Type": "application/json",
                },
                content=body,
            )
        assert finalize.status_code == 200


@pytest.mark.anyio
async def test_stream_over_declared_size_is_stopped_and_temp_removed(tmp_path: Path) -> None:
    _built, public = _built_release(tmp_path)
    service, _app, _audit, _environment = _service(tmp_path, public, maximum=8)
    release_id = "release-stable-" + "a" * 24
    digest = hashlib.sha256(b"12345678").hexdigest()
    with pytest.raises(ReleaseReplicaServiceError, match="size_mismatch") as failure:
        await service.upload(
            StreamingRequest((b"1234", b"56789")),  # type: ignore[arg-type]
            release_id=release_id,
            name="core.zip",
            size_bytes=8,
            sha256=digest,
        )
    assert failure.value.status_code == 413
    staging = service.namespace_root / "stable" / f".{release_id}.staging"
    assert staging.is_dir()
    assert tuple(staging.iterdir()) == ()


@pytest.mark.anyio
async def test_bad_manifest_crash_residue_symlink_and_ready_tamper_fail_closed(
    tmp_path: Path,
) -> None:
    built, public = _built_release(tmp_path)
    service, app, _audit, _environment = _service(tmp_path, public)
    release_id = built.manifest.release_id
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await _upload_all(client, built)
        assert all(response.status_code == 201 for response in responses)
        stage = service.namespace_root / "stable" / f".{release_id}.staging"
        residue = stage / (".core.zip." + "0" * 32 + ".part")
        residue.write_bytes(b"crash")
        manifest_digest = hashlib.sha256(built.manifest_path.read_bytes()).hexdigest()
        body = json.dumps({"manifest_sha256": manifest_digest}).encode()
        response = await client.post(
            f"/api/v1/releases/{release_id}/replicas/cdn/finalize",
            headers={
                **_finalize_headers(release_id, manifest_digest),
                "Content-Length": str(len(body)),
                "Content-Type": "application/json",
            },
            content=body,
        )
        assert response.status_code == 200
        assert not residue.exists()
        published = service.namespace_root / "stable" / release_id
        marker = published / ".ready.json"
        original = marker.read_bytes()
        marker.write_bytes(original.replace(b'"source_id":"cdn"', b'"source_id":"bad"'))
        retry = await client.post(
            f"/api/v1/releases/{release_id}/replicas/cdn/finalize",
            headers={
                **_finalize_headers(release_id, manifest_digest),
                "Content-Length": str(len(body)),
                "Content-Type": "application/json",
            },
            content=body,
        )
        assert retry.status_code == 409

    other_root = tmp_path / "other"
    other_root.mkdir()
    symlink_root = tmp_path / "symlink-root"
    try:
        symlink_root.symlink_to(other_root, target_is_directory=True)
    except OSError:
        return
    with pytest.raises((ValueError, ReleaseReplicaServiceError)):
        CDNReleaseReplicaService(
            storage_root=symlink_root,
            public_root=PUBLIC_ROOT,
            release_namespace=RELEASE_NAMESPACE,
            product_version=PRODUCT_VERSION,
            verifier=Ed25519SignatureVerifier({"release-key": public}),
            token_verifier=EnvironmentRotatingReleaseReplicaTokenVerifier(
                {RELEASE_REPLICA_TOKEN_CURRENT_ENV: TOKEN_CURRENT}
            ),
        )


@pytest.mark.anyio
async def test_invalid_manifest_signature_never_creates_public_release(tmp_path: Path) -> None:
    built, public = _built_release(tmp_path)
    service, app, _audit, _environment = _service(tmp_path, public)
    release_id = built.manifest.release_id
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _upload_all(client, built)
        stage_manifest = (
            service.namespace_root
            / "stable"
            / f".{release_id}.staging"
            / "release-manifest.json"
        )
        value = json.loads(stage_manifest.read_bytes())
        value["signature"]["value"] = base64.b64encode(b"x" * 64).decode()
        tampered = (json.dumps(value, sort_keys=True) + "\n").encode()
        stage_manifest.write_bytes(tampered)
        digest = hashlib.sha256(tampered).hexdigest()
        body = json.dumps({"manifest_sha256": digest}).encode()
        response = await client.post(
            f"/api/v1/releases/{release_id}/replicas/cdn/finalize",
            headers={
                **_finalize_headers(release_id, digest),
                "Content-Length": str(len(body)),
                "Content-Type": "application/json",
            },
            content=body,
        )
        assert response.status_code == 422
        assert not (service.namespace_root / "stable" / release_id).exists()


@pytest.mark.anyio
async def test_metadata_kind_must_match_verified_sbom_contract(tmp_path: Path) -> None:
    built, public = _built_release(tmp_path)
    service, app, _audit, _environment = _service(tmp_path, public)
    release_id = built.manifest.release_id
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _upload_all(client, built)
        metadata_path = (
            service.namespace_root
            / "stable"
            / f".{release_id}.staging"
            / "release-metadata.json"
        )
        metadata = json.loads(metadata_path.read_bytes())
        metadata["artifacts"][0]["kind"] = "bootstrap"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        manifest_digest = hashlib.sha256(built.manifest_path.read_bytes()).hexdigest()
        body = json.dumps({"manifest_sha256": manifest_digest}).encode()
        response = await client.post(
            f"/api/v1/releases/{release_id}/replicas/cdn/finalize",
            headers={
                **_finalize_headers(release_id, manifest_digest),
                "Content-Length": str(len(body)),
                "Content-Type": "application/json",
            },
            content=body,
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "replica_release_metadata_invalid"
        assert not (service.namespace_root / "stable" / release_id).exists()


@pytest.mark.anyio
async def test_replica_route_auth_source_and_header_fences(tmp_path: Path) -> None:
    built, public = _built_release(tmp_path)
    _service_instance, app, _audit, _environment = _service(tmp_path, public)
    release_id = built.manifest.release_id
    payload = built.manifest_path.read_bytes()
    headers = _headers(release_id, "release-manifest.json", payload)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = dict(headers)
        del missing["Authorization"]
        response = await client.put(
            f"/api/v1/releases/{release_id}/replicas/cdn/assets/release-manifest.json",
            headers=missing,
            content=payload,
        )
        assert response.status_code == 401
        wrong_source = await client.put(
            f"/api/v1/releases/{release_id}/replicas/github/assets/release-manifest.json",
            headers=headers,
            content=payload,
        )
        assert wrong_source.status_code == 404
        wrong_idempotency = dict(headers)
        wrong_idempotency["Idempotency-Key"] = "unbound"
        response = await client.put(
            f"/api/v1/releases/{release_id}/replicas/cdn/assets/release-manifest.json",
            headers=wrong_idempotency,
            content=payload,
        )
        assert response.status_code == 422
        encoded = dict(headers)
        encoded["Content-Encoding"] = "gzip"
        response = await client.put(
            f"/api/v1/releases/{release_id}/replicas/cdn/assets/release-manifest.json",
            headers=encoded,
            content=payload,
        )
        assert response.status_code == 422


def test_production_nginx_and_systemd_keep_replica_boundary_narrow() -> None:
    routes = Path("deploy/ecorex-cloud-sidecar/nginx/ecorex-cloud.routes.conf").read_text(
        encoding="utf-8"
    )
    unit = Path(
        "deploy/ecorex-cloud-sidecar/systemd/ecorex-control-plane@.service"
    ).read_text(encoding="utf-8")
    public_environment = Path(
        "deploy/ecorex-cloud-sidecar/config/control-plane.env.example"
    ).read_text(encoding="utf-8")
    secret_environment = Path(
        "deploy/ecorex-cloud-sidecar/config/control-plane.secret.env.example"
    ).read_text(encoding="utf-8")
    deployer = Path("ecorex/deployment/cloud_sidecar.py").read_text(encoding="utf-8")
    assert "location /ecorex-agent/releases/" in routes
    assert 'location ~ "^/ecorex-agent/releases/(?<release_namespace>v' in routes
    assert "proxy_request_buffering off" in routes
    public_share_route = routes.split("location ^~ /s/ {", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert "limit_except GET HEAD { deny all; }" in public_share_route
    assert "proxy_pass $ecorex_control_plane;" in public_share_route
    legacy_gateway_route = routes.split(
        "location = /ecorex-agent/api/v1/responses {", 1
    )[1].split("}\n", 1)[0]
    assert "rewrite ^ /api/v1/model/stream break;" in legacy_gateway_route
    assert "proxy_pass $ecorex_gateway;" in legacy_gateway_route
    assert "proxy_request_buffering off;" in legacy_gateway_route
    assert "proxy_buffering off;" in legacy_gateway_route
    assert "X-Accel-Buffering no always;" in legacy_gateway_route
    usage_route = routes.split(
        "location = /api/v1/usage {", 1
    )[1].split("}\n", 1)[0]
    assert "proxy_pass $ecorex_gateway;" in usage_route
    assert "proxy_buffering off;" in usage_route
    assert routes.count("location ^~ /api/v1/bootstrap-index/ {") == 1
    bootstrap_route = routes.split(
        "location ^~ /api/v1/bootstrap-index/ {", 1
    )[1].split("}\n", 1)[0]
    assert "client_max_body_size 4m;" in bootstrap_route
    assert "proxy_pass $ecorex_control_plane;" in bootstrap_route
    assert "proxy_set_header Authorization $http_authorization;" in bootstrap_route
    assert "proxy_request_buffering off;" in bootstrap_route
    assert "proxy_buffering off;" in bootstrap_route
    assert "alias /srv/ecorex-agent-download" not in bootstrap_route
    assert "ReadWritePaths=/var/lib/ecorex/control-plane /var/lib/ecorex/cas /srv/ecorex-agent-download" in unit
    assert unit.count("/srv/ecorex-agent-download") == 1
    assert (
        "ECOREX_CP_RELEASE_REPLICA_STORAGE_ROOT="
        "/srv/ecorex-agent-download/v1-artifacts"
    ) in public_environment
    assert (
        f"ECOREX_CP_RELEASE_REPLICA_NAMESPACE={RELEASE_NAMESPACE}"
        in public_environment
    )
    assert (
        f"ECOREX_CP_RELEASE_REPLICA_PRODUCT_VERSION={PRODUCT_VERSION}"
        in public_environment
    )
    assert "ECOREX_CP_RELEASE_REPLICA_TOKEN_CURRENT=" in secret_environment
    assert "ECOREX_CP_RELEASE_REPLICA_TOKEN_NEXT=" in secret_environment
    assert 'shutil.chown(directory, user="ecorex-cloud", group="ecorex-storage")' in deployer
    assert "os.chmod(directory, 0o755)" in deployer


def test_replica_audit_sink_is_durable_redacted_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "audit.sqlite3"
    migrate_cloud_audit_database(database)
    repository = CloudAuditRepository(
        database, encryption_key=b"e" * 32, integrity_key=b"i" * 32
    )
    sink = CloudReleaseReplicaAuditSink(repository)
    payload = {
        "source_id": "cdn",
        "release_id": "release-stable-" + "a" * 24,
        "name": "core.zip",
        "size_bytes": 7,
        "sha256": "b" * 64,
        "state": "ready",
    }
    created_at = datetime(2026, 7, 16, tzinfo=UTC)
    for _ in range(2):
        sink.record(
            event_type="release.replica.asset.ready",
            source_event_id=(
                "replica:cdn:asset:release-stable-"
                + "a" * 24
                + ":core.zip:"
                + "b" * 64
            ),
            payload=payload,
            created_at=created_at,
        )
    records = repository.list_metadata(
        ControlPrincipal("auditor", "test", "auditor"),
        event_type="release.replica.asset.ready",
    )
    assert len(records) == 1
    assert records[0].account_id == "system-release-replica"


@pytest.mark.parametrize(
    "namespace,version",
    [
        (PRODUCT_VERSION, PRODUCT_VERSION),
        (f"v0{PRODUCT_VERSION}", f"0{PRODUCT_VERSION}"),
        ("v1.0.1", PRODUCT_VERSION),
        (RELEASE_NAMESPACE, "1.0.1"),
        (f"{RELEASE_NAMESPACE}/../v9.9.9", PRODUCT_VERSION),
    ],
)
def test_service_rejects_namespace_and_product_version_drift(
    tmp_path: Path, namespace: str, version: str
) -> None:
    root = tmp_path / "replica"
    root.mkdir()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    with pytest.raises(ValueError, match="configuration"):
        CDNReleaseReplicaService(
            storage_root=root.resolve(),
            public_root=PUBLIC_ROOT,
            release_namespace=namespace,
            product_version=version,
            verifier=Ed25519SignatureVerifier({"release-key": public}),
            token_verifier=EnvironmentRotatingReleaseReplicaTokenVerifier(
                {RELEASE_REPLICA_TOKEN_CURRENT_ENV: TOKEN_CURRENT}
            ),
        )


def test_cloud_deployer_prepares_only_configured_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "v1-artifacts"
    root.mkdir()
    config_root = tmp_path / "cloud"
    (config_root / "config").mkdir(parents=True)
    environment_path = config_root / "config" / "control-plane.env"
    environment_path.write_text(
        "\n".join(
            (
                "ECOREX_CP_RELEASE_REPLICA_ENABLED=true",
                f"ECOREX_CP_RELEASE_REPLICA_STORAGE_ROOT={root}",
                "ECOREX_CP_RELEASE_REPLICA_PUBLIC_ROOT="
                "https://dl.ecoremedia.net/ecorex-agent/releases",
                f"ECOREX_CP_RELEASE_REPLICA_NAMESPACE={RELEASE_NAMESPACE}",
                f"ECOREX_CP_RELEASE_REPLICA_PRODUCT_VERSION={PRODUCT_VERSION}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cloud_deployment, "CONFIG_ROOT", config_root)
    monkeypatch.setattr(cloud_deployment, "RELEASE_REPLICA_ROOT", root)
    monkeypatch.setattr(cloud_deployment.shutil, "chown", lambda *_args, **_kwargs: None)
    synced: list[Path] = []
    monkeypatch.setattr(cloud_deployment, "_fsync_directory", synced.append)
    cloud_deployment._prepare_release_replica_storage()
    assert (root / RELEASE_NAMESPACE / "stable").is_dir()
    assert (root / RELEASE_NAMESPACE / "canary").is_dir()
    assert synced == [root / RELEASE_NAMESPACE, root]
    assert not (root / "v9.9.9").exists()

    environment_path.write_text(
        environment_path.read_text(encoding="utf-8").replace(
            f"ECOREX_CP_RELEASE_REPLICA_NAMESPACE={RELEASE_NAMESPACE}",
            "ECOREX_CP_RELEASE_REPLICA_NAMESPACE=v9.9.9",
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        cloud_deployment.CloudDeployError,
        match="release_replica_configuration_invalid",
    ):
        cloud_deployment._prepare_release_replica_storage()


@pytest.mark.anyio
async def test_finalize_recovers_ready_before_visibility_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built, public = _built_release(tmp_path)
    service, app, _audit, _environment = _service(tmp_path, public)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert all(response.status_code == 201 for response in await _upload_all(client, built))
    release_id = built.manifest.release_id
    digest = hashlib.sha256(built.manifest_path.read_bytes()).hexdigest()
    original = service._finish_visibility_and_cleanup

    def crash_after_ready(_published, _stage, _verified):
        raise RuntimeError("simulated_visibility_crash")

    monkeypatch.setattr(service, "_finish_visibility_and_cleanup", crash_after_ready)
    with pytest.raises(RuntimeError, match="simulated_visibility_crash"):
        await service.finalize(release_id=release_id, manifest_sha256=digest)
    published = service.namespace_root / "stable" / release_id
    assert (published / ".ready.json").is_file()
    if os.name != "nt":
        assert stat.S_IMODE(published.stat().st_mode) & 0o077 == 0
    monkeypatch.setattr(service, "_finish_visibility_and_cleanup", original)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        retried = await _upload_all(client, built)
    assert {response.status_code for response in retried} == {200}
    receipt = await service.finalize(release_id=release_id, manifest_sha256=digest)
    assert receipt["state"] == "ready"
    assert stat.S_IMODE(published.stat().st_mode) & 0o055 == 0o055
    assert not (
        service.namespace_root / "stable" / f".{release_id}.staging"
    ).exists()


@pytest.mark.anyio
async def test_finalize_recovers_private_partial_no_clobber_directory(
    tmp_path: Path,
) -> None:
    built, public = _built_release(tmp_path)
    service, app, _audit, _environment = _service(tmp_path, public)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert all(response.status_code == 201 for response in await _upload_all(client, built))
    release_id = built.manifest.release_id
    stage = service.namespace_root / "stable" / f".{release_id}.staging"
    published = service.namespace_root / "stable" / release_id
    published.mkdir(mode=0o700)
    first = next(path for path in stage.iterdir() if not path.name.startswith("."))
    os.link(first, published / first.name, follow_symlinks=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        retried = await _upload_all(client, built)
    assert {response.status_code for response in retried} == {200}
    digest = hashlib.sha256(built.manifest_path.read_bytes()).hexdigest()
    receipt = await service.finalize(release_id=release_id, manifest_sha256=digest)
    assert receipt["state"] == "ready"
    assert (published / ".ready.json").is_file()
