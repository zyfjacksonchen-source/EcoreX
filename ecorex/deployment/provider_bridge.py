"""Typed renderer for the loopback-only managed-provider TLS bridge.

The renderer has no install/reload side effects.  It produces one bounded
Nginx fragment and one ``/etc/hosts`` line that an operator can validate in an
isolated prefix before deliberately installing them.  Provider request paths
are product-owned; an administrator can rotate model credentials, but cannot
turn the bridge into an open forward proxy.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit


BRIDGE_HOSTS: Mapping[str, str] = MappingProxyType(
    {
        "ecorex_chat": "main-provider.ecorex.internal",
        "deepseek_chat": "deepseek-provider.ecorex.internal",
        "gemini_chat": "gemini-provider.ecorex.internal",
        "doubao_chat": "doubao-provider.ecorex.internal",
        "ecorex_image": "image-provider.ecorex.internal",
    }
)
BRIDGE_ROUTES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "ecorex_chat": ("models", "responses"),
        "deepseek_chat": ("models", "chat/completions"),
        "gemini_chat": ("models", "chat/completions"),
        "doubao_chat": ("models", "chat/completions"),
        "ecorex_image": ("models", "images/generations", "images/edits"),
    }
)
TLS_ROOT = Path("/var/lib/ecorex/provider-bridge/tls")
CA_BUNDLE_PATH = Path("/var/lib/ecorex/config/provider-bridge-ca.pem")
SERVER_CERT_PATH = TLS_ROOT / "provider-bridge.crt"
SERVER_KEY_PATH = TLS_ROOT / "provider-bridge.key"
LEGACY_HTTP_WAIVER = "ecorex-v1-legacy-provider-http-upstream-waiver"

_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$|^[a-z0-9]$")
_BASE_PATH = re.compile(r"^/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*)?$")
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_IPV6_ULA = ipaddress.ip_network("fc00::/7")


class ProviderBridgeConfigurationError(RuntimeError):
    pass


def _is_safe_plaintext_endpoint(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in _RFC1918)
    return address in _IPV6_ULA


@dataclass(frozen=True, slots=True)
class ProviderUpstream:
    scheme: str
    host: str
    port: int
    base_path: str
    legacy_http_waiver: str | None = None

    def __post_init__(self) -> None:
        host = self.host.casefold().rstrip(".")
        try:
            parsed_ip = ipaddress.ip_address(host)
        except ValueError:
            parsed_ip = None
        if (
            self.scheme not in {"http", "https"}
            or (parsed_ip is None and _HOST.fullmatch(host) is None)
            or host in {"localhost", "localhost.localdomain"}
            or not 1 <= self.port <= 65535
            or _BASE_PATH.fullmatch(self.base_path) is None
            or (self.base_path != "/" and self.base_path.endswith("/"))
            or (
                self.scheme == "http"
                and (
                    self.legacy_http_waiver != LEGACY_HTTP_WAIVER
                    or parsed_ip is None
                    or not _is_safe_plaintext_endpoint(parsed_ip)
                )
            )
            or (
                self.scheme == "https"
                and (
                    self.legacy_http_waiver is not None
                    or (
                        parsed_ip is not None
                        and (parsed_ip.is_loopback or parsed_ip.is_unspecified)
                    )
                )
            )
        ):
            raise ProviderBridgeConfigurationError(
                "provider bridge upstream is invalid"
            )
        object.__setattr__(self, "host", host)

    @classmethod
    def from_origin(
        cls, origin: str, *, legacy_http_waiver: str | None = None
    ) -> "ProviderUpstream":
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderBridgeConfigurationError(
                "provider bridge upstream origin is invalid"
            )
        return cls(
            scheme=parsed.scheme,
            host=parsed.hostname,
            port=parsed.port or (443 if parsed.scheme == "https" else 80),
            base_path=(parsed.path.rstrip("/") or "/"),
            legacy_http_waiver=legacy_http_waiver,
        )


@dataclass(frozen=True, slots=True)
class ProviderBridgeSpec:
    upstreams: Mapping[str, ProviderUpstream]
    server_certificate_path: Path = SERVER_CERT_PATH
    server_private_key_path: Path = SERVER_KEY_PATH
    public_ca_bundle_path: Path = CA_BUNDLE_PATH

    def __post_init__(self) -> None:
        upstreams = dict(self.upstreams)
        if (
            set(upstreams) != set(BRIDGE_HOSTS)
            or any(not isinstance(value, ProviderUpstream) for value in upstreams.values())
            or self.server_certificate_path != SERVER_CERT_PATH
            or self.server_private_key_path != SERVER_KEY_PATH
            or self.public_ca_bundle_path != CA_BUNDLE_PATH
        ):
            raise ProviderBridgeConfigurationError(
                "provider bridge specification is invalid"
            )
        object.__setattr__(self, "upstreams", MappingProxyType(upstreams))

    @property
    def managed_origins(self) -> Mapping[str, str]:
        return MappingProxyType(
            {preset: "https://" + host for preset, host in BRIDGE_HOSTS.items()}
        )


def render_hosts_fragment() -> str:
    return "127.0.0.1 " + " ".join(BRIDGE_HOSTS.values()) + "\n"


def render_nginx(spec: ProviderBridgeSpec) -> str:
    blocks = [
        "# Generated by ecorex.deployment.provider_bridge; do not edit.\n",
    ]
    for preset in BRIDGE_HOSTS:
        upstream = spec.upstreams[preset]
        route_blocks = "".join(
            _route(upstream, route) for route in BRIDGE_ROUTES[preset]
        )
        blocks.append(
            "server {\n"
            "    listen 127.0.0.1:443 ssl;\n"
            f"    server_name {BRIDGE_HOSTS[preset]};\n"
            f"    ssl_certificate {spec.server_certificate_path};\n"
            f"    ssl_certificate_key {spec.server_private_key_path};\n"
            "    ssl_protocols TLSv1.2 TLSv1.3;\n"
            "    ssl_session_tickets off;\n"
            "    allow 127.0.0.1;\n"
            "    allow ::1;\n"
            "    deny all;\n"
            "    access_log off;\n"
            "    client_max_body_size 72m;\n"
            "    location / { return 404; }\n"
            f"{route_blocks}"
            "}\n"
        )
    return "".join(blocks)


def _route(upstream: ProviderUpstream, route: str) -> str:
    base = "" if upstream.base_path == "/" else upstream.base_path
    rendered_host = f"[{upstream.host}]" if ":" in upstream.host else upstream.host
    target = f"{upstream.scheme}://{rendered_host}:{upstream.port}{base}/{route}"
    tls = ""
    if upstream.scheme == "https":
        tls = (
            "        proxy_ssl_server_name on;\n"
            f"        proxy_ssl_name {upstream.host};\n"
            "        proxy_ssl_verify on;\n"
            "        proxy_ssl_verify_depth 4;\n"
            "        proxy_ssl_trusted_certificate /etc/pki/tls/certs/ca-bundle.crt;\n"
        )
    method = "GET" if route == "models" else "POST"
    return (
        f"    location = /v1/{route} {{\n"
        f"        limit_except {method} {{ deny all; }}\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Connection \"\";\n"
        f"        proxy_set_header Host {rendered_host};\n"
        "        proxy_pass_request_headers on;\n"
        "        proxy_pass_request_body on;\n"
        "        proxy_redirect off;\n"
        "        proxy_intercept_errors off;\n"
        "        proxy_buffering off;\n"
        "        proxy_request_buffering off;\n"
        "        proxy_connect_timeout 15s;\n"
        "        proxy_send_timeout 900s;\n"
        "        proxy_read_timeout 900s;\n"
        "        proxy_max_temp_file_size 0;\n"
        f"{tls}"
        f"        proxy_pass {target};\n"
        "    }\n"
    )


__all__ = [
    "BRIDGE_HOSTS",
    "BRIDGE_ROUTES",
    "CA_BUNDLE_PATH",
    "LEGACY_HTTP_WAIVER",
    "ProviderBridgeConfigurationError",
    "ProviderBridgeSpec",
    "ProviderUpstream",
    "SERVER_CERT_PATH",
    "SERVER_KEY_PATH",
    "render_hosts_fragment",
    "render_nginx",
]
