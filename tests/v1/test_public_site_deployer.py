from __future__ import annotations

import base64
import dataclasses
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import ecorex.deployment.public_site as deployment
from ecorex.deployment.cloud_artifact import (
    BUILD_CONTRACT,
    cloud_manifest_file_bytes,
    cloud_manifest_signing_payload,
)
from ecorex.release import (
    Ed25519MemorySigner,
    public_bootstrap_authority_signing_bytes,
    public_bootstrap_freshness_signing_bytes,
)


RELEASE_ID = "release-stable-0123456789abcdef01234567"
BUILD_DIGEST = "b" * 64
MANIFEST_SHA256 = "c" * 64
PUBLICATION_SHA256 = "d" * 64
WAIVER_SHA256 = "e" * 64
SIGNATURE = {
    "algorithm": "ed25519",
    "key_id": "test-release-key",
    "value": base64.b64encode(b"s" * 64).decode("ascii"),
}
AUTHORIZATION_PRIVATE = Ed25519PrivateKey.from_private_bytes(b"k" * 32)
FRESHNESS_PRIVATE = Ed25519PrivateKey.from_private_bytes(b"f" * 32)
AUTHORIZATION_SIGNER = Ed25519MemorySigner(
    "ecorex-direct-release-test", AUTHORIZATION_PRIVATE
)
ADMIN_INDEX = b"<html><head><title>EcoreX Admin 1.0.0</title></head></html>"
ADMIN_CSS = b"body{color:CanvasText}"
ADMIN_JS = b'globalThis.ecorexAdminVersion="1.0.0";'


def _admin_identity() -> dict[str, object]:
    assets = []
    for payload, suffix, media_type in (
        (ADMIN_CSS, "css", "text/css"),
        (ADMIN_JS, "js", "text/javascript"),
    ):
        digest = hashlib.sha256(payload).hexdigest()
        assets.append(
            {
                "path": f"/ecorex-agent/admin/assets/admin.{digest}.{suffix}",
                "sha256": digest,
                "size_bytes": len(payload),
                "media_type": media_type,
            }
        )
    return {
        "schema_version": 1,
        "cloud_release_id": "ecorex-cloud-v1.0.0-test",
        "cloud_version": "1.0.0",
        "cloud_manifest_sha256": "8" * 64,
        "cloud_manifest_key_id": AUTHORIZATION_SIGNER.key_id,
        "index": {
            "path": "/ecorex-agent/admin/",
            "sha256": hashlib.sha256(ADMIN_INDEX).hexdigest(),
            "size_bytes": len(ADMIN_INDEX),
        },
        "assets": assets,
        "version_marker": {
            "header": "X-EcoreX-Product-Version",
            "value": "1.0.0",
        },
        "health": {
            "path": "/ecorex-agent/admin/health/ready",
            "status": 200,
            "body_sha256": hashlib.sha256(b'{"status":"ready"}').hexdigest(),
            "body_size_bytes": len(b'{"status":"ready"}'),
        },
    }


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _signature(
    private: Ed25519PrivateKey,
    *,
    key_id: str,
    payload: bytes,
) -> dict[str, str]:
    return {
        "algorithm": "ed25519",
        "key_id": key_id,
        "value": base64.b64encode(private.sign(payload)).decode("ascii"),
    }


def _resign_freshness(
    pointer: dict[str, object],
    *,
    issued_at: datetime,
    expires_at: datetime,
    key_id: str = "test-freshness-key",
    private: Ed25519PrivateKey = FRESHNESS_PRIVATE,
) -> None:
    freshness = pointer["freshness"]
    assert isinstance(freshness, dict)
    issued = issued_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    authority_sha256 = str(freshness["authority_sha256"])
    freshness.update(
        {
            "issued_at": issued,
            "expires_at": expires,
            "signature": _signature(
                private,
                key_id=key_id,
                payload=public_bootstrap_freshness_signing_bytes(
                    authority_sha256=authority_sha256,
                    issued_at=issued,
                    expires_at=expires,
                ),
            ),
        }
    )


def _sources(file_name: str) -> list[dict[str, object]]:
    return [
        {
            "source_id": "mirror",
            "kind": "github-cn-mirror",
            "priority": 0,
            "url": f"https://mirror.example/release/{file_name}",
        },
        {
            "source_id": "github",
            "kind": "github-release",
            "priority": 1,
            "url": f"https://github.example/release/{file_name}",
        },
        {
            "source_id": "cdn",
            "kind": "ecorex-cdn",
            "priority": 2,
            "url": f"https://cdn.example/release/{file_name}",
        },
    ]


