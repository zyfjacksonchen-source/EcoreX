from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex.release import (
    Ed25519MemorySigner,
    ReleaseBuilder,
    ReleaseBuildSpec,
    WebBundleBuildInput,
)
from ecorex.server import WebBundleManifest
from ecorex.server.bundle import RUNTIME_CONFIG_MARKER, load_verified_web_bundle
from ecorex.update import ReleaseChannel, ReleaseSource, SourceKind


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_ROOT = REPOSITORY_ROOT / "desktop"


def _command(name: str) -> str:
    resolved = shutil.which(name)
    if resolved is None and os.name == "nt":
        resolved = shutil.which(f"{name}.cmd")
    assert resolved is not None, f"{name} is required by the production Web release gate"
    return resolved


def _run(*command: str) -> None:
    completed = subprocess.run(
        command,
        cwd=DESKTOP_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env={**os.environ, "NO_COLOR": "1"},
    )
    assert completed.returncode == 0, (
        f"production Web command failed: {command!r}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def _tree_digest(root: Path) -> tuple[tuple[str, str], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file())
    )


def _sources() -> tuple[ReleaseSource, ...]:
    return (
        ReleaseSource(
            "mirror",
            SourceKind.GITHUB_CN_MIRROR,
            0,
            "https://mirror.example/releases",
        ),
        ReleaseSource(
            "github",
            SourceKind.GITHUB_RELEASE,
            1,
            "https://github.example/releases",
        ),
        ReleaseSource(
            "cdn",
            SourceKind.ECOREX_CDN,
            2,
            "https://cdn.example/releases",
        ),
    )


def test_real_vite_dist_is_content_addressed_signed_and_server_loadable(
    tmp_path: Path,
) -> None:
    npm = _command("npm")
    node = _command("node")
    _run(npm, "run", "build")

    production_dist = DESKTOP_ROOT / "dist"
    dist = tmp_path / "web"
    shutil.copytree(production_dist, dist)
    first_tree = _tree_digest(dist)
    _run(node, "tools/rehash-dist.mjs", str(dist))
    assert _tree_digest(dist) == first_tree

    index = (dist / "index.html").read_text(encoding="utf-8")
    assert index.count(RUNTIME_CONFIG_MARKER) == 1
    assert "<style" not in index.casefold()
    assert "webui-overlay" not in index.casefold()
    assets = tuple(path for path in (dist / "assets").rglob("*") if path.is_file())
    assert assets
    for asset in assets:
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        assert digest[:16] in asset.name.casefold()

    private_key = Ed25519PrivateKey.generate()
    signer = Ed25519MemorySigner("real-web-release-key", private_key)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    result = ReleaseBuilder(signer).build(
        ReleaseBuildSpec(
            channel=ReleaseChannel.CANARY,
            created_at="2026-07-10T12:00:00+08:00",
            sources=_sources(),
            artifacts=(),
            web_bundle=WebBundleBuildInput(dist),
        ),
        tmp_path / "release",
    )
    web_manifest_path = result.artifact_paths["web-manifest"]
    web_manifest = WebBundleManifest.from_json(web_manifest_path.read_bytes())
    loaded = load_verified_web_bundle(
        web_root=dist,
        release_manifest_path=result.manifest_path,
        web_manifest_path=web_manifest_path,
        trusted_public_keys={"real-web-release-key": public_key},
    )

    assert loaded.web_manifest == web_manifest
    assert set(loaded.files) == {"index.html", *(path.relative_to(dist).as_posix() for path in assets)}
    assert loaded.index_template.count(RUNTIME_CONFIG_MARKER) == 1
