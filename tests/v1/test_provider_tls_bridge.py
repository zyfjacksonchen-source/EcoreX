from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import inspect
import json
import os
from pathlib import Path
import ssl
import stat

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from cryptography.x509.oid import ExtendedKeyUsageOID
import pytest

from ecorex.deployment.provider_bridge import (
    BRIDGE_HOSTS,
    LEGACY_HTTP_WAIVER,
    ProviderBridgeConfigurationError,
    ProviderBridgeSpec,
    ProviderUpstream,
    render_hosts_fragment,
    render_nginx,
)
from ecorex.deployment.provider_bridge_install import (
    HOSTS_BEGIN,
    ProviderBridgeInstallError,
    install_provider_bridge,
    validate_provider_bridge_materials,
)
from ecorex.deployment import provider_bridge_install as bridge_install
from ecorex.gateway.chat_completions_provider import (
    ManagedHTTPSChatCompletionsProvider,
)
from ecorex.gateway.responses_provider import ManagedHTTPSResponsesProvider
from ecorex.image_orchestrator.managed_provider import ManagedHTTPSImageProvider
from ecorex.image_orchestrator.openai_provider import OpenAICompatibleImageProvider
from ecorex.security.provider_tls import (
    ProviderTLSConfigurationError,
    pinned_provider_ssl_context,
    requires_private_provider_ca,
    validate_provider_ca_binding,
)


def _ca(path: Path) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EcoreX test CA")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    payload = certificate.public_bytes(serialization.Encoding.PEM)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _spec() -> ProviderBridgeSpec:
    return ProviderBridgeSpec(
        upstreams={
            "ecorex_chat": ProviderUpstream.from_origin(
                "http://10.64.1.20/v1",
                legacy_http_waiver=LEGACY_HTTP_WAIVER,
            ),
            "deepseek_chat": ProviderUpstream.from_origin(
                "https://api.deepseek.com/v1"
            ),
            "gemini_chat": ProviderUpstream.from_origin(
                "http://[fd64::20]:8080/v1",
                legacy_http_waiver=LEGACY_HTTP_WAIVER,
            ),
            "doubao_chat": ProviderUpstream.from_origin(
                "https://ark.cn-beijing.volces.com/api/v3"
            ),
            "ecorex_image": ProviderUpstream.from_origin(
                "http://10.64.1.20/v1",
                legacy_http_waiver=LEGACY_HTTP_WAIVER,
            ),
        }
    )


def _deployment_materials(tmp_path: Path):
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "EcoreX provider test CA")]
    )
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, next(iter(BRIDGE_HOSTS.values())))]
    )
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(host) for host in BRIDGE_HOSTS.values()]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = tmp_path / "provider-ca.pem"
    certificate_path = tmp_path / "provider.crt"
    private_key_path = tmp_path / "provider.key"
    ca_path.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM))
    certificate_path.write_bytes(
        server_certificate.public_bytes(serialization.Encoding.PEM)
    )
    private_key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    spec_path = tmp_path / "provider-bridge-spec.json"
    upstreams = {
        preset: {
            "origin": (
                f"{upstream.scheme}://"
                + (f"[{upstream.host}]" if ":" in upstream.host else upstream.host)
                + f":{upstream.port}{upstream.base_path}"
            ),
            "legacy_http_waiver": upstream.legacy_http_waiver,
        }
        for preset, upstream in _spec().upstreams.items()
    }
    spec_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "upstreams": upstreams,
                "public_ca_bundle_sha256": hashlib.sha256(
                    ca_path.read_bytes()
                ).hexdigest(),
                "server_certificate_sha256": hashlib.sha256(
                    certificate_path.read_bytes()
                ).hexdigest(),
                "server_private_key_sha256": hashlib.sha256(
                    private_key_path.read_bytes()
                ).hexdigest(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return (
        spec_path,
        ca_path,
        certificate_path,
        private_key_path,
    )


def _emulate_root_file_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[int, int]]:
    """Keep production fchown strict while making non-root POSIX CI explicit."""

    calls: list[tuple[int, int]] = []
    if not hasattr(bridge_install.os, "fchown"):
        return calls

    def fchown(_descriptor: int, uid: int, gid: int) -> None:
        calls.append((uid, gid))

    monkeypatch.setattr(bridge_install.os, "fchown", fchown)
    return calls


