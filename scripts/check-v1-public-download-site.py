#!/usr/bin/env python3
"""Strict static gate for the v1 public Bootstrap discovery site."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "deploy" / "ecorex-site"
HASHED_ASSET = re.compile(
    r"^(?P<stem>[A-Za-z0-9][A-Za-z0-9._-]*)\."
    r"(?P<digest>[0-9a-f]{12})\."
    r"(?P<suffix>js|css|png|svg|webp|woff2)$"
)
FORBIDDEN_LEGACY = (
    "install-webui.ps1",
    "install-webui.sh",
    "manifest.json",
    "release-index.json",
    "public-bootstrap-index.json.lock",
    "systemd/ecorex-web.service.example",
    "caddy/ecorex-web.routes.caddy",
    "nginx/ecorex-web.conf.example",
    "admin/index.html",
    "admin/admin.js",
    "admin/admin.css",
    "site.js",
    "styles.css",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def _validate_hashed_file(path: Path, errors: list[str]) -> None:
    match = HASHED_ASSET.fullmatch(path.name)
    _require(match is not None, f"public asset is not content addressed: {path}", errors)
    if match is not None:
        _require(
            _sha256(path).startswith(match.group("digest")),
            f"public asset hash/name mismatch: {path}",
            errors,
        )


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from ecorex.release import (  # noqa: PLC0415 - gate must run from any cwd
        unpublished_public_bootstrap_index,
        validate_public_bootstrap_index,
    )

    errors: list[str] = []
    for relative in FORBIDDEN_LEGACY:
        _require(
            not (SITE / relative).exists(),
            f"legacy public release input remains: deploy/ecorex-site/{relative}",
            errors,
        )

    pointer_path = SITE / "public-bootstrap-index.json"
    try:
        pointer_bytes = pointer_path.read_bytes()
        pointer = json.loads(pointer_bytes.decode("utf-8"))
        validate_public_bootstrap_index(pointer)
        expected = (
            json.dumps(
                unpublished_public_bootstrap_index(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        _require(
            pointer_bytes == expected,
            "checked-in public pointer must be the canonical unpublished document",
            errors,
        )
        _require(
            b"https://" not in pointer_bytes and b"signature" not in pointer_bytes,
            "checked-in public pointer must not contain fabricated URLs or signatures",
            errors,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        errors.append(f"public Bootstrap pointer is invalid: {error}")

    scripts = sorted(SITE.glob("site.*.js"))
    styles = sorted(SITE.glob("styles.*.css"))
    _require(len(scripts) == 1, "site must contain exactly one hashed public JS", errors)
    _require(len(styles) == 1, "site must contain exactly one hashed public CSS", errors)
    public_assets = scripts + styles + sorted((SITE / "assets").glob("*"))
    for path in public_assets:
        if path.is_file():
            _validate_hashed_file(path, errors)

    try:
        html = (SITE / "index.html").read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"public index.html cannot be read: {error}")
        html = ""
    _require("__HASH__" not in html, "public HTML contains an unresolved hash", errors)
    script_tags = re.findall(r"<script\b([^>]*)>", html, flags=re.IGNORECASE)
    _require(
        len(script_tags) == 1 and all("src=" in attributes for attributes in script_tags),
        "public HTML must contain exactly one external script and no inline code",
        errors,
    )
    _require(
        "<style" not in html.casefold() and "<base" not in html.casefold(),
        "public HTML must not contain inline style or a base URL",
        errors,
    )
    _require(
        'href="/ecorex-agent/admin/"' in html
        and 'href="/admin/"' not in html
        and 'href="./admin/"' not in html,
        "public HTML must link directly to the canonical v1 Control Plane administrator WebUI",
        errors,
    )
    for path in scripts + styles:
        _require(
            f'./{path.name}' in html,
            f"public HTML does not reference {path.name}",
            errors,
        )
    for relative in re.findall(r'(?:src|href)="\./([^"#?]+)', html):
        target = (SITE / relative).resolve()
        _require(
            target == SITE.resolve() or SITE.resolve() in target.parents,
            f"public HTML reference escapes site root: {relative}",
            errors,
        )
        _require(target.exists(), f"public HTML reference is missing: {relative}", errors)

    if len(scripts) == 1:
        javascript = scripts[0].read_text(encoding="utf-8")
        _require(
            "export async function verifyManifestBytes" in javascript
            and "await sha256Hex(payload" in javascript
            and "await verifyManifestBytes(index.release.manifest)" in javascript,
            "public JS must fail closed on exact manifest-byte SHA-256 verification",
            errors,
        )
        _require(
            "cache: \"no-store\"" in javascript
            and "credentials: \"omit\"" in javascript
            and "new AbortController()" in javascript,
            "public discovery fetches must bypass caches/credentials and time out",
            errors,
        )

    caddy = (SITE / "caddy" / "ecorex-agent.routes.caddy").read_text(
        encoding="utf-8"
    )
    nginx = (SITE / "nginx" / "ecorex-agent.conf.example").read_text(
        encoding="utf-8"
    )
    _require(
        "/ecorex-agent/public-bootstrap-index.json" in caddy
        and 'Cache-Control "no-store"' in caddy
        and "Content-Security-Policy" in caddy
        and "script-src 'self'" in caddy
        and "frame-ancestors 'none'" in caddy
        and "max-age=31536000, immutable" in caddy
        and "[0-9a-f]{12}" in caddy,
        "Caddy must no-store mutable discovery and cache hashed assets immutably",
        errors,
    )
    _require(
        "handle /ecorex-agent/admin/*" in caddy
        and "uri strip_prefix /ecorex-agent" in caddy
        and "handle /api/v1/admin*" in caddy
        and "reverse_proxy 127.0.0.1:18084" in caddy
        and "127.0.0.1:9909" not in caddy
        and "/message" not in caddy
        and "/upload" not in caddy
        and "basic_auth" not in caddy,
        "Caddy must expose only the v1 Control Plane admin proxy, not a legacy Web Runtime",
        errors,
    )
    _require(
        "location = /ecorex-agent/public-bootstrap-index.json" in nginx
        and 'Cache-Control "no-store"' in nginx
        and "Content-Security-Policy" in nginx
        and "script-src 'self'" in nginx
        and "frame-ancestors 'none'" in nginx
        and "max-age=31536000, immutable" in nginx
        and "[0-9a-f]{12}" in nginx,
        "Nginx must no-store mutable discovery and cache hashed assets immutably",
        errors,
    )
    _require(
        "location ^~ /admin/" in nginx
        and "location ^~ /api/v1/admin/" in nginx
        and "location = /ecorex-agent/admin/health/ready" in nginx
        and "rewrite ^ /health/ready break;" in nginx
        and "$ecorex_control_plane" in nginx
        and "$ecorex_web_runtime" not in nginx
        and "127.0.0.1:9909" not in nginx
        and "auth_basic" not in nginx
        and "application/octet-stream exe dmg" not in nginx,
        "Nginx must expose only signed Runtime downloads and the v1 Control Plane admin proxy",
        errors,
    )

    result = {
        "schema_version": 1,
        "status": "failed" if errors else "passed",
        "public_pointer": "unpublished",
        "hashed_asset_count": len([path for path in public_assets if path.is_file()]),
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