def _pointer() -> dict[str, object]:
    now = datetime.now(UTC).replace(microsecond=0)
    issued_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    target = {
        "manifest_sha256": MANIFEST_SHA256,
        "release_id": RELEASE_ID,
        "version": "1.0.0",
        "build_digest": BUILD_DIGEST,
    }
    authority_payload = public_bootstrap_authority_signing_bytes(
        sequence=1,
        revision=RELEASE_ID,
        target=target,
    )
    authority_signature = _signature(
        AUTHORIZATION_PRIVATE,
        key_id=AUTHORIZATION_SIGNER.key_id,
        payload=authority_payload,
    )
    authority_sha256 = hashlib.sha256(authority_payload).hexdigest()
    freshness_payload = public_bootstrap_freshness_signing_bytes(
        authority_sha256=authority_sha256,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    freshness_signature = _signature(
        FRESHNESS_PRIVATE,
        key_id="test-freshness-key",
        payload=freshness_payload,
    )
    bootstrap = []
    for artifact_id, platform, architecture, file_name in (
        ("bootstrap-windows-x64", "windows", "x64", "bootstrap-windows.zip"),
        ("bootstrap-macos-arm64", "macos", "arm64", "bootstrap-arm64.zip"),
        ("bootstrap-macos-x64", "macos", "x64", "bootstrap-x64.zip"),
    ):
        bootstrap.append(
            {
                "artifact_id": artifact_id,
                "platform": platform,
                "architecture": architecture,
                "file_name": file_name,
                "size_bytes": 42,
                "sha256": hashlib.sha256(file_name.encode()).hexdigest(),
                "signature": SIGNATURE,
                "sources": _sources(file_name),
            }
        )
    return {
        "schema_version": 1,
        "document_type": "ecorex.public-bootstrap-discovery",
        "trust": "untrusted-discovery-hint",
        "status": "published",
        "authority": {
            "sequence": 1,
            "revision": RELEASE_ID,
            "target": target,
            "signature": authority_signature,
        },
        "freshness": {
            "authority_sha256": authority_sha256,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "signature": freshness_signature,
        },
        "release": {
            "release_id": RELEASE_ID,
            "version": "1.0.0",
            "channel": "stable",
            "created_at": issued_at,
            "build_digest": BUILD_DIGEST,
            "publication_receipt_sha256": PUBLICATION_SHA256,
            "manifest": {
                "file_name": "release-manifest.json",
                "sha256": MANIFEST_SHA256,
                "signature": SIGNATURE,
                "sources": _sources("release-manifest.json"),
            },
            "bootstrap_artifacts": bootstrap,
        },
    }


def _write_staging(paths: deployment.DeploymentPaths) -> Path:
    root = paths.staging_root / RELEASE_ID
    site = root / "site"
    assets = site / "assets"
    assets.mkdir(parents=True)
    image = b"test-png-payload"
    image_name = f"preview.{hashlib.sha256(image).hexdigest()[:12]}.png"
    javascript = b'export const ready=true; fetch("./public-bootstrap-index.json");'
    js_name = f"site.{hashlib.sha256(javascript).hexdigest()[:12]}.js"
    stylesheet = b"body{color:CanvasText}"
    css_name = f"styles.{hashlib.sha256(stylesheet).hexdigest()[:12]}.css"
    html = (
        "<!doctype html><html><head>"
        f'<link rel="stylesheet" href="./{css_name}">'
        f'<link rel="preload" href="./assets/{image_name}">'
        "</head><body>"
        '<a href="/ecorex-agent/admin/">admin</a>'
        f'<script src="./{js_name}"></script>'
        "</body></html>"
    ).encode()
    (site / "index.html").write_bytes(html)
    (site / js_name).write_bytes(javascript)
    (site / css_name).write_bytes(stylesheet)
    (assets / image_name).write_bytes(image)
    pointer_payload = _canonical(_pointer())
    (site / "public-bootstrap-index.json").write_bytes(pointer_payload)
    receipt = {
        "ok": True,
        "status": "deployable-with-explicit-operator-waiver",
        "protected_pipeline_passed": False,
        "release_id": RELEASE_ID,
        "version": "1.0.0",
        "manifest_sha256": MANIFEST_SHA256,
        "waiver_sha256": WAIVER_SHA256,
        "publication_receipt_sha256": PUBLICATION_SHA256,
        "public_index_sha256": hashlib.sha256(pointer_payload).hexdigest(),
        "public_index_status": "published",
    }
    (root / "direct-deployable.json").write_bytes(_canonical(receipt))
    unsigned = deployment._validate_unsigned_staged_site(
        RELEASE_ID,
        paths=paths,
        expected_owner_uid=None,
        authorization_expected=False,
    )
    unsigned = dataclasses.replace(unsigned, admin_identity=_admin_identity())
    authorization = deployment.sign_public_site_authorization(
        unsigned, signer=AUTHORIZATION_SIGNER
    )
    (root / "deployment-authorization.json").write_bytes(_canonical(authorization))
    paths.release_keyring_path.parent.mkdir(parents=True, exist_ok=True)
    public = AUTHORIZATION_PRIVATE.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    paths.release_keyring_path.write_bytes(
        _canonical(
            {AUTHORIZATION_SIGNER.key_id: base64.b64encode(public).decode("ascii")}
        )
    )
    freshness_public = FRESHNESS_PRIVATE.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    paths.publication_keyring_path.write_bytes(
        _canonical(
            {
                "test-freshness-key": base64.b64encode(freshness_public).decode(
                    "ascii"
                )
            }
        )
    )
    return site


def _write_signed_cloud_artifact(root: Path) -> Path:
    static = (
        root
        / "venv"
        / "lib"
        / "python3.11"
        / "site-packages"
        / "ecorex"
        / "control_plane"
        / "admin_web"
        / "static"
    )
    source = (
        Path(__file__).resolve().parents[2]
        / "ecorex"
        / "control_plane"
        / "admin_web"
        / "static"
    )
    static.mkdir(parents=True)
    rows = []
    for name in ("index.html", "admin.css", "admin.js", "asset-manifest.json"):
        payload = (source / name).read_bytes()
        target = static / name
        target.write_bytes(payload)
        rows.append(
            {
                "path": target.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                "posix_mode": "0644",
            }
        )
    manifest = {
        "schema_version": 1,
        "release_id": "ecorex-cloud-v1.0.0-test",
        "version": "1.0.0",
        "platform": "linux",
        "architecture": "aarch64",
        "python_version": "3.11.9",
        "build_contract": BUILD_CONTRACT,
        "source_commit": "a" * 40,
        "dependency_lock_manifest_sha256": "b" * 64,
        "files": rows,
    }
    manifest_bytes = cloud_manifest_file_bytes(manifest)
    (root / "cloud-release-manifest.json").write_bytes(manifest_bytes)
    signature = AUTHORIZATION_PRIVATE.sign(cloud_manifest_signing_payload(manifest))
    (root / "cloud-release-manifest.sig.json").write_bytes(
        _canonical(
            {
                "key_id": AUTHORIZATION_SIGNER.key_id,
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "signature_b64": base64.b64encode(signature).decode("ascii"),
            }
        )
    )
    return root


def _paths(tmp_path: Path) -> deployment.DeploymentPaths:
    paths = deployment.DeploymentPaths.for_test(tmp_path)
    paths.download_root.mkdir(parents=True)
    return paths


def _pointer_trust(
    paths: deployment.DeploymentPaths,
) -> deployment.PublicPointerTrust:
    return deployment._read_public_pointer_trust(
        paths,
        expected_owner_uid=None,
    )


class _Controller:
    def __init__(self) -> None:
        self.validations = 0
        self.reloads = 0

    def validate(self) -> None:
        self.validations += 1

    def reload(self) -> None:
        self.reloads += 1


class _Readback:
    def __init__(
        self,
        site: deployment.ValidatedSite,
        *,
        admin_status: int = 200,
        pointer_payload: bytes | None = None,
    ) -> None:
        self.site = site
        self.admin_status = admin_status
        self.pointer_payload = (
            site.public_index.payload
            if pointer_payload is None
            else pointer_payload
        )

    def get(self, url: str, *, maximum_bytes: int) -> deployment.HttpReadback:
        del maximum_bytes
        relative = url.removeprefix(deployment.PUBLIC_ORIGIN)
        if relative == "":
            value = self.site.file("index.html")
            return deployment.HttpReadback(
                200, {"cache-control": ("no-store",)}, value.payload
            )
        if relative == "public-bootstrap-index.json":
            return deployment.HttpReadback(
                200, {"cache-control": ("no-store",)}, self.pointer_payload
            )
        if relative == "admin/":
            return deployment.HttpReadback(
                self.admin_status,
                {
                    "cache-control": ("no-store, max-age=0",),
                    "content-security-policy": (
                        deployment.ADMIN_CONTENT_SECURITY_POLICY,
                    ),
                    deployment.ADMIN_VERSION_HEADER: ("1.0.0",),
                },
                ADMIN_INDEX if self.admin_status == 200 else b"bad gateway",
            )
        if relative == "admin/health/ready":
            return deployment.HttpReadback(
                200,
                {
                    "cache-control": ("no-store",),
                    deployment.ADMIN_VERSION_HEADER: ("1.0.0",),
                },
                b'{"status":"ready"}',
            )
        if relative.startswith("admin/assets/"):
            payload = ADMIN_CSS if relative.endswith(".css") else ADMIN_JS
            return deployment.HttpReadback(
                200,
                {
                    "cache-control": (
                        "public, max-age=31536000, immutable",
                    ),
                    deployment.ADMIN_VERSION_HEADER: ("1.0.0",),
                },
                payload,
            )
        value = self.site.file(relative)
        return deployment.HttpReadback(
            200,
            {"cache-control": ("public, max-age=31536000, immutable",)},
            value.payload,
        )


def _portable_noreplace(source: Path, target: Path) -> None:
    if os.path.lexists(target):
        raise deployment.PublicSiteDeployError("atomic_noreplace_failed")
    source.rename(target)


def _portable_exchange(source: Path, target: Path) -> None:
    temporary = source.with_name(f".{source.name}.test-exchange")
    source.rename(temporary)
    target.rename(source)
    temporary.rename(target)


def _enable_portable_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``apply(..., enforce_server_fence=False)`` exercises the transaction on
    # an unprivileged CI account.  Linux still exposes ``os.lchown``, so the
    # production-only root:static ownership operation must be represented by
    # the portable boundary just like renameat2/flock.  Keep the production
    # implementation intact: the real server path still lchowns every current
    # symlink to root:STATIC_READ_GID and validates it during layout takeover.
    if sys.platform.startswith("linux"):
        monkeypatch.setattr(deployment.os, "lchown", lambda *_args: None)
    monkeypatch.setattr(deployment, "_rename_noreplace", _portable_noreplace)
    monkeypatch.setattr(deployment, "_rename_exchange", _portable_exchange)
    monkeypatch.setattr(deployment, "_acquire_lock", lambda _path: 41)
    monkeypatch.setattr(deployment, "_release_lock", lambda _descriptor: None)


def _require_symlink(tmp_path: Path) -> None:
    target = tmp_path / "symlink-target"
    link = tmp_path / "symlink-probe"
    target.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit directory symlinks")
    link.unlink()
    target.rmdir()


def test_plan_binds_exact_published_site_without_mutation(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_staging(paths)
    site = deployment.validate_staged_site(
        RELEASE_ID, paths=paths, expected_owner_uid=None
    )

    result = deployment.plan(RELEASE_ID, paths=paths, expected_owner_uid=None)

    assert result["status"] == "planned"
    assert result["mutation_performed"] is False
    assert result["target"] == "https://dl.ecoremedia.net/ecorex-agent/"
    assert result["slot_action"] == "create"
    assert "public-bootstrap-index.json" not in {
        value.relative_path for value in site.files
    }
    authorization = json.loads(
        (site.root.parent / "deployment-authorization.json").read_text()
    )
    assert (
        authorization["authorization"]["public_index_sha256"]
        == site.public_index.sha256
    )
    assert not paths.slots_root.exists()
    assert not os.path.lexists(paths.current_path)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("extra", "site_file_set_invalid"),
        ("bad-hash", "site_content_address_invalid"),
        ("unpublished", "site_published_identity_mismatch"),
    ],
)
def test_staging_rejects_unbound_or_unpublished_bytes(
    tmp_path: Path,
    mutation: str,
    code: str,
) -> None:
    paths = _paths(tmp_path)
    site = _write_staging(paths)
    if mutation == "extra":
        (site / "debug.log").write_text("internal implementation detail")
    elif mutation == "bad-hash":
        next(site.glob("site.*.js")).write_text("tampered")
    else:
        pointer = json.loads((site / "public-bootstrap-index.json").read_text())
        pointer.update(status="unpublished", authority=None, freshness=None, release=None)
        (site / "public-bootstrap-index.json").write_bytes(_canonical(pointer))

    with pytest.raises(deployment.PublicSiteDeployError, match=code):
        deployment.validate_staged_site(
            RELEASE_ID, paths=paths, expected_owner_uid=None
        )


