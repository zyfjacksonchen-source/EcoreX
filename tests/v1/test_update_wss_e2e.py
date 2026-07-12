from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import ipaddress
import socket
import ssl
import threading
import time
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import uvicorn
import pytest

from ecorex.control_plane import (
    REQUIRED_RELEASE_GATES,
    ControlPlaneRepository,
    ControlPrincipal,
    create_control_plane_app,
    migrate_control_plane_database,
)
from ecorex.update import (
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
    WebSocketUpdateSignalSource,
)


ADMIN_TOKEN = "admin-token-12345678901234567890"
CLIENT_TOKEN = "client-token-1234567890123456789"


class AcceptingVerifier:
    def verify(self, payload, signature) -> bool:
        assert payload and signature.key_id == "release-key"
        return True


@pytest.fixture(autouse=True)
def _isolate_bootstrap_publication_proof(monkeypatch) -> None:
    """The WSS transport test consumes an already trusted stable rollout."""

    monkeypatch.setattr(
        ControlPlaneRepository,
        "_require_bootstrap_index_proof",
        lambda *_args, **_kwargs: None,
    )


class Credentials:
    def bearer_token(self) -> str:
        return CLIENT_TOKEN


class Authenticator:
    def __init__(self) -> None:
        self.client_authenticated = threading.Event()
        self.admin = ControlPrincipal(
            subject="admin",
            client_id="admin-client",
            account_id="admin-account",
            organization_id="ops",
            roles=frozenset({"release_admin"}),
        )
        self.client = ControlPrincipal(
            subject="client",
            client_id="client-1",
            account_id="account-1",
            organization_id="org-1",
        )

    def authenticate(self, bearer_token: str) -> ControlPrincipal:
        if bearer_token == ADMIN_TOKEN:
            return self.admin
        if bearer_token == CLIENT_TOKEN:
            self.client_authenticated.set()
            return self.client
        raise PermissionError("invalid token")


def _signature() -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="release-key",
        value=base64.b64encode(b"test-signature").decode(),
    )


def _manifest() -> ReleaseManifest:
    payload = b"signed core package"
    return ReleaseManifest(
        schema_version=1,
        release_id="release-1.0.1-stable",
        version="1.0.1",
        build_digest=hashlib.sha256(b"build-1.0.1").hexdigest(),
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T12:00:00+08:00",
        sources=(
            ReleaseSource(
                "mirror", SourceKind.GITHUB_CN_MIRROR, 0, "https://mirror.example/v1"
            ),
            ReleaseSource(
                "github", SourceKind.GITHUB_RELEASE, 1, "https://github.example/v1"
            ),
            ReleaseSource("cdn", SourceKind.ECOREX_CDN, 2, "https://cdn.example/v1"),
        ),
        artifacts=(
            ReleaseArtifact(
                artifact_id="core-windows-x64",
                platform="windows",
                architecture="x64",
                file_name="ecorex-core.zip",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                signature=_signature(),
            ),
        ),
        signature=_signature(),
    )


def _prepare_rollout(
    repository: ControlPlaneRepository,
    authenticator: Authenticator,
):
    manifest = _manifest()
    repository.create_candidate(
        manifest,
        actor=authenticator.admin,
        client_request_id="candidate-wss",
    )
    for gate in sorted(REQUIRED_RELEASE_GATES):
        repository.record_gate(
            manifest.release_id,
            gate,
            status="passed",
            evidence=f"ci://wss/{gate}",
            actor=authenticator.admin,
            client_request_id=f"wss-gate-{gate}",
        )
    repository.publish(
        manifest.release_id,
        actor=authenticator.admin,
        client_request_id="publish-wss",
    )
    rollout = repository.create_rollout(
        manifest.release_id,
        percentage=100,
        organizations=["org-1"],
        accounts=[],
        minimum_compatible_version=None,
        actor=authenticator.admin,
        client_request_id="rollout-wss",
    )
    return manifest, rollout


def _tls_files(tmp_path: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]
    )
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "control-plane-cert.pem"
    key_path = tmp_path / "control-plane-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@contextmanager
def _real_tls_server(app, cert_path: Path, key_path: Path):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
        log_level="critical",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="ecorex-control-plane-e2e",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise RuntimeError("real Control Plane server did not start")
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()


def test_real_wss_update_available_round_trip_with_tls_and_authorization(
    tmp_path: Path,
) -> None:
    authenticator = Authenticator()
    migrate_control_plane_database(tmp_path / "control.db")
    repository = ControlPlaneRepository(
        tmp_path / "control.db", verifier=AcceptingVerifier()
    )
    manifest, rollout = _prepare_rollout(repository, authenticator)
    app = create_control_plane_app(repository, authenticator=authenticator)
    cert_path, key_path = _tls_files(tmp_path)
    context = ssl.create_default_context(cafile=str(cert_path))

    with _real_tls_server(app, cert_path, key_path) as port:
        source = WebSocketUpdateSignalSource(
            f"wss://localhost:{port}/api/v1/client/updates/ws",
            credentials=Credentials(),
            allowed_hosts=frozenset({"localhost"}),
            channel=ReleaseChannel.STABLE,
            platform="windows",
            architecture="x64",
            current_version="1.0.0",
            ssl_context=context,
        )

        async def receive_signal():
            events = source.events()
            pending = asyncio.create_task(anext(events))
            assert await asyncio.to_thread(authenticator.client_authenticated.wait, 5)
            deadline = asyncio.get_running_loop().time() + 5
            while (
                authenticator.client.client_id
                not in app.state.update_signal_hub._connections
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.01)

            def activate():
                with httpx.Client(verify=context, trust_env=False, timeout=5) as client:
                    return client.post(
                        "https://localhost:"
                        f"{port}/api/v1/admin/rollouts/{rollout.rollout_id}/activate",
                        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                        json={"client_request_id": "activate-real-wss"},
                    )

            response = await asyncio.to_thread(activate)
            assert response.status_code == 200, response.text
            signal = await asyncio.wait_for(pending, timeout=5)
            await events.aclose()
            await source.close()
            return signal

        signal = asyncio.run(receive_signal())

    assert signal.release_id == manifest.release_id
    assert signal.version == manifest.version
    assert signal.build_digest == manifest.build_digest
    assert signal.channel is ReleaseChannel.STABLE
    assert source.url.endswith(
        "channel=stable&platform=windows&architecture=x64&current_version=1.0.0"
    )
