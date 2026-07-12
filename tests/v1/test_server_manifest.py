from __future__ import annotations

import base64
import hashlib
from dataclasses import replace

import pytest

from ecorex.server import BundleIntegrityError, WebBundleManifest, WebFileRecord
from ecorex.server.bundle import RUNTIME_CONFIG_MARKER, _validate_index
from ecorex.update import SignatureEnvelope


def _signature() -> SignatureEnvelope:
    return SignatureEnvelope(
        algorithm="ed25519",
        key_id="test-key",
        value=base64.b64encode(b"0" * 64).decode("ascii"),
    )


def _record(path: str, *, size: int = 1, immutable: bool = True) -> WebFileRecord:
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    if immutable:
        stem, dot, suffix = path.rpartition(".")
        path = f"{stem}.{digest[:12]}{dot}{suffix}"
    return WebFileRecord(
        path=path,
        size_bytes=size,
        sha256=digest,
        immutable=immutable,
    )


def _manifest(files: tuple[WebFileRecord, ...]) -> WebBundleManifest:
    return WebBundleManifest(
        schema_version=1,
        release_id="release-1.0.0-stable-001",
        version="1.0.0",
        build_digest="a" * 64,
        bundle_sha256=WebBundleManifest.compute_bundle_sha256(files),
        entrypoint="index.html",
        files=files,
        signature=_signature(),
    )


def _index() -> WebFileRecord:
    return WebFileRecord(
        path="index.html",
        size_bytes=1,
        sha256="b" * 64,
        immutable=False,
    )


@pytest.mark.parametrize(
    "path",
    [
        ".",
        "../secret.js",
        ".well-known/app.js",
        "api/v1.js",
        "CON.js",
        "assets\\app.js",
    ],
)
def test_web_file_paths_are_portable_and_cannot_shadow_api(path):
    with pytest.raises(BundleIntegrityError):
        WebFileRecord(
            path=path,
            size_bytes=1,
            sha256="a" * 64,
            immutable=False,
        )


def test_web_manifest_rejects_cross_platform_path_collisions():
    digest = hashlib.sha256(b"asset").hexdigest()
    first = WebFileRecord(
        path=f"Assets/App.{digest[:12]}.js",
        size_bytes=1,
        sha256=digest,
        immutable=True,
    )
    second = replace(first, path=f"assets/app.{digest[:12]}.js")
    files = (_index(), first, second)
    with pytest.raises(BundleIntegrityError, match="colliding"):
        _manifest(files)


def test_web_manifest_requires_one_html_entrypoint_and_hashed_assets():
    non_immutable = _record("assets/app.js", immutable=False)
    with pytest.raises(BundleIntegrityError, match="non-entrypoint"):
        _manifest((_index(), non_immutable))

    second_html = _record("shell.html")
    with pytest.raises(BundleIntegrityError, match="only HTML"):
        _manifest((_index(), second_html))


def test_web_manifest_has_a_total_memory_bound():
    first = _record("assets/first.bin", size=80 * 1024 * 1024)
    second = _record("assets/second.bin", size=80 * 1024 * 1024)
    with pytest.raises(BundleIntegrityError, match="150 MiB"):
        _manifest((_index(), first, second))


def test_web_manifest_parser_rejects_non_json_payload_types():
    with pytest.raises(BundleIntegrityError, match="text or bytes"):
        WebBundleManifest.from_json({})


def test_index_contract_requires_a_head_marker_and_external_scripts():
    valid = (
        f"<html><head>{RUNTIME_CONFIG_MARKER}"
        '<script src="/assets/app.js"></script></head><body></body></html>'
    ).encode()
    assert _validate_index(valid).startswith("<html>")

    marker_in_body = (
        f"<html><head></head><body>{RUNTIME_CONFIG_MARKER}</body></html>"
    ).encode()
    with pytest.raises(BundleIntegrityError, match="inside <head>"):
        _validate_index(marker_in_body)

    disguised_inline = (
        f"<html><head>{RUNTIME_CONFIG_MARKER}"
        '<script data-src="/assets/app.js">alert(1)</script></head></html>'
    ).encode()
    with pytest.raises(BundleIntegrityError, match="inline scripts"):
        _validate_index(disguised_inline)
