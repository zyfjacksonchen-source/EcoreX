from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    ReleaseBuildError,
    ReleaseBuilder,
    ReleaseBuildSpec,
    WebBundleBuildInput,
)
from ecorex.update import ReleaseChannel, ReleaseSource, SourceKind


RUNTIME_MARKER = "<!--__ECOREX_RUNTIME_CONFIG__-->"


def _asset_path(content: bytes, *, stem: str = "app", suffix: str = ".js") -> str:
    return f"assets/{stem}.{hashlib.sha256(content).hexdigest()[:12]}{suffix}"


def _dist(root: Path, *, index: str | None = None) -> Path:
    root.mkdir(parents=True)
    content = b"document.body.dataset.ready='true';\n"
    relative = _asset_path(content)
    target = root / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    if index is None:
        index = (
            f"<html><head>{RUNTIME_MARKER}"
            f'<script type="module" src="/{relative}"></script>'
            "</head><body></body></html>"
        )
    (root / "index.html").write_text(index, encoding="utf-8", newline="\n")
    return root


def _spec(tmp_path: Path, dist: Path) -> ReleaseBuildSpec:
    core = tmp_path / "core"
    core.mkdir(exist_ok=True)
    (core / "runtime.txt").write_text("runtime", encoding="utf-8")
    sources = (
        ReleaseSource("mirror", SourceKind.GITHUB_CN_MIRROR, 0, "https://m.example/v1"),
        ReleaseSource("github", SourceKind.GITHUB_RELEASE, 1, "https://g.example/v1"),
        ReleaseSource("cdn", SourceKind.ECOREX_CDN, 2, "https://c.example/v1"),
    )
    return ReleaseBuildSpec(
        channel=ReleaseChannel.STABLE,
        created_at="2026-07-10T12:00:00+08:00",
        sources=sources,
        artifacts=(
            ArtifactBuildInput(
                source_dir=core,
                kind=ArtifactKind.CORE,
                platform="windows",
                architecture="x64",
            ),
        ),
        web_bundle=WebBundleBuildInput(dist),
    )


def _build(tmp_path: Path, dist: Path) -> None:
    signer = Ed25519MemorySigner("web-security-key", Ed25519PrivateKey.generate())
    ReleaseBuilder(signer).build(_spec(tmp_path, dist), tmp_path / "release")


def test_web_bundle_rejects_unhashed_production_asset(tmp_path: Path) -> None:
    dist = _dist(tmp_path / "dist")
    hashed = next((dist / "assets").iterdir())
    unhashed = dist / "assets" / "app.js"
    hashed.rename(unhashed)
    index = (dist / "index.html").read_text(encoding="utf-8")
    (dist / "index.html").write_text(
        index.replace(hashed.name, unhashed.name), encoding="utf-8"
    )

    with pytest.raises(ReleaseBuildError, match="hash|SHA-256|immutable"):
        _build(tmp_path, dist)


@pytest.mark.parametrize("extra_name", ["stale.js", ".env", "assets/.hidden.js"])
def test_web_bundle_rejects_extra_or_hidden_files(
    tmp_path: Path, extra_name: str
) -> None:
    dist = _dist(tmp_path / "dist")
    extra = dist / extra_name
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("stale", encoding="utf-8")

    with pytest.raises(ReleaseBuildError, match="extra|hidden|allowlist"):
        _build(tmp_path, dist)
    assert not (tmp_path / "release").exists()


def test_web_bundle_rejects_orphaned_but_hash_named_stale_asset(tmp_path: Path) -> None:
    dist = _dist(tmp_path / "dist")
    stale = b"console.log('stale');\n"
    relative = _asset_path(stale, stem="stale")
    (dist / relative).write_bytes(stale)

    with pytest.raises(ReleaseBuildError, match="extra|orphan|allowlist"):
        _build(tmp_path, dist)

    assert not (tmp_path / "release").exists()


def test_web_bundle_rejects_missing_hash_named_lazy_dependency(tmp_path: Path) -> None:
    dist = _dist(tmp_path / "dist")
    original = next((dist / "assets").glob("app.*.js"))
    content = b'import("./missing.deadbeef.js");\n'
    replacement = dist / _asset_path(content)
    original.unlink()
    replacement.write_bytes(content)
    index = (dist / "index.html").read_text(encoding="utf-8")
    (dist / "index.html").write_text(
        index.replace(original.name, replacement.name), encoding="utf-8"
    )

    with pytest.raises(ReleaseBuildError, match="missing|dependency|allowlist"):
        _build(tmp_path, dist)


