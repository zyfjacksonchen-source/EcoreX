"""Fail-closed installation authority for the loopback provider TLS bridge."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import subprocess
import tempfile
from typing import Callable

try:
    import grp
except ImportError:  # pragma: no cover - Windows planner/test import
    grp = None  # type: ignore[assignment]

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import (
    ec,
    ed25519,
    ed448,
    padding,
    rsa,
)
from cryptography.x509.oid import ExtendedKeyUsageOID

from ecorex.deployment.provider_bridge import (
    BRIDGE_HOSTS,
    BRIDGE_ROUTES,
    CA_BUNDLE_PATH,
    ProviderBridgeConfigurationError,
    ProviderBridgeSpec,
    ProviderUpstream,
    SERVER_CERT_PATH,
    SERVER_KEY_PATH,
    render_nginx,
)


PROVIDER_BRIDGE_SPEC_PATH = Path("/var/lib/ecorex/config/provider-bridge-spec.json")
PROVIDER_BRIDGE_NGINX_PATH = Path(
    "/etc/nginx/conf.d/ecorex-provider-bridge.conf"
)
PROVIDER_BRIDGE_HOSTS_PATH = Path("/etc/hosts")
HOSTS_BEGIN = "# BEGIN ECOREX PROVIDER BRIDGE"
HOSTS_END = "# END ECOREX PROVIDER BRIDGE"
MINIMUM_CERTIFICATE_LIFETIME = timedelta(days=7)


class ProviderBridgeInstallError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ValidatedProviderBridge:
    spec: ProviderBridgeSpec
    nginx_payload: bytes
    ca_bundle_path: Path


def _read_regular(
    path: Path,
    *,
    expected_mode: int,
    expected_uid: int | None,
    expected_gid: int | None,
    maximum_size: int,
    enforce_identity: bool,
) -> bytes:
    descriptor = -1
    try:
        before = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(before.st_mode)
            or path.resolve(strict=True) != path
        ):
            raise OSError
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_size < 1
            or opened.st_size > maximum_size
        ):
            raise OSError
        if enforce_identity and os.name != "nt":
            if (
                stat.S_IMODE(opened.st_mode) != expected_mode
                or (expected_uid is not None and opened.st_uid != expected_uid)
                or (expected_gid is not None and opened.st_gid != expected_gid)
            ):
                raise OSError
        chunks: list[bytes] = []
        remaining = maximum_size + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after_fd = os.fstat(descriptor)
        after_path = path.lstat()
        if (
            path.is_symlink()
            or len(payload) != opened.st_size
            or len(payload) > maximum_size
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (after_fd.st_dev, after_fd.st_ino, after_fd.st_size)
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (after_path.st_dev, after_path.st_ino, after_path.st_size)
        ):
            raise OSError
        return payload
    except OSError:
        raise ProviderBridgeInstallError("provider_bridge_material_invalid") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _certificate_time(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _certificate_window(certificate: x509.Certificate, now: datetime) -> None:
    not_before = _certificate_time(
        certificate.not_valid_before_utc
        if hasattr(certificate, "not_valid_before_utc")
        else certificate.not_valid_before
    )
    not_after = _certificate_time(
        certificate.not_valid_after_utc
        if hasattr(certificate, "not_valid_after_utc")
        else certificate.not_valid_after
    )
    if not_before > now or not_after - now < MINIMUM_CERTIFICATE_LIFETIME:
        raise ProviderBridgeInstallError("provider_bridge_certificate_expiry_invalid")


def _verify_leaf_signature(
    certificate: x509.Certificate, authority: x509.Certificate
) -> None:
    public_key = authority.public_key()
    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                padding.PKCS1v15(),
                certificate.signature_hash_algorithm,
            )
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                ec.ECDSA(certificate.signature_hash_algorithm),
            )
        elif isinstance(public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            public_key.verify(
                certificate.signature, certificate.tbs_certificate_bytes
            )
        else:
            raise ValueError
    except Exception:
        raise ProviderBridgeInstallError("provider_bridge_certificate_chain_invalid") from None


def _validate_nginx_public_key(public_key: object) -> None:
    if isinstance(public_key, rsa.RSAPublicKey):
        if public_key.key_size < 2048:
            raise ProviderBridgeInstallError("provider_bridge_certificate_key_invalid")
        return
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        if public_key.key_size < 256:
            raise ProviderBridgeInstallError("provider_bridge_certificate_key_invalid")
        return
    raise ProviderBridgeInstallError("provider_bridge_certificate_key_invalid")


def _parse_spec(payload: bytes) -> tuple[ProviderBridgeSpec, dict[str, str]]:
    try:
        value = json.loads(payload.decode("utf-8"))
        required = {
            "schema_version",
            "upstreams",
            "public_ca_bundle_sha256",
            "server_certificate_sha256",
            "server_private_key_sha256",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ValueError
        if value["schema_version"] != 1 or not isinstance(value["upstreams"], dict):
            raise ValueError
        digests = {
            name: str(value[name])
            for name in required
            if name.endswith("_sha256")
        }
        if any(
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in digests.values()
        ):
            raise ValueError
        upstreams: dict[str, ProviderUpstream] = {}
        for preset, item in value["upstreams"].items():
            if not isinstance(item, dict) or set(item) != {
                "origin",
                "legacy_http_waiver",
            }:
                raise ValueError
            upstreams[str(preset)] = ProviderUpstream.from_origin(
                str(item["origin"]),
                legacy_http_waiver=item["legacy_http_waiver"],
            )
        return ProviderBridgeSpec(upstreams=upstreams), digests
    except (
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        ProviderBridgeConfigurationError,
    ):
        raise ProviderBridgeInstallError("provider_bridge_spec_invalid") from None


def validate_provider_bridge_materials(
    spec_path: Path = PROVIDER_BRIDGE_SPEC_PATH,
    *,
    ca_bundle_path: Path = CA_BUNDLE_PATH,
    server_certificate_path: Path = SERVER_CERT_PATH,
    server_private_key_path: Path = SERVER_KEY_PATH,
    enforce_identity: bool = True,
    now: datetime | None = None,
) -> ValidatedProviderBridge:
    """Validate every root-owned input before rendering or changing Nginx."""

    try:
        cloud_group = (
            None
            if not enforce_identity or os.name == "nt"
            else grp.getgrnam("ecorex-cloud").gr_gid
        )
        nginx_group = (
            None
            if not enforce_identity or os.name == "nt"
            else grp.getgrnam("nginx").gr_gid
        )
    except (KeyError, AttributeError):
        raise ProviderBridgeInstallError("provider_bridge_identity_invalid") from None
    spec_payload = _read_regular(
        spec_path,
        expected_mode=0o600,
        expected_uid=0,
        expected_gid=0,
        maximum_size=256 * 1024,
        enforce_identity=enforce_identity,
    )
    bridge_spec, digests = _parse_spec(spec_payload)
    ca_payload = _read_regular(
        ca_bundle_path,
        expected_mode=0o640,
        expected_uid=0,
        expected_gid=cloud_group,
        maximum_size=1024 * 1024,
        enforce_identity=enforce_identity,
    )
    certificate_payload = _read_regular(
        server_certificate_path,
        expected_mode=0o640,
        expected_uid=0,
        expected_gid=nginx_group,
        maximum_size=1024 * 1024,
        enforce_identity=enforce_identity,
    )
    private_key_payload = _read_regular(
        server_private_key_path,
        expected_mode=0o640,
        expected_uid=0,
        expected_gid=nginx_group,
        maximum_size=1024 * 1024,
        enforce_identity=enforce_identity,
    )
    expected = {
        "public_ca_bundle_sha256": ca_payload,
        "server_certificate_sha256": certificate_payload,
        "server_private_key_sha256": private_key_payload,
    }
    if any(
        hashlib.sha256(payload).hexdigest() != digests[name]
        for name, payload in expected.items()
    ):
        raise ProviderBridgeInstallError("provider_bridge_material_digest_mismatch")
    try:
        authority = x509.load_pem_x509_certificate(ca_payload)
        certificate = x509.load_pem_x509_certificate(certificate_payload)
        private_key = serialization.load_pem_private_key(
            private_key_payload, password=None
        )
        authority_constraints = authority.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
        leaf_constraints = certificate.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
        extended_key_usage = certificate.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage
        ).value
        names = set(
            certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value.get_values_for_type(x509.DNSName)
        )
    except Exception:
        raise ProviderBridgeInstallError("provider_bridge_certificate_invalid") from None
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    _certificate_window(authority, instant)
    _certificate_window(certificate, instant)
    if (
        not authority_constraints.ca
        or leaf_constraints.ca
        or ExtendedKeyUsageOID.SERVER_AUTH not in extended_key_usage
        or authority.subject != authority.issuer
        or certificate.issuer != authority.subject
    ):
        raise ProviderBridgeInstallError("provider_bridge_certificate_chain_invalid")
    if names != set(BRIDGE_HOSTS.values()):
        raise ProviderBridgeInstallError("provider_bridge_certificate_san_invalid")
    _validate_nginx_public_key(authority.public_key())
    _validate_nginx_public_key(certificate.public_key())
    _verify_leaf_signature(authority, authority)
    _verify_leaf_signature(certificate, authority)
    certificate_key = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if certificate_key != private_public_key:
        raise ProviderBridgeInstallError("provider_bridge_private_key_mismatch")
    return ValidatedProviderBridge(
        spec=bridge_spec,
        nginx_payload=render_nginx(bridge_spec).encode("utf-8"),
        ca_bundle_path=ca_bundle_path,
    )


def _managed_hosts_payload(current: bytes) -> bytes:
    try:
        text = current.decode("utf-8")
    except UnicodeDecodeError:
        raise ProviderBridgeInstallError("provider_bridge_hosts_invalid") from None
    lines = text.splitlines()
    begin = [index for index, line in enumerate(lines) if line == HOSTS_BEGIN]
    end = [index for index, line in enumerate(lines) if line == HOSTS_END]
    managed = "127.0.0.1 " + " ".join(BRIDGE_HOSTS.values())

    def contains_managed_host(line: str) -> bool:
        tokens = {token.casefold().rstrip(".") for token in line.split()}
        return any(host in tokens for host in BRIDGE_HOSTS.values())

    if begin or end:
        if len(begin) != 1 or len(end) != 1 or end[0] != begin[0] + 2:
            raise ProviderBridgeInstallError("provider_bridge_hosts_invalid")
        outside = lines[: begin[0]] + lines[end[0] + 1 :]
        if any(contains_managed_host(line) for line in outside):
            raise ProviderBridgeInstallError("provider_bridge_hosts_conflict")
        lines[begin[0] : end[0] + 1] = [HOSTS_BEGIN, managed, HOSTS_END]
    else:
        if any(contains_managed_host(line) for line in lines):
            raise ProviderBridgeInstallError("provider_bridge_hosts_conflict")
        if lines and lines[-1]:
            lines.append("")
        lines.extend((HOSTS_BEGIN, managed, HOSTS_END))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _replace(path: Path, payload: bytes, mode: int) -> None:
    if (
        path.is_symlink()
        or path.parent.is_symlink()
        or path.parent.resolve(strict=True) != path.parent
    ):
        raise ProviderBridgeInstallError("provider_bridge_install_target_invalid")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError
            written += count
        os.fsync(descriptor)
        if os.name != "nt":
            os.fchown(descriptor, 0, 0)
        os.close(descriptor)
        descriptor = -1
        if not hasattr(os, "fchmod"):
            os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_parent(path)
    except OSError:
        raise ProviderBridgeInstallError("provider_bridge_install_write_failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = -1
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path.parent, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError
        os.fsync(descriptor)
    except OSError:
        raise ProviderBridgeInstallError("provider_bridge_install_sync_failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _run(command: tuple[str, ...], code: str) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise ProviderBridgeInstallError(code) from None


def _http_status(
    context: ssl.SSLContext, host: str, method: str, path: str
) -> int:
    try:
        with socket.create_connection(("127.0.0.1", 443), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=host) as secured:
                secured.settimeout(5)
                secured.sendall(
                    f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode(
                        "ascii"
                    )
                )
                first_line = secured.recv(256).split(b"\r\n", 1)[0]
        parts = first_line.split()
        if len(parts) < 2:
            raise ValueError
        return int(parts[1])
    except (OSError, ssl.SSLError, ValueError):
        raise ProviderBridgeInstallError("provider_bridge_loopback_probe_failed") from None


def probe_provider_bridge(materials: ValidatedProviderBridge) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(materials.ca_bundle_path))
    for preset, host in BRIDGE_HOSTS.items():
        if _http_status(context, host, "GET", "/") != 404:
            raise ProviderBridgeInstallError("provider_bridge_loopback_probe_failed")
        for route in BRIDGE_ROUTES[preset]:
            status = _http_status(context, host, "OPTIONS", "/v1/" + route)
            if status not in {403, 405}:
                raise ProviderBridgeInstallError(
                    "provider_bridge_loopback_probe_failed"
                )


def install_provider_bridge(
    materials: ValidatedProviderBridge,
    *,
    nginx_path: Path = PROVIDER_BRIDGE_NGINX_PATH,
    hosts_path: Path = PROVIDER_BRIDGE_HOSTS_PATH,
    run_command: Callable[[tuple[str, ...], str], None] = _run,
    probe: Callable[[ValidatedProviderBridge], None] = probe_provider_bridge,
) -> None:
    """Install both managed files or restore their exact previous bytes."""

    try:
        if nginx_path.is_symlink() or hosts_path.is_symlink():
            raise OSError
        old_nginx = None if not nginx_path.exists() else nginx_path.read_bytes()
        old_nginx_mode = 0o600 if old_nginx is None else stat.S_IMODE(nginx_path.stat().st_mode)
        old_hosts = hosts_path.read_bytes()
        old_hosts_mode = stat.S_IMODE(hosts_path.stat().st_mode)
    except OSError:
        raise ProviderBridgeInstallError("provider_bridge_install_target_invalid") from None
    new_hosts = _managed_hosts_payload(old_hosts)
    changed = old_nginx != materials.nginx_payload or old_hosts != new_hosts
    try:
        if old_nginx != materials.nginx_payload:
            _replace(nginx_path, materials.nginx_payload, 0o600)
        if old_hosts != new_hosts:
            _replace(hosts_path, new_hosts, old_hosts_mode)
        run_command(("/usr/sbin/nginx", "-t"), "provider_bridge_nginx_invalid")
        if changed:
            run_command(
                ("/usr/bin/systemctl", "reload", "nginx.service"),
                "provider_bridge_nginx_reload_failed",
            )
        probe(materials)
    except (ProviderBridgeInstallError, OSError) as error:
        try:
            if old_nginx is None:
                nginx_path.unlink(missing_ok=True)
                _fsync_parent(nginx_path)
            else:
                _replace(nginx_path, old_nginx, old_nginx_mode)
            _replace(hosts_path, old_hosts, old_hosts_mode)
            run_command(("/usr/sbin/nginx", "-t"), "provider_bridge_restore_failed")
            if changed:
                run_command(
                    ("/usr/bin/systemctl", "reload", "nginx.service"),
                    "provider_bridge_restore_failed",
                )
        except (ProviderBridgeInstallError, OSError):
            raise ProviderBridgeInstallError("provider_bridge_restore_failed") from None
        if isinstance(error, ProviderBridgeInstallError):
            raise error
        raise ProviderBridgeInstallError("provider_bridge_install_failed") from None


__all__ = [
    "PROVIDER_BRIDGE_SPEC_PATH",
    "ProviderBridgeInstallError",
    "ValidatedProviderBridge",
    "install_provider_bridge",
    "probe_provider_bridge",
    "validate_provider_bridge_materials",
]