def test_private_provider_ca_is_paired_digest_pinned_and_strict(tmp_path: Path) -> None:
    origin = "https://main-provider.ecorex.internal"
    assert requires_private_provider_ca([origin]) is True
    with pytest.raises(ProviderTLSConfigurationError, match="pinned CA"):
        validate_provider_ca_binding(
            [origin], ca_bundle_path=None, ca_bundle_sha256=None
        )

    ca = tmp_path / "provider-ca.pem"
    digest = _ca(ca)
    validate_provider_ca_binding(
        [origin], ca_bundle_path=ca, ca_bundle_sha256=digest
    )
    context = pinned_provider_ssl_context(ca, digest)
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2

    with pytest.raises(ProviderTLSConfigurationError, match="digest"):
        pinned_provider_ssl_context(ca, "0" * 64)
    ca.write_text("not a certificate", encoding="ascii")
    changed = hashlib.sha256(ca.read_bytes()).hexdigest()
    with pytest.raises(ProviderTLSConfigurationError, match="invalid"):
        pinned_provider_ssl_context(ca, changed)


def test_public_provider_uses_system_trust_unless_a_pair_is_explicit() -> None:
    origin = "https://api.deepseek.com"
    assert requires_private_provider_ca([origin]) is False
    validate_provider_ca_binding(
        [origin], ca_bundle_path=None, ca_bundle_sha256=None
    )
    assert pinned_provider_ssl_context(None, None) is None
    with pytest.raises(ProviderTLSConfigurationError, match="together"):
        pinned_provider_ssl_context(Path("/missing.pem"), None)


def test_private_ca_cannot_expand_a_mixed_public_private_origin_set(
    tmp_path: Path,
) -> None:
    ca = tmp_path / "provider-ca.pem"
    digest = _ca(ca)
    with pytest.raises(ProviderTLSConfigurationError, match="all-private"):
        validate_provider_ca_binding(
            [
                "https://main-provider.ecorex.internal",
                "https://api.deepseek.com",
            ],
            ca_bundle_path=ca,
            ca_bundle_sha256=digest,
        )


def test_bridge_renderer_has_five_sni_hosts_exact_routes_and_no_open_proxy() -> None:
    rendered = render_nginx(_spec())
    assert rendered.count("server {\n") == 5
    assert rendered.count("listen 127.0.0.1:443 ssl;") == 5
    assert "listen 443 ssl;" not in rendered
    assert rendered.count("allow 127.0.0.1;") == 5
    assert rendered.count("deny all;") >= 5
    assert "location / { return 404; }" in rendered
    assert "$request_uri" not in rendered
    assert "proxy_redirect off;" in rendered
    assert "proxy_buffering off;" in rendered
    assert (
        "proxy_pass https://ark.cn-beijing.volces.com:443/api/v3/chat/completions;"
        in rendered
    )
    assert (
        "proxy_pass http://10.64.1.20:80/v1/responses;"
        in rendered
    )
    assert "proxy_pass http://[fd64::20]:8080/v1/chat/completions;" in rendered
    for host in BRIDGE_HOSTS.values():
        assert f"server_name {host};" in rendered
    hosts = render_hosts_fragment()
    assert hosts.startswith("127.0.0.1 ")
    assert set(hosts.split()[1:]) == set(BRIDGE_HOSTS.values())


def test_bridge_rejects_unwaived_hostname_and_public_http_upstreams() -> None:
    with pytest.raises(ProviderBridgeConfigurationError):
        ProviderUpstream.from_origin("http://provider.test/v1")
    with pytest.raises(ProviderBridgeConfigurationError):
        ProviderUpstream.from_origin(
            "http://provider.test/v1", legacy_http_waiver=LEGACY_HTTP_WAIVER
        )
    with pytest.raises(ProviderBridgeConfigurationError):
        ProviderUpstream.from_origin(
            "http://8.8.8.8/v1", legacy_http_waiver=LEGACY_HTTP_WAIVER
        )


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:9000/v1",
        "http://10.8.0.2/v1",
        "http://172.20.0.2/v1",
        "http://192.168.50.2/v1",
        "http://[::1]:9000/v1",
        "http://[fd42::2]/v1",
    ],
)
def test_bridge_allows_only_explicitly_waived_loopback_or_private_http(
    origin: str,
) -> None:
    upstream = ProviderUpstream.from_origin(
        origin, legacy_http_waiver=LEGACY_HTTP_WAIVER
    )
    assert upstream.scheme == "http"


