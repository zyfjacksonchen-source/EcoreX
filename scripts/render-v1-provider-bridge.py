#!/usr/bin/env python3
"""Render side-effect-free Nginx/hosts inputs for the v1 provider bridge."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from ecorex.deployment.provider_bridge import (
    ProviderBridgeConfigurationError,
    ProviderBridgeSpec,
    ProviderUpstream,
    render_hosts_fragment,
    render_nginx,
)


def _load(path: Path) -> ProviderBridgeSpec:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
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
        if any(
            not isinstance(value[name], str)
            or len(value[name]) != 64
            or any(character not in "0123456789abcdef" for character in value[name])
            for name in required
            if name.endswith("_sha256")
        ):
            raise ValueError
        upstreams = {}
        for preset, item in value["upstreams"].items():
            if not isinstance(preset, str) or not isinstance(item, dict) or set(item) != {
                "origin",
                "legacy_http_waiver",
            }:
                raise ValueError
            upstreams[preset] = ProviderUpstream.from_origin(
                item["origin"], legacy_http_waiver=item["legacy_http_waiver"]
            )
        return ProviderBridgeSpec(upstreams=upstreams)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ProviderBridgeConfigurationError(
            "provider bridge specification is invalid"
        ) from None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--nginx-output", type=Path)
    parser.add_argument("--hosts-output", type=Path)
    args = parser.parse_args()
    try:
        spec = _load(args.spec)
        nginx = render_nginx(spec).encode("utf-8")
        hosts = render_hosts_fragment().encode("ascii")
        if (args.nginx_output is None) != (args.hosts_output is None):
            raise ProviderBridgeConfigurationError(
                "provider bridge outputs must be configured together"
            )
        if args.nginx_output is not None and args.hosts_output is not None:
            with args.nginx_output.open("xb") as stream:
                stream.write(nginx)
            try:
                with args.hosts_output.open("xb") as stream:
                    stream.write(hosts)
            except BaseException:
                args.nginx_output.unlink(missing_ok=True)
                raise
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "rendered",
                    "nginx_sha256": hashlib.sha256(nginx).hexdigest(),
                    "hosts_sha256": hashlib.sha256(hosts).hexdigest(),
                    "installed": args.nginx_output is not None,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except ProviderBridgeConfigurationError:
        print(
            '{"schema_version":1,"status":"failed","error":"provider_bridge_configuration_invalid"}',
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
