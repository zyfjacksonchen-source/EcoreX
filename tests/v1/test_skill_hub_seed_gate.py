from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def _validate(tmp_path: Path, *args: str):
    evidence = tmp_path / "seed-package-gate.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/validate_skill_hub_upstream_lock.py"),
            *args,
            "--evidence",
            str(evidence),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    return result, json.loads(evidence.read_text(encoding="utf-8"))


def test_fixed_skill_seed_resolutions_pass_without_cow_network(tmp_path):
    result, report = _validate(tmp_path)

    assert result.returncode == 0
    assert report["status"] == "ready"
    assert report["release_gate"] == "pass"
    assert report["candidate_count"] == 53
    assert report["native_alias_count"] == 5
    assert report["unsupported_count"] == 20
    assert report["verified_count"] == 28
    assert report["pending_count"] == 0
    assert {item["slug"] for item in report["native_aliases"]} == {
        "docx",
        "xlsx",
        "pptx",
        "pdf",
        "lark-cli",
    }
    assert {item["slug"] for item in report["unsupported"]}.isdisjoint(
        set(report["excluded_slugs"])
    )
    assert report["network_sync"] == "disabled"
    assert report["user_directories_modified"] is False
    assert {item["slug"] for item in report["verified"]}.isdisjoint(
        {item["slug"] for item in report["unsupported"]}
    )


def test_seed_lock_builder_is_deterministic_and_preserves_source_digests(tmp_path):
    output = tmp_path / "rebuilt-lock.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/build_skill_hub_seed_lock.py"),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    expected = json.loads(
        (ROOT / "docs/v0.3.0/skill-hub/cow-skill-hub.lock.json").read_text(
            encoding="utf-8"
        )
    )
    rebuilt = json.loads(output.read_text(encoding="utf-8"))
    assert rebuilt == expected
    assert rebuilt["catalog_snapshot"]["network_dependency"] is False
    assert all(
        len(item["source_package_sha256"]) == 64
        for item in rebuilt["seed_candidates"]
    )


def test_seed_gate_rejects_alias_target_or_unsupported_reason_tampering(tmp_path):
    source = json.loads(
        (ROOT / "docs/v0.3.0/skill-hub/cow-skill-hub.lock.json").read_text(
            encoding="utf-8"
        )
    )
    alias = next(
        item
        for item in source["seed_candidates"]
        if item["resolution"]["kind"] == "native_alias"
    )
    alias["resolution"]["native_extension_id"] = "skill.attacker"
    lock = tmp_path / "tampered-lock.json"
    lock.write_text(json.dumps(source), encoding="utf-8")

    result, report = _validate(tmp_path, "--lock", str(lock))
    assert result.returncode == 1
    assert report["status"] == "blocked"
    assert report["release_gate"] == "fail_closed"

    source = json.loads(
        (ROOT / "docs/v0.3.0/skill-hub/cow-skill-hub.lock.json").read_text(
            encoding="utf-8"
        )
    )
    refused = next(
        item
        for item in source["seed_candidates"]
        if item["resolution"]["kind"] == "unsupported"
    )
    refused["resolution"]["reason_code"] = "pretend_safe"
    lock.write_text(json.dumps(source), encoding="utf-8")
    result, report = _validate(tmp_path, "--lock", str(lock))
    assert result.returncode == 1
    assert report["release_gate"] == "fail_closed"


def test_seed_gate_rejects_source_canonical_and_version_tampering(tmp_path):
    source_root = ROOT / "docs/v0.3.0/skill-hub/source-packages"
    package_root = ROOT / "docs/v0.3.0/skill-hub/seed-packages"
    copied_sources = tmp_path / "source-packages"
    copied_packages = tmp_path / "seed-packages"
    shutil.copytree(source_root, copied_sources)
    shutil.copytree(package_root, copied_packages)

    source_file = next(copied_sources.glob("*.zip"))
    payload = bytearray(source_file.read_bytes())
    payload[-1] ^= 1
    source_file.write_bytes(payload)
    result, report = _validate(
        tmp_path,
        "--source-packages-root",
        str(copied_sources),
        "--packages-root",
        str(copied_packages),
    )
    assert result.returncode == 1
    assert report["release_gate"] == "fail_closed"

    shutil.rmtree(copied_sources)
    shutil.copytree(source_root, copied_sources)
    package_file = next(copied_packages.glob("*.zip"))
    payload = bytearray(package_file.read_bytes())
    payload[-1] ^= 1
    package_file.write_bytes(payload)
    result, report = _validate(
        tmp_path,
        "--source-packages-root",
        str(copied_sources),
        "--packages-root",
        str(copied_packages),
    )
    assert result.returncode == 1
    assert report["release_gate"] == "fail_closed"

    lock_value = json.loads(
        (ROOT / "docs/v0.3.0/skill-hub/cow-skill-hub.lock.json").read_text(
            encoding="utf-8"
        )
    )
    mirrored = next(
        item
        for item in lock_value["seed_candidates"]
        if item["resolution"]["kind"] == "mirrored"
    )
    mirrored["version"] = "99.0.0"
    lock = tmp_path / "version-tampered-lock.json"
    lock.write_text(json.dumps(lock_value), encoding="utf-8")
    result, report = _validate(tmp_path, "--lock", str(lock))
    assert result.returncode == 1
    assert report["release_gate"] == "fail_closed"


def test_seed_canonicalizer_is_deterministic_and_offline_after_source_capture(
    tmp_path,
):
    packages = tmp_path / "packages"
    audit = tmp_path / "audit.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/canonicalize_skill_hub_seed_packages.py"),
            "--packages-root",
            str(packages),
            "--audit",
            str(audit),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={
            **dict(os.environ),
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "",
        },
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(audit.read_text(encoding="utf-8"))
    assert report["network_dependency"] is False
    assert report["source_identity_verified_count"] == 53
    assert report["mirrored_count"] == 28
    lock = json.loads(
        (ROOT / "docs/v0.3.0/skill-hub/cow-skill-hub.lock.json").read_text(
            encoding="utf-8"
        )
    )
    for candidate in lock["seed_candidates"]:
        resolution = candidate["resolution"]
        if resolution["kind"] == "mirrored":
            rebuilt = packages / resolution["package_file"]
            assert rebuilt.stat().st_size == resolution["package_size_bytes"]
            assert hashlib.sha256(rebuilt.read_bytes()).hexdigest() == resolution[
                "package_sha256"
            ]