def test_bridge_rejects_unknown_presets() -> None:
    spec = _spec()
    with pytest.raises(ProviderBridgeConfigurationError):
        ProviderBridgeSpec(
            upstreams={
                **dict(spec.upstreams),
                "unknown": spec.upstreams["ecorex_chat"],
            }
        )


@pytest.mark.parametrize(
    "provider",
    [
        ManagedHTTPSResponsesProvider,
        ManagedHTTPSChatCompletionsProvider,
        OpenAICompatibleImageProvider,
        ManagedHTTPSImageProvider,
    ],
)
def test_every_managed_http_provider_accepts_only_an_explicit_ssl_context(
    provider,
) -> None:
    parameter = inspect.signature(provider).parameters["ssl_context"]
    assert parameter.default is None


def test_bridge_deployment_validates_digest_chain_san_key_and_expiry(
    tmp_path: Path,
) -> None:
    spec_path, ca_path, certificate_path, private_key_path = _deployment_materials(
        tmp_path
    )
    materials = validate_provider_bridge_materials(
        spec_path,
        ca_bundle_path=ca_path,
        server_certificate_path=certificate_path,
        server_private_key_path=private_key_path,
        enforce_identity=False,
    )
    assert materials.nginx_payload.count(b"listen 127.0.0.1:443 ssl;") == 5

    private_key_path.write_bytes(private_key_path.read_bytes() + b"\n")
    with pytest.raises(
        ProviderBridgeInstallError, match="provider_bridge_material_digest_mismatch"
    ):
        validate_provider_bridge_materials(
            spec_path,
            ca_bundle_path=ca_path,
            server_certificate_path=certificate_path,
            server_private_key_path=private_key_path,
            enforce_identity=False,
        )


def test_bridge_deployment_rejects_public_http_before_install(tmp_path: Path) -> None:
    spec_path, ca_path, certificate_path, private_key_path = _deployment_materials(
        tmp_path
    )
    value = json.loads(spec_path.read_text(encoding="utf-8"))
    value["upstreams"]["ecorex_chat"] = {
        "origin": "http://8.8.8.8/v1",
        "legacy_http_waiver": LEGACY_HTTP_WAIVER,
    }
    spec_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProviderBridgeInstallError, match="provider_bridge_spec_invalid"):
        validate_provider_bridge_materials(
            spec_path,
            ca_bundle_path=ca_path,
            server_certificate_path=certificate_path,
            server_private_key_path=private_key_path,
            enforce_identity=False,
        )