def test_readback_requires_no_store_immutable_assets_and_admin_route(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    _write_staging(paths)
    site = deployment.validate_staged_site(
        RELEASE_ID, paths=paths, expected_owner_uid=None
    )

    trust = _pointer_trust(paths)
    readback = deployment._verify_target_readback(site, _Readback(site), trust)
    assert readback["index"]["cache_control"] == "no-store"
    assert len(readback["assets"]) == 3
    assert readback["admin"]["status"] == 200

    with pytest.raises(
        deployment.PublicSiteDeployError, match="admin_route_readback_failed"
    ):
        deployment._verify_target_readback(
            site, _Readback(site, admin_status=502), trust
        )

    class BrokenAdminReadback(_Readback):
        def __init__(self, site_value, failure: str) -> None:
            super().__init__(site_value)
            self.failure = failure

        def get(self, url: str, *, maximum_bytes: int) -> deployment.HttpReadback:
            if self.failure == "generic" and url == deployment.ADMIN_URL:
                return deployment.HttpReadback(
                    200,
                    {
                        "cache-control": ("no-store",),
                        "content-security-policy": (
                            deployment.ADMIN_CONTENT_SECURITY_POLICY,
                        ),
                        deployment.ADMIN_VERSION_HEADER: ("1.0.0",),
                    },
                    b"old or generic administrator page",
                )
            if self.failure == "asset" and "/admin/assets/" in url:
                return deployment.HttpReadback(404, {}, b"missing")
            if self.failure == "health" and url == deployment.ADMIN_HEALTH_URL:
                return deployment.HttpReadback(
                    200,
                    {
                        "cache-control": ("no-store",),
                        deployment.ADMIN_VERSION_HEADER: ("1.0.0",),
                    },
                    b'{"status":"unavailable"}',
                )
            return super().get(url, maximum_bytes=maximum_bytes)

    for failure, code in (
        ("generic", "admin_route_readback_failed"),
        ("asset", "admin_asset_readback_failed"),
        ("health", "admin_health_readback_failed"),
    ):
        with pytest.raises(deployment.PublicSiteDeployError, match=code):
            deployment._verify_target_readback(
                site, BrokenAdminReadback(site, failure), trust
            )


def test_readback_accepts_only_freshness_renewal_for_authorized_target(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    _write_staging(paths)
    site = deployment.validate_staged_site(
        RELEASE_ID, paths=paths, expected_owner_uid=None
    )
    refreshed = json.loads(site.public_index.payload)
    now = datetime.now(UTC).replace(microsecond=0)
    _resign_freshness(
        refreshed,
        issued_at=now,
        expires_at=now + timedelta(hours=2),
    )
    refreshed_payload = _canonical(refreshed)

    readback = deployment._verify_target_readback(
        site,
        _Readback(site, pointer_payload=refreshed_payload),
        _pointer_trust(paths),
    )
    assert readback["pointer"]["sha256"] == hashlib.sha256(
        refreshed_payload
    ).hexdigest()

    rejected: list[dict[str, object]] = []
    tampered = json.loads(refreshed_payload)
    tampered["freshness"]["signature"]["value"] = base64.b64encode(
        b"x" * 64
    ).decode("ascii")
    rejected.append(tampered)
    unknown_key = json.loads(refreshed_payload)
    unknown_key["freshness"]["signature"]["key_id"] = "unknown-freshness-key"
    rejected.append(unknown_key)
    expired = json.loads(refreshed_payload)
    _resign_freshness(
        expired,
        issued_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )
    rejected.append(expired)
    for invalid in rejected:
        with pytest.raises(
            deployment.PublicSiteDeployError,
            match="public_pointer_readback_failed",
        ):
            deployment._verify_target_readback(
                site,
                _Readback(site, pointer_payload=_canonical(invalid)),
                _pointer_trust(paths),
            )

    drifted = json.loads(refreshed_payload)
    drifted_digest = "7" * 64
    drifted["authority"]["target"]["build_digest"] = drifted_digest
    drifted["release"]["build_digest"] = drifted_digest
    target = drifted["authority"]["target"]
    authority_payload = public_bootstrap_authority_signing_bytes(
        sequence=1,
        revision=RELEASE_ID,
        target=target,
    )
    drifted["freshness"]["authority_sha256"] = hashlib.sha256(
        authority_payload
    ).hexdigest()
    drifted["authority"]["signature"] = _signature(
        AUTHORIZATION_PRIVATE,
        key_id=AUTHORIZATION_SIGNER.key_id,
        payload=authority_payload,
    )
    _resign_freshness(
        drifted,
        issued_at=now,
        expires_at=now + timedelta(hours=2),
    )
    with pytest.raises(
        deployment.PublicSiteDeployError,
        match="public_pointer_readback_failed",
    ):
        deployment._verify_target_readback(
            site,
            _Readback(site, pointer_payload=_canonical(drifted)),
            _pointer_trust(paths),
        )


def test_authorization_rejects_receipt_site_key_and_domain_tampering(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    site_root = _write_staging(paths)
    release_stage = site_root.parent

    receipt_path = release_stage / "direct-deployable.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["waiver_sha256"] = "9" * 64
    receipt_path.write_bytes(_canonical(receipt))
    with pytest.raises(
        deployment.PublicSiteDeployError,
        match="site_authorization_identity_mismatch",
    ):
        deployment.validate_staged_site(
            RELEASE_ID, paths=paths, expected_owner_uid=None
        )

    shutil.rmtree(paths.staging_root)
    site_root = _write_staging(paths)
    image = next((site_root / "assets").iterdir())
    replacement = b"different-but-content-addressed-image"
    replacement_name = f"preview.{hashlib.sha256(replacement).hexdigest()[:12]}.png"
    (site_root / "assets" / replacement_name).write_bytes(replacement)
    image.unlink()
    html_path = site_root / "index.html"
    html_path.write_text(
        html_path.read_text().replace(image.name, replacement_name)
    )
    with pytest.raises(
        deployment.PublicSiteDeployError,
        match="site_authorization_identity_mismatch",
    ):
        deployment.validate_staged_site(
            RELEASE_ID, paths=paths, expected_owner_uid=None
        )

    shutil.rmtree(paths.staging_root)
    _write_staging(paths)
    wrong = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    paths.release_keyring_path.write_bytes(
        _canonical(
            {AUTHORIZATION_SIGNER.key_id: base64.b64encode(wrong).decode("ascii")}
        )
    )
    with pytest.raises(
        deployment.PublicSiteDeployError,
        match="site_authorization_signature_rejected",
    ):
        deployment.validate_staged_site(
            RELEASE_ID, paths=paths, expected_owner_uid=None
        )

    shutil.rmtree(paths.staging_root)
    site_root = _write_staging(paths)
    unsigned = deployment._validate_unsigned_staged_site(
        RELEASE_ID,
        paths=paths,
        expected_owner_uid=None,
        authorization_expected=True,
    )
    authorization_path = site_root.parent / "deployment-authorization.json"
    authorization = json.loads(authorization_path.read_text())
    unsigned = dataclasses.replace(
        unsigned,
        admin_identity=authorization["authorization"]["admin"],
    )
    wrong_domain_signature = AUTHORIZATION_PRIVATE.sign(
        b"ecorex.public-site-deployment.v0\0"
        + deployment._canonical_json(
            deployment.public_site_authorization_payload(unsigned)
        )
    )
    authorization["signature"]["value"] = base64.b64encode(
        wrong_domain_signature
    ).decode("ascii")
    authorization_path.write_bytes(_canonical(authorization))
    with pytest.raises(
        deployment.PublicSiteDeployError,
        match="site_authorization_signature_rejected",
    ):
        deployment.validate_staged_site(
            RELEASE_ID, paths=paths, expected_owner_uid=None
        )


def test_offline_signing_cli_never_requires_production_download_root(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    site_root = _write_staging(paths)
    release_stage = site_root.parent
    (release_stage / "deployment-authorization.json").unlink()
    module = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "sign-v1-public-site-deployment.py"
        )
    )

    class FakeExternalSigner:
        key_id = AUTHORIZATION_SIGNER.key_id
        public_key_bytes = AUTHORIZATION_PRIVATE.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

        def __init__(self) -> None:
            self.receipts: list[SimpleNamespace] = []

        def sign(self, payload: bytes) -> bytes:
            signature = AUTHORIZATION_PRIVATE.sign(payload)
            self.receipts.append(
                SimpleNamespace(payload_sha256=hashlib.sha256(payload).hexdigest())
            )
            return signature

    module["run"].__globals__["_signer"] = FakeExternalSigner
    cloud_artifact = _write_signed_cloud_artifact(tmp_path / "cloud-artifact")
    code = module["run"](
        [
            "--release-id",
            RELEASE_ID,
            "--staging-release-dir",
            str(release_stage),
            "--cloud-artifact-root",
            str(cloud_artifact),
        ]
    )

    assert code == 0
    assert (release_stage / "deployment-authorization.json").is_file()
    offline_paths = deployment.DeploymentPaths.for_offline_staging(release_stage)
    assert offline_paths.download_root == release_stage.parent.resolve()
    assert offline_paths.download_root != deployment.DOWNLOAD_ROOT
    with pytest.raises(
        deployment.PublicSiteDeployError, match="production_server_fence_failed"
    ):
        deployment.apply(
            RELEASE_ID,
            confirm_target=deployment.PUBLIC_ORIGIN,
            paths=offline_paths,
        )


def test_apply_creates_no_clobber_slot_atomic_pointer_and_root_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_symlink(tmp_path)
    paths = _paths(tmp_path)
    _write_staging(paths)
    site = deployment.validate_staged_site(
        RELEASE_ID, paths=paths, expected_owner_uid=None
    )
    _enable_portable_apply(monkeypatch)

    result = deployment.apply(
        RELEASE_ID,
        confirm_target=deployment.PUBLIC_ORIGIN,
        paths=paths,
        controller=_Controller(),
        client=_Readback(site),
        expected_owner_uid=None,
        enforce_server_fence=False,
    )

    assert result["status"] == "passed"
    assert paths.current_path.is_symlink()
    assert os.readlink(paths.current_path) == f"site-slots/{RELEASE_ID}"
    slot = paths.slots_root / RELEASE_ID
    assert not (slot / "public-bootstrap-index.json").exists()
    immutable_before = {
        path.relative_to(slot).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in slot.rglob("*")
        if path.is_file()
    }
    assert not paths.journal_path.exists()
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["receipt_type"] == "ecorex-public-site-deployment"
    assert receipt["readback"]["admin"]["status"] == 200
    assert receipt["previous_target_type"] == "absent"

    refreshed = json.loads(site.public_index.payload)
    now = datetime.now(UTC).replace(microsecond=0)
    _resign_freshness(
        refreshed,
        issued_at=now,
        expires_at=now + timedelta(hours=2),
    )
    second = deployment.apply(
        RELEASE_ID,
        confirm_target=deployment.PUBLIC_ORIGIN,
        paths=paths,
        controller=_Controller(),
        client=_Readback(site, pointer_payload=_canonical(refreshed)),
        expected_owner_uid=None,
        enforce_server_fence=False,
    )
    assert second["idempotent"] is True
    assert second["receipt_sha256"] == result["receipt_sha256"]
    assert immutable_before == {
        path.relative_to(slot).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in slot.rglob("*")
        if path.is_file()
    }


class _CurrentAwareReadback(_Readback):
    def __init__(
        self,
        site: deployment.ValidatedSite,
        paths: deployment.DeploymentPaths,
    ) -> None:
        super().__init__(site)
        self.paths = paths

    def get(self, url: str, *, maximum_bytes: int) -> deployment.HttpReadback:
        if url == deployment.PUBLIC_ORIGIN and not self.paths.current_path.is_symlink():
            body = (self.paths.current_path / "index.html").read_bytes()
            return deployment.HttpReadback(200, {"cache-control": ("no-store",)}, body)
        if url == deployment.ADMIN_URL:
            return deployment.HttpReadback(502, {}, b"bad gateway")
        if (
            url == f"{deployment.PUBLIC_ORIGIN}public-bootstrap-index.json"
            and self.paths.public_pointer_path.is_file()
        ):
            return deployment.HttpReadback(
                200,
                {"cache-control": ("no-store",)},
                self.paths.public_pointer_path.read_bytes(),
            )
        return super().get(url, maximum_bytes=maximum_bytes)


def test_failed_first_v1_activation_restores_legacy_directory_and_rechecks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_symlink(tmp_path)
    paths = _paths(tmp_path)
    _write_staging(paths)
    pointer_before = (paths.staging_root / RELEASE_ID / "site" / "public-bootstrap-index.json").read_bytes()
    paths.public_pointer_path.parent.mkdir(parents=True)
    paths.public_pointer_path.write_bytes(pointer_before)
    paths.current_path.mkdir()
    legacy_html = b"<html>legacy-v0</html>"
    (paths.current_path / "index.html").write_bytes(legacy_html)
    site = deployment.validate_staged_site(
        RELEASE_ID, paths=paths, expected_owner_uid=None
    )
    _enable_portable_apply(monkeypatch)
    synced: list[Path] = []
    real_fsync = deployment._fsync_directory

    def record_fsync(path: Path) -> None:
        synced.append(path)
        real_fsync(path)

    monkeypatch.setattr(deployment, "_fsync_directory", record_fsync)

    with pytest.raises(
        deployment.PublicSiteDeployError, match="admin_route_readback_failed"
    ):
        deployment.apply(
            RELEASE_ID,
            confirm_target=deployment.PUBLIC_ORIGIN,
            paths=paths,
            controller=_Controller(),
            client=_CurrentAwareReadback(site, paths),
            expected_owner_uid=None,
            enforce_server_fence=False,
        )

    assert paths.current_path.is_dir()
    assert not paths.current_path.is_symlink()
    assert (paths.current_path / "index.html").read_bytes() == legacy_html
    assert not paths.journal_path.exists()
    assert paths.download_root in synced
    assert paths.legacy_root in synced
    assert not any(paths.legacy_root.iterdir())
    assert paths.public_pointer_path.read_bytes() == pointer_before


def test_process_crash_after_pointer_switch_is_completed_from_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_symlink(tmp_path)
    paths = _paths(tmp_path)
    _write_staging(paths)
    site = deployment.validate_staged_site(
        RELEASE_ID, paths=paths, expected_owner_uid=None
    )
    _enable_portable_apply(monkeypatch)
    real_advance = deployment._advance_journal
    crashed = False

    def crash_once(paths_value, journal, phase):
        nonlocal crashed
        if not crashed and phase == "current_switched":
            crashed = True
            raise KeyboardInterrupt
        return real_advance(paths_value, journal, phase)

    monkeypatch.setattr(deployment, "_advance_journal", crash_once)
    with pytest.raises(KeyboardInterrupt):
        deployment.apply(
            RELEASE_ID,
            confirm_target=deployment.PUBLIC_ORIGIN,
            paths=paths,
            controller=_Controller(),
            client=_Readback(site),
            expected_owner_uid=None,
            enforce_server_fence=False,
        )
    assert paths.journal_path.exists()
    assert paths.current_path.is_symlink()

    monkeypatch.setattr(deployment, "_advance_journal", real_advance)
    recovered = deployment.apply(
        RELEASE_ID,
        confirm_target=deployment.PUBLIC_ORIGIN,
        paths=paths,
        controller=_Controller(),
        client=_Readback(site),
        expected_owner_uid=None,
        enforce_server_fence=False,
    )
    assert recovered["recovered"] is True
    assert recovered["resolution"] == "target"
    assert not paths.journal_path.exists()


def test_receipt_then_commit_marker_failure_recovers_verified_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_symlink(tmp_path)
    paths = _paths(tmp_path)
    _write_staging(paths)
    site = deployment.validate_staged_site(
        RELEASE_ID, paths=paths, expected_owner_uid=None
    )
    _enable_portable_apply(monkeypatch)
    real_clear = deployment._clear_journal
    failed = False

    def fail_once(paths_value) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise deployment.PublicSiteDeployError(
                "site_activation_journal_clear_failed"
            )
        real_clear(paths_value)

    monkeypatch.setattr(deployment, "_clear_journal", fail_once)
    with pytest.raises(
        deployment.PublicSiteDeployError,
        match="site_activation_recovery_required",
    ):
        deployment.apply(
            RELEASE_ID,
            confirm_target=deployment.PUBLIC_ORIGIN,
            paths=paths,
            controller=_Controller(),
            client=_Readback(site),
            expected_owner_uid=None,
            enforce_server_fence=False,
        )
    assert paths.current_path.is_symlink()
    assert paths.journal_path.is_file()
    assert (paths.receipt_root / f"{RELEASE_ID}.json").is_file()

    recovered = deployment.apply(
        RELEASE_ID,
        confirm_target=deployment.PUBLIC_ORIGIN,
        paths=paths,
        controller=_Controller(),
        client=_Readback(site),
        expected_owner_uid=None,
        enforce_server_fence=False,
    )
    assert recovered["recovered"] is True
    assert recovered["resolution"] == "target"
    assert not paths.journal_path.exists()


def test_precreated_legacy_root_symlink_is_rejected_before_ownership_change(
    tmp_path: Path,
) -> None:
    _require_symlink(tmp_path)
    paths = _paths(tmp_path)
    outside = tmp_path / "outside-legacy"
    outside.mkdir()
    paths.legacy_root.parent.mkdir(parents=True, exist_ok=True)
    paths.legacy_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        deployment.PublicSiteDeployError, match="production_site_layout_invalid"
    ):
        deployment._normalize_directory(
            paths.legacy_root,
            mode=0o700,
            gid=0,
            device=None,
            create=True,
        )


def test_conflicting_existing_slot_is_never_overwritten(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_staging(paths)
    site = deployment.validate_staged_site(
        RELEASE_ID, paths=paths, expected_owner_uid=None
    )
    slot = paths.slots_root / RELEASE_ID
    (slot / "assets").mkdir(parents=True)
    for value in site.files:
        target = slot / value.relative_path
        target.write_bytes(value.payload)
    next(slot.glob("site.*.js")).write_text("conflicting bytes")

    with pytest.raises(deployment.PublicSiteDeployError, match="site_slot_file_mismatch"):
        deployment.plan(RELEASE_ID, paths=paths, expected_owner_uid=None)
