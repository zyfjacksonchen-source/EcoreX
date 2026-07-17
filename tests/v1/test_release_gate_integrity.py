from __future__ import annotations

import importlib.util
import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ecorex.control_plane.repository import REQUIRED_RELEASE_GATES
from ecorex.pack_catalog import REQUIRED_CAPABILITY_PACK_IDS
from ecorex.release import candidate_receipt_signing_payload
from ecorex.release.candidate import STAGE_WORKFLOW_PATH, TARGETS
from ecorex.update import (
    ReleaseArtifact,
    ReleaseChannel,
    ReleaseManifest,
    ReleaseSource,
    SignatureEnvelope,
    SourceKind,
)


ROOT = Path(__file__).resolve().parents[2]
COMMIT = "a" * 40
RUN_ID = 4815162342


def _module(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _json(path: Path, value: object) -> Path:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def test_release_gate_set_requires_real_image_storage_soak_and_reproducibility() -> None:
    assert {
        "image-shared-storage",
        "image-soak",
        "reproducibility",
    } <= REQUIRED_RELEASE_GATES


def test_candidate_workflow_executes_instead_of_auto_declaring_runtime_gates() -> None:
    workflow = (ROOT / ".github/workflows/ecorex-v1-candidate.yml").read_text(
        encoding="utf-8"
    )

    assert "f0750d247bfe52ffb95c137cadc9983a03010690" in workflow
    assert "check-v1-release-baseline.py" in workflow
    assert "fetch-depth: 0" in workflow
    assert "--reporter=json" in workflow
    assert (
        "PLAYWRIGHT_JSON_OUTPUT_FILE: "
        "../.candidate/quality/playwright-report.json"
    ) in workflow
    assert "> ../.candidate/quality/playwright-report.json" not in workflow
    assert "--full-pytest-junit" in workflow
    assert "--migration-pytest-junit" in workflow
    assert "--gate integration --gate e2e" not in workflow
    assert "test_migration_copy_on_write.py" in workflow
    assert "test_product_legacy_migration.py" in workflow
    assert "test_migration_quarantine.py" in workflow
    assert "test_v030_release_schema_archive.py" in workflow
    assert "test_candidate_storage_migrations.py" in workflow
    assert "test_update_activation_health.py" in workflow
    assert "--node-id candidate-shared-a" in workflow
    assert "--node-id candidate-shared-b" in workflow
    assert "--node-id candidate-soak-a" in workflow
    assert "--node-id candidate-soak-b" in workflow
    assert "--minimum-duration-seconds 14400" in workflow
    assert "name: Protected four-hour PostgreSQL and MinIO image soak" in workflow
    assert "runs-on: ubuntu-24.04" in workflow
    assert "bind-v1-release-gate-evidence.py" in workflow
    assert "--output .candidate/output/migration-release-bound.json" in workflow
    assert "--evidence-file .candidate/output/migration-release-bound.json" in workflow
    assert "ci_run_id:" in workflow
    assert "ci_run_attempt:" in workflow
    assert "scripts/verify-v1-ci-provenance.py" in workflow
    assert "scripts/select-v1-ci-reproducibility-artifacts.py" in workflow
    assert "/attempts/${CI_RUN_ATTEMPT}" in workflow
    assert "run-id: ${{ inputs.ci_run_id }}" in workflow
    assert "artifact-ids: ${{ steps.ci-artifacts.outputs.artifact_ids }}" in workflow
    assert "--artifact-metadata .candidate/ci/artifacts.json" in workflow
    assert "- reproducibility-input" in workflow
    assert "scripts/bind-v1-reproducibility-evidence.py" in workflow
    assert "--output .candidate/output/reproducibility-release-bound.json" in workflow
    assert "--gate reproducibility" in workflow
    assert (
        "--evidence-file .candidate/output/reproducibility-release-bound.json"
        in workflow
    )
    assert "continue-on-error" not in workflow


def test_ci_and_candidate_check_all_source_files_before_building() -> None:
    for name in ("ecorex-v1-ci.yml", "ecorex-v1-candidate.yml"):
        workflow = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        assert "git diff --check HEAD --" in workflow
        assert "python scripts/check-v1-source-tree.py" in workflow
        assert "run-v1-lint.py --compile" in workflow


def test_lint_inventory_covers_current_v1_surfaces_and_excludes_history() -> None:
    lint = _module("ecorex_v1_lint_inventory", "scripts/run-v1-lint.py")
    targets = {path.relative_to(ROOT).as_posix() for path in lint.v1_python_targets()}

    assert {"ecorex", "tests/v1", "platform-staging", "release/capability-packs"} <= targets
    assert "scripts/build-v1-candidate.py" in targets
    assert "scripts/write-v1-quality-evidence.py" in targets
    assert not any("v022" in value or "v024" in value for value in targets)


def _junit(path: Path, *, tests: int, modules: tuple[str, ...], skipped: int = 0) -> Path:
    cases = [
        f'<testcase classname="{module}" name="test_required_{index}">'
        + ("<skipped/>" if index < skipped else "")
        + "</testcase>"
        for index, module in enumerate(modules)
    ]
    cases.extend(
        f'<testcase classname="tests.v1.test_filler" name="test_{index}"/>'
        for index in range(len(cases), tests)
    )
    path.write_text(
        f'<testsuites><testsuite name="pytest" tests="{tests}" failures="0" '
        f'errors="0" skipped="{skipped}">' + "".join(cases) + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def _playwright(path: Path, required_titles: tuple[str, ...]) -> Path:
    specs = [
        {
            "title": (
                required_titles[index]
                if index < len(required_titles)
                else f"case-{index}"
            ),
            "ok": True,
            "tests": [{"results": [{"status": "passed"}]}],
        }
        for index in range(36)
    ]
    return _json(
        path,
        {
            "config": {},
            "errors": [],
            "stats": {
                "expected": 36,
                "skipped": 0,
                "unexpected": 0,
                "flaky": 0,
                "duration": 1000,
            },
            "suites": [{"title": "ga", "suites": [], "specs": specs}],
        },
    )


def _quality_args(tmp_path: Path) -> tuple[object, list[str], Path]:
    writer = _module("ecorex_v1_quality_writer", "scripts/write-v1-quality-evidence.py")
    dependencies = {
        name: _json(tmp_path / f"{name}.json", {"status": "passed", "name": name})
        for name in ("byte", "supply", "baseline")
    }
    full_modules = tuple(writer._FULL_SENTINELS)
    migration_modules = tuple(writer._MIGRATION_CORPUS)
    full = _junit(tmp_path / "full.xml", tests=1000, modules=full_modules)
    migration = _junit(
        tmp_path / "migration.xml", tests=len(migration_modules), modules=migration_modules
    )
    browser = _playwright(
        tmp_path / "playwright.json",
        tuple(writer._BROWSER_SENTINELS),
    )
    output = tmp_path / "quality.json"
    return (
        writer,
        [
            "--commit-sha",
            COMMIT,
            "--workflow-run-id",
            str(RUN_ID),
            "--byte-contract",
            str(dependencies["byte"]),
            "--supply-chain",
            str(dependencies["supply"]),
            "--baseline-evidence",
            str(dependencies["baseline"]),
            "--full-pytest-junit",
            str(full),
            "--migration-pytest-junit",
            str(migration),
            "--playwright-json",
            str(browser),
            "--output",
            str(output),
        ],
        output,
    )


def test_quality_receipt_requires_machine_readable_non_skipped_execution(
    tmp_path: Path,
) -> None:
    writer, arguments, output = _quality_args(tmp_path)
    assert writer.run(arguments) == 0
    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["schema_version"] == 3
    assert value["executions"]["browser-e2e"]["tests"] == 36
    assert value["executions"]["full-pytest"]["tests"] == 1000
    assert value["executions"]["migration-pytest"]["skipped"] == 0

    skipped = _junit(
        tmp_path / "skipped.xml",
        tests=len(writer._MIGRATION_CORPUS),
        modules=tuple(writer._MIGRATION_CORPUS),
        skipped=len(writer._MIGRATION_CORPUS),
    )
    bad = list(arguments)
    bad[bad.index("--migration-pytest-junit") + 1] = str(skipped)
    bad[bad.index("--output") + 1] = str(tmp_path / "bad.json")
    assert writer.run(bad) == 1

    browser_path = Path(arguments[arguments.index("--playwright-json") + 1])
    browser_value = json.loads(browser_path.read_text(encoding="utf-8"))
    browser_value["suites"][0]["specs"][0]["title"] = "unrelated passing test"
    browser_path.write_text(json.dumps(browser_value), encoding="utf-8")
    missing_browser_corpus = list(arguments)
    missing_browser_corpus[missing_browser_corpus.index("--output") + 1] = str(
        tmp_path / "missing-browser-corpus.json"
    )
    assert writer.run(missing_browser_corpus) == 1


def _signed_candidate(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    placeholder = SignatureEnvelope("ed25519", "release-key", base64.b64encode(b"0" * 64).decode())
    artifact = ReleaseArtifact(
        "core-windows-x64", "windows", "x64", "core.zip", 1, hashlib.sha256(b"x").hexdigest(), placeholder
    )
    manifest = ReleaseManifest(
        1,
        "release-canary-" + "1" * 24,
        "1.0.0",
        "f" * 64,
        ReleaseChannel.CANARY,
        "2026-07-12T12:00:00+08:00",
        (
            ReleaseSource("mirror", SourceKind.GITHUB_CN_MIRROR, 0, "https://m.example/r"),
            ReleaseSource("github", SourceKind.GITHUB_RELEASE, 1, "https://g.example/r"),
            ReleaseSource("cdn", SourceKind.ECOREX_CDN, 2, "https://c.example/r"),
        ),
        (artifact,),
        placeholder,
    )
    manifest = replace(
        manifest,
        signature=SignatureEnvelope(
            "ed25519", "release-key", base64.b64encode(private.sign(manifest.canonical_payload())).decode()
        ),
    )
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    staging_value = {
        "schema_version": 1,
        "status": "passed",
        "workflow_path": STAGE_WORKFLOW_PATH,
        "workflow_run_id": RUN_ID,
        "run_attempt": 1,
        "commit_sha": COMMIT,
        "repository": "ecorex/ecorex",
        "metadata_sha256": "d" * 64,
    }
    staging = _json(tmp_path / "staging.json", staging_value)
    stage_receipts = {
        f"{kind}-{platform}-{architecture}": hashlib.sha256(
            f"{kind}-{platform}-{architecture}".encode()
        ).hexdigest()
        for platform, architecture in TARGETS
        for kind in ("core", "bootstrap", *REQUIRED_CAPABILITY_PACK_IDS)
    }
    candidate: dict[str, object] = {
        "schema_version": 2,
        "receipt_type": "ecorex-candidate-build",
        "status": "passed",
        "code": None,
        "commit_sha": COMMIT,
        "staging_provenance": {
            "workflow_path": STAGE_WORKFLOW_PATH,
            "workflow_run_id": RUN_ID,
            "run_attempt": 1,
            "receipt_sha256": hashlib.sha256(staging.read_bytes()).hexdigest(),
        },
        "release_id": manifest.release_id,
        "version": manifest.version,
        "channel": manifest.channel.value,
        "build_digest": manifest.build_digest,
        "python_dependency_lock_sha256": "a" * 64,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "web_tree_sha256": "b" * 64,
        "stage_receipts": stage_receipts,
        "artifacts": {
            artifact.artifact_id: {
                "file_name": artifact.file_name,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            }
        },
        "signing": {
            "algorithm": "ed25519",
            "key_id": "release-key",
            "operation_count": 1,
            "executable_sha256": "c" * 64,
            "adapter_sha256": None,
        },
    }
    candidate["signature"] = {
        "algorithm": "ed25519",
        "key_id": "release-key",
        "value": base64.b64encode(private.sign(candidate_receipt_signing_payload(candidate))).decode(),
    }
    return _json(tmp_path / "candidate.json", candidate), manifest_path, staging, base64.b64encode(public).decode()


def test_release_binding_requires_candidate_identity_windows_boundary_and_four_hour_soak(
    tmp_path: Path,
) -> None:
    binder = _module("ecorex_v1_gate_binder", "scripts/bind-v1-release-gate-evidence.py")
    candidate, manifest, staging, public = _signed_candidate(tmp_path)
    short_soak = _json(
        tmp_path / "short-soak.json",
        {
            "schema_version": 1,
            "evidence_type": "ecorex-image-shared-storage-execution",
            "gate_type": "soak",
            "status": "passed",
            "commit_sha": COMMIT,
            "workflow_run_id": RUN_ID,
            "node_ids": ["node-a", "node-b"],
            "jobs_per_round": 256,
            "workers_per_round": 48,
            "pytest_node_ids": [
                "tests/v1/test_image_orchestrator_real_shared_storage.py::"
                "test_real_postgres_s3_concurrency_idempotency_recovery_and_gc",
                "tests/v1/test_image_orchestrator_production_storage.py::"
                "test_real_postgres_image_schema_migrate_validate_and_drift_gate",
            ],
            "rounds_completed": 1,
            "duration_seconds": 60,
            "pytest_junit_sha256": ["e" * 64],
        },
    )
    common = [
        "--candidate-receipt", str(candidate),
        "--release-manifest", str(manifest),
        "--trusted-public-key", public,
        "--staging-provenance", str(staging),
        "--commit-sha", COMMIT,
        "--workflow-run-id", str(RUN_ID),
        "--expected-staging-run-id", str(RUN_ID),
    ]
    assert binder.run(
        [
            "--gate",
            "image-soak",
            "--source-evidence",
            str(short_soak),
            *common,
            "--output",
            str(tmp_path / "bound.json"),
        ]
    ) == 1

    source = json.loads(short_soak.read_text(encoding="utf-8"))
    source["duration_seconds"] = 14400
    long_soak = _json(tmp_path / "long-soak.json", source)
    output = tmp_path / "long-bound.json"
    assert binder.run(
        [
            "--gate",
            "image-soak",
            "--source-evidence",
            str(long_soak),
            *common,
            "--output",
            str(output),
        ]
    ) == 0
    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["release_id"] == "release-canary-" + "1" * 24
    assert value["build_digest"] == "f" * 64
    assert len(value["stage_receipts"]) == 24

    forged = json.loads(candidate.read_text())
    forged["web_tree_sha256"] = "9" * 64
    forged_path = _json(tmp_path / "forged.json", forged)
    assert binder.run(
        ["--gate", "image-soak", "--source-evidence", str(long_soak),
         *[str(forged_path) if item == str(candidate) else item for item in common],
         "--output", str(tmp_path / "forged-bound.json")]
    ) == 1

    nan_source = dict(source)
    nan_source["duration_seconds"] = float("nan")
    nan_path = _json(tmp_path / "nan.json", nan_source)
    assert binder.run(
        ["--gate", "image-soak", "--source-evidence", str(nan_path), *common,
         "--output", str(tmp_path / "nan-bound.json")]
    ) == 1


def test_migration_gate_cannot_be_minted_until_it_is_release_bound(
    tmp_path: Path,
) -> None:
    quality_writer, quality_args, quality = _quality_args(tmp_path)
    assert quality_writer.run(quality_args) == 0
    candidate, manifest, staging, public = _signed_candidate(tmp_path)
    binder = _module("ecorex_v1_migration_binder", "scripts/bind-v1-release-gate-evidence.py")
    bound = tmp_path / "migration-bound.json"
    assert binder.run(
        [
            "--gate", "migration-dry-run",
            "--source-evidence", str(quality),
            "--candidate-receipt", str(candidate),
            "--release-manifest", str(manifest),
            "--trusted-public-key", public,
            "--staging-provenance", str(staging),
            "--commit-sha", COMMIT,
            "--workflow-run-id", str(RUN_ID),
            "--expected-staging-run-id", str(RUN_ID),
            "--output", str(bound),
        ]
    ) == 0
    writer = _module("ecorex_v1_typed_gate_writer", "scripts/write-v1-gate-receipts.py")
    writer_authority = [
        "--candidate-receipt", str(candidate),
        "--trusted-public-key", public,
        "--staging-provenance", str(staging),
        "--expected-staging-run-id", str(RUN_ID),
    ]
    direct_dir = tmp_path / "direct"
    assert writer.run(
        [
            "--gate", "migration-dry-run",
            "--evidence-file", str(quality),
            "--manifest", str(manifest),
            *writer_authority,
            "--commit-sha", COMMIT,
            "--workflow-run-id", str(RUN_ID),
            "--output-dir", str(direct_dir),
        ]
    ) == 1
    receipt_dir = tmp_path / "receipts"
    assert writer.run(
        [
            "--gate", "migration-dry-run",
            "--evidence-file", str(bound),
            "--source-evidence", str(quality),
            "--manifest", str(manifest),
            *writer_authority,
            "--commit-sha", COMMIT,
            "--workflow-run-id", str(RUN_ID),
            "--output-dir", str(receipt_dir),
        ]
    ) == 0
    receipt = json.loads((receipt_dir / "migration-dry-run.json").read_text())
    manifest_value = json.loads(manifest.read_text())
    assert receipt["release_id"] == manifest_value["release_id"]
    assert receipt["build_digest"] == manifest_value["build_digest"]
    assert receipt["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()

    nonbound_dir = tmp_path / "nonbound"
    assert writer.run(
        [
            "--gate", "lint",
            "--evidence-file", str(quality),
            "--manifest", str(manifest),
            *writer_authority,
            "--commit-sha", COMMIT,
            "--workflow-run-id", str(RUN_ID),
            "--output-dir", str(nonbound_dir),
        ]
    ) == 0

    mixed_dir = tmp_path / "mixed-bound-gates"
    assert writer.run(
        [
            "--gate", "migration-dry-run",
            "--gate", "e2e",
            "--evidence-file", str(bound),
            "--source-evidence", str(quality),
            "--manifest", str(manifest),
            *writer_authority,
            "--commit-sha", COMMIT,
            "--workflow-run-id", str(RUN_ID),
            "--output-dir", str(mixed_dir),
        ]
    ) == 1

    reformatted_candidate = tmp_path / "reformatted-candidate.json"
    reformatted_candidate.write_text(
        json.dumps(json.loads(candidate.read_text(encoding="utf-8")), indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    reformatted_authority = list(writer_authority)
    reformatted_authority[
        reformatted_authority.index("--candidate-receipt") + 1
    ] = str(reformatted_candidate)
    assert writer.run(
        [
            "--gate", "migration-dry-run",
            "--evidence-file", str(bound),
            "--source-evidence", str(quality),
            "--manifest", str(manifest),
            *reformatted_authority,
            "--commit-sha", COMMIT,
            "--workflow-run-id", str(RUN_ID),
            "--output-dir", str(tmp_path / "reformatted-candidate-receipts"),
        ]
    ) == 1

    original_bound = json.loads(bound.read_text(encoding="utf-8"))
    for name, mutate in (
        (
            "candidate-hash",
            lambda value: value.__setitem__("candidate_receipt_sha256", "0" * 64),
        ),
        (
            "source-hash",
            lambda value: value.__setitem__("source_evidence_sha256", "0" * 64),
        ),
        (
            "execution",
            lambda value: value["execution"].__setitem__(
                "tests", value["execution"]["tests"] + 1
            ),
        ),
    ):
        forged = json.loads(json.dumps(original_bound))
        mutate(forged)
        forged_path = _json(tmp_path / f"raw-bound-{name}.json", forged)
        assert writer.run(
            [
                "--gate", "migration-dry-run",
                "--evidence-file", str(forged_path),
                "--source-evidence", str(quality),
                "--manifest", str(manifest),
                *writer_authority,
                "--commit-sha", COMMIT,
                "--workflow-run-id", str(RUN_ID),
                "--output-dir", str(tmp_path / f"raw-bound-{name}"),
            ]
        ) == 1

def test_source_tree_checker_detects_relevant_untracked_file(tmp_path: Path) -> None:
    checker = _module("ecorex_v1_source_checker", "scripts/check-v1-source-tree.py")
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    source = tmp_path / "ecorex" / "module.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="untracked"):
        checker.check(tmp_path)


@pytest.mark.parametrize(
    "payload",
    (
        b"first\r\nsecond\n",
        b"first\r\nsecond\r\n",
    ),
)
def test_source_tree_checker_rejects_non_lf_index_in_any_tracked_file(
    tmp_path: Path, payload: bytes,
) -> None:
    checker = _module("ecorex_v1_source_checker_eol", "scripts/check-v1-source-tree.py")
    (tmp_path / "ecorex").mkdir()
    (tmp_path / "ecorex" / "module.py").write_bytes(b"value = True\n")
    historical = tmp_path / "docs" / "historical.md"
    historical.parent.mkdir()
    historical.write_bytes(payload)
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    subprocess.run(
        ("git", "-c", "core.autocrlf=false", "add", "--", "."),
        cwd=tmp_path,
        check=True,
    )

    with pytest.raises(ValueError, match="index-eol:docs/historical.md"):
        checker.check(tmp_path)


def test_source_tree_checker_does_not_reject_binary_index_eol(tmp_path: Path) -> None:
    checker = _module(
        "ecorex_v1_source_checker_binary_eol", "scripts/check-v1-source-tree.py"
    )
    source = tmp_path / "ecorex" / "module.py"
    source.parent.mkdir()
    source.write_bytes(b"value = True\n")
    binary = tmp_path / "fixtures" / "payload.bin"
    binary.parent.mkdir()
    binary.write_bytes(b"\x00\r\n\xff")
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    subprocess.run(("git", "add", "--", "."), cwd=tmp_path, check=True)

    checker.check(tmp_path)


def test_source_tree_checker_cli_error_is_json_safe(monkeypatch, capsys) -> None:
    checker = _module("ecorex_v1_source_checker_json_safe", "scripts/check-v1-source-tree.py")

    def fail() -> None:
        raise ValueError('index-eol:docs/control\tline\n"quoted.md')

    monkeypatch.setattr(checker, "check", fail)
    assert checker.main() == 1
    output = capsys.readouterr().err
    parsed = json.loads(output)
    assert parsed["ok"] is False
    assert parsed["error"] == 'index-eol:docs/control\tline\n"quoted.md'


def test_source_tree_checker_includes_every_workflow_file(tmp_path: Path) -> None:
    checker = _module("ecorex_v1_source_checker_workflow", "scripts/check-v1-source-tree.py")
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    workflow = tmp_path / ".github" / "workflows" / "surprise.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: surprise\n", encoding="utf-8")

    assert workflow in checker.source_files(tmp_path)
    with pytest.raises(ValueError, match="untracked:.github/workflows/surprise.yaml"):
        checker.check(tmp_path)


def test_source_tree_checker_includes_github_actions_lock(tmp_path: Path) -> None:
    checker = _module("ecorex_v1_source_checker_action_lock", "scripts/check-v1-source-tree.py")
    action_lock = tmp_path / "requirements" / "locks" / "github-actions.json"
    action_lock.parent.mkdir(parents=True)
    action_lock.write_text("{}\n", encoding="utf-8")

    assert action_lock in checker.source_files(tmp_path)