def test_web_bundle_finds_lazy_asset_after_adjacent_minified_strings(
    tmp_path: Path,
) -> None:
    dist = _dist(tmp_path / "dist")
    original = next((dist / "assets").glob("app.*.js"))
    lazy = b"export const ready=true;\n"
    lazy_relative = _asset_path(lazy, stem="lazy")
    (dist / lazy_relative).write_bytes(lazy)
    entry = (
        b'const label="ordinary";const load=()=>import("./'
        + Path(lazy_relative).name.encode()
        + b'");export{load};\n'
    )
    replacement = dist / _asset_path(entry)
    original.unlink()
    replacement.write_bytes(entry)
    index = (dist / "index.html").read_text(encoding="utf-8")
    (dist / "index.html").write_text(
        index.replace(original.name, replacement.name), encoding="utf-8"
    )

    _build(tmp_path, dist)


@pytest.mark.parametrize(
    "index",
    [
        '<html><head><script src="/assets/missing.12345678.js"></script></head></html>',
        f"<html><head>{RUNTIME_MARKER}<script>alert(1)</script></head></html>",
        f"<html><head>{RUNTIME_MARKER}<style>body{{margin:0}}</style></head></html>",
        f'<html><head>{RUNTIME_MARKER}</head><body style="margin:0"></body></html>',
        f'<html><head>{RUNTIME_MARKER}</head><body onclick="alert(1)"></body></html>',
    ],
)
def test_web_bundle_rejects_missing_marker_and_csp_incompatible_inline_code(
    tmp_path: Path, index: str
) -> None:
    dist = _dist(tmp_path / "dist", index=index)

    with pytest.raises(ReleaseBuildError, match="runtime|inline|CSP|reference"):
        _build(tmp_path, dist)


def test_web_bundle_rejects_old_overlay_reference_even_when_sha_named(
    tmp_path: Path,
) -> None:
    dist = _dist(tmp_path / "dist")
    legacy = b"window.__legacyOverlay=true;\n"
    relative = _asset_path(legacy, stem="ecorex-v029-overlay")
    (dist / relative).write_bytes(legacy)
    original = next((dist / "assets").glob("app.*.js"))
    index = (
        f"<html><head>{RUNTIME_MARKER}"
        f'<script type="module" src="/assets/{original.name}"></script>'
        f'<script defer src="/{relative}"></script>'
        "</head></html>"
    )
    (dist / "index.html").write_text(index, encoding="utf-8")

    with pytest.raises(ReleaseBuildError, match="legacy|overlay|old bundle"):
        _build(tmp_path, dist)


def test_web_bundle_rejects_renamed_legacy_overlay_content(tmp_path: Path) -> None:
    dist = _dist(tmp_path / "dist")
    original = next((dist / "assets").glob("app.*.js"))
    legacy = b'document.body.className="ecorex-v029-modal";\n'
    replacement = dist / _asset_path(legacy, stem="app")
    original.unlink()
    replacement.write_bytes(legacy)
    index = (dist / "index.html").read_text(encoding="utf-8")
    (dist / "index.html").write_text(
        index.replace(original.name, replacement.name), encoding="utf-8"
    )

    with pytest.raises(ReleaseBuildError, match="legacy|overlay|old bundle"):
        _build(tmp_path, dist)


def test_web_bundle_rejects_symlink_without_publishing(tmp_path: Path) -> None:
    dist = _dist(tmp_path / "dist")
    link = dist / "assets" / "linked.12345678.js"
    try:
        link.symlink_to(tmp_path / "outside.js")
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    with pytest.raises(ReleaseBuildError, match="link|reparse"):
        _build(tmp_path, dist)
    assert not (tmp_path / "release").exists()


def test_web_bundle_reparse_flag_is_detected_without_windows_privilege() -> None:
    from types import SimpleNamespace

    from ecorex.release.web_bundle import _metadata_is_link_or_reparse

    metadata = SimpleNamespace(
        st_mode=0o100644,
        st_file_attributes=getattr(os.stat_result, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
    )
    assert _metadata_is_link_or_reparse(metadata)
