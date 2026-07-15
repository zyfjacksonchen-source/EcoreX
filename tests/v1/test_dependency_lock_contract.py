from __future__ import annotations

import importlib.util
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


def _dependency_gate_module():
    spec = importlib.util.spec_from_file_location(
        "ecorex_v1_dependency_lock_gate",
        ROOT / "scripts" / "check-v1-dependency-locks.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    assert report["github_actions"] == 6
    assert report["minimum_actions_runner_version"] == "2.327.1"
    assert len(report["github_actions_lock_sha256"]) == 64


def test_workflow_actions_are_exact_reviewed_node24_revisions() -> None:
    gate = _dependency_gate_module()

    actions, lock = gate._load_action_lock(ROOT)

    assert lock["minimum_runner_version"] == "2.327.1"
    assert actions == {
        "actions/checkout": (
            "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
            "v7.0.0",
        ),
        "actions/download-artifact": (
            "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
            "v8.0.1",
        ),
        "actions/setup-go": (
            "4a3601121dd01d1626a1e23e37211e3254c1c06c",
            "v6.4.0",
        ),
        "actions/setup-node": (
            "820762786026740c76f36085b0efc47a31fe5020",
            "v7.0.0",
        ),
        "actions/setup-python": (
            "ece7cb06caefa5fff74198d8649806c4678c61a1",
            "v6.3.0",
        ),
        "actions/upload-artifact": (
            "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            "v7.0.1",
        ),
    }


def _workflow_fixture(tmp_path: Path) -> Path:
    copied = tmp_path / "repo"
    shutil.copytree(ROOT / ".github", copied / ".github")
    bootstrap = copied / "platform-staging" / "bootstrap"
    bootstrap.mkdir(parents=True)
    shutil.copy2(ROOT / "platform-staging" / "bootstrap" / "go.mod", bootstrap)
    locks = copied / "requirements" / "locks"
    locks.mkdir(parents=True)
    shutil.copy2(LOCK_ROOT / "github-actions.json", locks)
    return copied


def test_action_lock_rejects_unverified_or_noncanonical_revision(tmp_path: Path) -> None:
    gate = _dependency_gate_module()
    copied = _workflow_fixture(tmp_path)
    lock_path = copied / gate.ACTION_LOCK_RELATIVE
    value = json.loads(lock_path.read_text(encoding="utf-8"))
    value["actions"][0]["verification"] = "unknown"
    lock_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ValueError, match="github_actions_lock_entry_invalid"):
        gate._load_action_lock(copied)


def test_workflow_gate_rejects_unreviewed_inventory_and_checkout_credentials(
    tmp_path: Path,
) -> None:
    gate = _dependency_gate_module()
    copied = _workflow_fixture(tmp_path)
    workflows = copied / ".github" / "workflows"
    unexpected = workflows / "legacy-publisher.yml"
    unexpected.write_text("name: legacy\n", encoding="utf-8")

    with pytest.raises(ValueError, match="workflow_inventory_invalid:unexpected"):
        gate._validate_workflows(copied)

    unexpected.unlink()
    ci = workflows / "ecorex-v1-ci.yml"
    source = ci.read_text(encoding="utf-8")
    source = source.replace("          persist-credentials: false\n", "", 1)
    ci.write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match="workflow_checkout_persists_credentials"):
        gate._validate_workflows(copied)


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
