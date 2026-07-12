from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from ecorex.release.dependency_lock import (
    DependencyLockError,
    REQUIRED_PROFILES,
    load_dependency_lock_manifest,
)
from ecorex.release.models import ReleaseBuildSpec, WebBundleBuildInput
from ecorex.update import ReleaseChannel, ReleaseSource, SourceKind


ROOT = Path(__file__).resolve().parents[2]
LOCK_ROOT = ROOT / "requirements" / "locks"


def test_repository_dependency_lock_set_is_content_addressed_and_complete() -> None:
    lock = load_dependency_lock_manifest(LOCK_ROOT / "manifest.json")

    assert frozenset(lock.profiles) == REQUIRED_PROFILES
    assert len(lock.sha256) == 64
    assert all(len(profile["lock_sha256"]) == 64 for profile in lock.profiles.values())


def test_dependency_lock_rejects_mutated_profile_bytes(tmp_path: Path) -> None:
    copied = tmp_path / "locks"
    shutil.copytree(LOCK_ROOT, copied)
    runtime = copied / "runtime.lock"
    runtime.write_bytes(runtime.read_bytes() + b"\n# mutation\n")

    with pytest.raises(DependencyLockError, match="dependency_lock_digest_mismatch"):
        load_dependency_lock_manifest(copied / "manifest.json")


def test_dependency_lock_gate_covers_python_npm_workflows_and_action_pins() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check-v1-dependency-locks.py")],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "passed"
    assert report["profiles"]["runtime"] >= 20
    assert report["npm_packages"] >= 1


def test_release_spec_rejects_non_digest_dependency_lock() -> None:
    source = ReleaseSource(
        source_id="mirror",
        kind=SourceKind.GITHUB_CN_MIRROR,
        base_url="https://mirror.example/releases",
        priority=0,
    )

    with pytest.raises(ValueError, match="dependency_lock_sha256"):
        ReleaseBuildSpec(
            channel=ReleaseChannel.CANARY,
            created_at="2026-07-11T00:00:00+00:00",
            sources=(source,),
            artifacts=(),
            web_bundle=WebBundleBuildInput(ROOT / "desktop" / "dist"),
            dependency_lock_sha256="not-a-digest",
        )