def test_bridge_install_is_idempotent_and_manages_one_tagged_hosts_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path, ca_path, certificate_path, private_key_path = _deployment_materials(
        tmp_path
    )
    materials = validate_provider_bridge_materials(
        spec_path,
        ca_bundle_path=ca_path,
        server_certificate_path=certificate_path,
        server_private_key_path=private_key_path,
        enforce_identity=False,
    )
    nginx_path = tmp_path / "provider.conf"
    hosts_path = tmp_path / "hosts"
    hosts_path.write_text("127.0.0.1 localhost\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []
    probes: list[object] = []
    ownership_calls = _emulate_root_file_ownership(monkeypatch)

    def run(command: tuple[str, ...], _code: str) -> None:
        commands.append(command)

    install_provider_bridge(
        materials,
        nginx_path=nginx_path,
        hosts_path=hosts_path,
        run_command=run,
        probe=lambda value: probes.append(value),
    )
    install_provider_bridge(
        materials,
        nginx_path=nginx_path,
        hosts_path=hosts_path,
        run_command=run,
        probe=lambda value: probes.append(value),
    )

    assert nginx_path.read_bytes() == materials.nginx_payload
    assert hosts_path.read_text(encoding="utf-8").count(HOSTS_BEGIN) == 1
    assert commands.count(("/usr/sbin/nginx", "-t")) == 2
    assert commands.count(("/usr/bin/systemctl", "reload", "nginx.service")) == 1
    assert len(probes) == 2
    if os.name != "nt":
        assert ownership_calls == [(0, 0), (0, 0)]
        assert stat.S_IMODE(nginx_path.stat().st_mode) == 0o600


def test_bridge_install_retries_only_the_bounded_post_reload_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path, ca_path, certificate_path, private_key_path = _deployment_materials(
        tmp_path
    )
    materials = validate_provider_bridge_materials(
        spec_path,
        ca_bundle_path=ca_path,
        server_certificate_path=certificate_path,
        server_private_key_path=private_key_path,
        enforce_identity=False,
    )
    nginx_path = tmp_path / "provider.conf"
    hosts_path = tmp_path / "hosts"
    hosts_path.write_text("127.0.0.1 localhost\n", encoding="utf-8")
    _emulate_root_file_ownership(monkeypatch)
    attempts = 0
    sleeps: list[float] = []
    now = [0.0]

    def probe(_value: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ProviderBridgeInstallError("provider_bridge_loopback_probe_failed")

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    install_provider_bridge(
        materials,
        nginx_path=nginx_path,
        hosts_path=hosts_path,
        run_command=lambda _command, _code: None,
        probe=probe,
        sleeper=sleep,
        clock=lambda: now[0],
    )

    assert attempts == 3
    assert sleeps == [0.25, 0.25]


def test_bridge_install_failure_restores_exact_previous_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_path, ca_path, certificate_path, private_key_path = _deployment_materials(
        tmp_path
    )
    materials = validate_provider_bridge_materials(
        spec_path,
        ca_bundle_path=ca_path,
        server_certificate_path=certificate_path,
        server_private_key_path=private_key_path,
        enforce_identity=False,
    )
    nginx_path = tmp_path / "provider.conf"
    hosts_path = tmp_path / "hosts"
    nginx_path.write_bytes(b"previous nginx\n")
    hosts_path.write_bytes(b"127.0.0.1 localhost\n")
    ownership_calls = _emulate_root_file_ownership(monkeypatch)
    failed = False

    def run(_command: tuple[str, ...], code: str) -> None:
        nonlocal failed
        if code == "provider_bridge_nginx_reload_failed" and not failed:
            failed = True
            raise ProviderBridgeInstallError(code)

    with pytest.raises(
        ProviderBridgeInstallError, match="provider_bridge_nginx_reload_failed"
    ):
        install_provider_bridge(
            materials,
            nginx_path=nginx_path,
            hosts_path=hosts_path,
            run_command=run,
            probe=lambda _value: None,
        )
    assert nginx_path.read_bytes() == b"previous nginx\n"
    assert hosts_path.read_bytes() == b"127.0.0.1 localhost\n"
    if os.name != "nt":
        assert ownership_calls == [(0, 0)] * 4


def test_managed_hosts_rejects_duplicate_sni_outside_owned_block() -> None:
    host = next(iter(BRIDGE_HOSTS.values()))
    payload = (
        f"127.0.0.1 localhost\n127.0.0.2 {host.upper()}.\n"
        f"{HOSTS_BEGIN}\n127.0.0.1 {' '.join(BRIDGE_HOSTS.values())}\n"
        f"{bridge_install.HOSTS_END}\n"
    ).encode("utf-8")
    with pytest.raises(ProviderBridgeInstallError, match="provider_bridge_hosts_conflict"):
        bridge_install._managed_hosts_payload(payload)


def test_atomic_replace_handles_short_writes_and_syncs_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "atomic.conf"
    real_write = bridge_install.os.write
    ownership_calls = _emulate_root_file_ownership(monkeypatch)
    writes = 0

    def short_write(descriptor: int, payload) -> int:
        nonlocal writes
        writes += 1
        chunk = bytes(payload)
        return real_write(descriptor, chunk[: max(1, len(chunk) // 2)])

    monkeypatch.setattr(bridge_install.os, "write", short_write)
    bridge_install._replace(target, b"a" * 4097, 0o600)
    assert target.read_bytes() == b"a" * 4097
    assert writes > 1
    if os.name != "nt":
        assert ownership_calls == [(0, 0)]
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
