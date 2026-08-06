from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from ecorex.pack_catalog import REQUIRED_CAPABILITY_PACK_IDS
from ecorex.release import (
    ArtifactBuildInput,
    ArtifactKind,
    Ed25519MemorySigner,
    ReleaseBuildSpec,
    ReleaseBuilder,
    WebBundleBuildInput,
)
from ecorex.release.candidate import candidate_receipt_signing_payload, scan_stage_tree
from ecorex.release.ci_reproducibility import (
    CI_WORKFLOW_PATH,
    CiReproducibilityError,
    EXPECTED_TARGETS,
    _read_stable_regular_file,
    bind_to_candidate,
    build_artifact_selection,
    build_source_evidence,
    canonical_json_bytes,
    read_contracts,
    validate_run_metadata,
)
from ecorex.update import ReleaseChannel, ReleaseSource, SourceKind


ROOT = Path(__file__).resolve().parents[2]
COMMIT = "a" * 40
RUN_ID = 7812345
RUN_ATTEMPT = 2
NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def _metadata(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "head_sha": COMMIT,
        "head_branch": "main",
        "path": CI_WORKFLOW_PATH,
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "repository": {
            "id": 101,
            "full_name": "EcoreX/EcoreX",
            "default_branch": "main",
            "fork": False,
        },
        "head_repository": {
            "id": 101,
            "full_name": "EcoreX/EcoreX",
            "fork": False,
        },
        "pull_requests": [],
        "head_commit": {"id": COMMIT},
        "created_at": "2026-07-12T11:30:00Z",
        "run_started_at": "2026-07-12T11:31:00Z",
        "updated_at": "2026-07-12T11:50:00Z",
    }
    value.update(changes)
    return value


def _validate(value: dict[str, object], **expected: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "repository": "EcoreX/EcoreX",
        "commit_sha": COMMIT,
        "workflow_run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "protected_ref": "refs/heads/main",
        "now": NOW,
        "maximum_age": timedelta(hours=24),
    }
    arguments.update(expected)
    return validate_run_metadata(value, **arguments)  # type: ignore[arg-type]


def _contract() -> bytes:
    value = {
        "document_type": "ecorex.v1-byte-contract",
        "files": [
            {
                "kind": "web-entry",
                "path": "desktop/dist/index.html",
                "sha256": "1" * 64,
                "size_bytes": 101,
            },
            {
                "kind": "web-content-addressed-asset",
                "path": "desktop/dist/assets/index.2222222222222222.js",
                "sha256": "2" * 64,
                "size_bytes": 202,
            },
        ],
        "schema_version": 1,
    }
    return canonical_json_bytes(value)


def _contracts(root: Path, *, payload: bytes | None = None) -> Path:
    root.mkdir()
    for target in EXPECTED_TARGETS:
        directory = root / f"ecorex-v1-byte-{target}"
        directory.mkdir()
        (directory / "byte-contract.json").write_bytes(payload or _contract())
    return root


def _artifact_metadata(**changes: object) -> dict[str, object]:
    artifacts: list[dict[str, object]] = []
    for index, target in enumerate(EXPECTED_TARGETS, start=1):
        artifacts.append(
            {
                "id": 9000 + index,
                "name": f"ecorex-v1-byte-{target}",
                "size_in_bytes": 4096 + index,
                "digest": "sha256:" + format(index, "064x"),
                "expired": False,
                "created_at": f"2026-07-12T11:{31 + index:02d}:00Z",
                "updated_at": f"2026-07-12T11:{32 + index:02d}:00Z",
                "expires_at": "2026-07-19T11:50:00Z",
                "workflow_run": {
                    "id": RUN_ID,
                    "repository_id": 101,
                    "head_repository_id": 101,
                    "head_branch": "main",
                    "head_sha": COMMIT,
                },
            }
        )
    value: dict[str, object] = {
        "total_count": len(artifacts),
        "artifacts": artifacts,
    }
    value.update(changes)
    return value


def _source_evidence(tmp_path: Path) -> dict[str, object]:
    metadata = tmp_path / "run.json"
    metadata.write_bytes(canonical_json_bytes(_metadata()))
    artifact_metadata = tmp_path / "artifacts.json"
    artifact_metadata.write_bytes(canonical_json_bytes(_artifact_metadata()))
    return build_source_evidence(
        repository_root=ROOT,
        run_metadata_path=metadata,
        artifact_metadata_path=artifact_metadata,
        contracts_root=_contracts(tmp_path / "contracts"),
        repository="EcoreX/EcoreX",
        commit_sha=COMMIT,
        workflow_run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
        protected_ref="refs/heads/main",
        now=NOW,
        maximum_age=timedelta(hours=24),
    )


def test_source_evidence_binds_exact_run_and_four_runner_contracts(
    tmp_path: Path,
) -> None:
    evidence = _source_evidence(tmp_path)

    assert evidence["commit_sha"] == COMMIT
    assert evidence["ci_run"] == {
        "repository": "EcoreX/EcoreX",
        "repository_id": 101,
        "head_repository_id": 101,
        "workflow_path": CI_WORKFLOW_PATH,
        "workflow_run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "event": "push",
        "protected_ref": "refs/heads/main",
        "run_started_at": "2026-07-12T11:31:00Z",
        "completed_at": "2026-07-12T11:50:00Z",
    }
    assert tuple(evidence["byte_contract_sha256"]) == EXPECTED_TARGETS
    assert tuple(evidence["artifacts"]) == EXPECTED_TARGETS
    assert len(set(evidence["byte_contract_sha256"].values())) == 1
    assert len(str(evidence["canonical_web_bundle_sha256"])) == 64


@pytest.mark.parametrize(
    ("field", "changed", "expected"),
    [
        ("head_sha", "b" * 40, {}),
        ("id", RUN_ID + 1, {}),
        ("run_attempt", RUN_ATTEMPT + 1, {}),
        ("head_sha", COMMIT, {"commit_sha": "b" * 40}),
        ("id", RUN_ID, {"workflow_run_id": RUN_ID + 1}),
        ("run_attempt", RUN_ATTEMPT, {"run_attempt": RUN_ATTEMPT + 1}),
    ],
)
def test_run_metadata_rejects_different_commit_run_or_attempt(
    field: str, changed: object, expected: dict[str, object]
) -> None:
    value = _metadata(**{field: changed})
    if field == "head_sha" and changed != COMMIT:
        value["head_commit"] = {"id": changed}
    with pytest.raises(CiReproducibilityError, match="ci_run_identity_untrusted"):
        _validate(value, **expected)


@pytest.mark.parametrize(
    "changes",
    [
        {"event": "pull_request", "pull_requests": [{"number": 12}]},
        {"event": "pull_request_target", "pull_requests": []},
        {
            "head_repository": {"full_name": "attacker/EcoreX", "fork": True},
        },
        {
            "repository": {
                "full_name": "EcoreX/EcoreX",
                "default_branch": "main",
                "fork": True,
            },
        },
    ],
)
def test_run_metadata_rejects_pr_and_fork_sources(changes: dict[str, object]) -> None:
    with pytest.raises(CiReproducibilityError, match="ci_run_identity_untrusted"):
        _validate(_metadata(**changes))


def test_run_metadata_rejects_wrong_workflow_branch_failure_and_stale_run() -> None:
    for value in (
        _metadata(path=".github/workflows/other.yml"),
        _metadata(head_branch="feature"),
        _metadata(conclusion="failure"),
    ):
        with pytest.raises(CiReproducibilityError, match="ci_run_identity_untrusted"):
            _validate(value)
    with pytest.raises(CiReproducibilityError, match="ci_run_stale_or_future"):
        _validate(
            _metadata(
                created_at="2026-07-10T11:00:00Z",
                run_started_at="2026-07-10T11:01:00Z",
                updated_at="2026-07-10T11:30:00Z",
            )
        )


def test_run_metadata_accepts_only_canonical_or_main_qualified_workflow_path() -> None:
    qualified = _validate(_metadata(path=f"{CI_WORKFLOW_PATH}@main"))
    assert qualified["workflow_path"] == CI_WORKFLOW_PATH

    for path in (f"{CI_WORKFLOW_PATH}@feature", ".github/workflows/other.yml@main"):
        with pytest.raises(CiReproducibilityError, match="identity_untrusted"):
            _validate(_metadata(path=path))


def test_run_metadata_accepts_api_omitted_default_branch_only() -> None:
    repository = dict(_metadata()["repository"])
    repository["default_branch"] = None
    assert _validate(_metadata(repository=repository))["protected_ref"] == "refs/heads/main"

    repository["default_branch"] = "feature"
    with pytest.raises(CiReproducibilityError, match="identity_untrusted"):
        _validate(_metadata(repository=repository))


def test_artifact_selection_binds_ids_to_the_current_attempt(tmp_path: Path) -> None:
    run_metadata = tmp_path / "run.json"
    run_metadata.write_bytes(canonical_json_bytes(_metadata()))
    artifact_metadata = tmp_path / "artifacts.json"
    artifact_metadata.write_bytes(canonical_json_bytes(_artifact_metadata()))
    selection = build_artifact_selection(
        run_metadata_path=run_metadata,
        artifact_metadata_path=artifact_metadata,
        repository="EcoreX/EcoreX",
        commit_sha=COMMIT,
        workflow_run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
        protected_ref="refs/heads/main",
        now=NOW,
        maximum_age=timedelta(hours=24),
    )

    assert selection["ci_run"]["run_attempt"] == RUN_ATTEMPT
    assert [
        selection["artifacts"][target]["artifact_id"]
        for target in EXPECTED_TARGETS
    ] == [9001, 9002, 9003, 9004]
    assert len(str(selection["artifact_metadata_sha256"])) == 64


def test_artifact_selection_rejects_old_attempt_and_foreign_run(
    tmp_path: Path,
) -> None:
    run_metadata = tmp_path / "run.json"
    run_metadata.write_bytes(canonical_json_bytes(_metadata()))
    previous_attempt = _artifact_metadata()
    previous_attempt["artifacts"][0]["created_at"] = "2026-07-12T11:20:00Z"
    previous_path = tmp_path / "previous.json"
    previous_path.write_bytes(canonical_json_bytes(previous_attempt))
    with pytest.raises(CiReproducibilityError, match="attempt_untrusted"):
        build_artifact_selection(
            run_metadata_path=run_metadata,
            artifact_metadata_path=previous_path,
            repository="EcoreX/EcoreX",
            commit_sha=COMMIT,
            workflow_run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            protected_ref="refs/heads/main",
            now=NOW,
            maximum_age=timedelta(hours=24),
        )

    foreign = _artifact_metadata()
    foreign["artifacts"][0]["workflow_run"]["id"] = RUN_ID + 1
    foreign_path = tmp_path / "foreign.json"
    foreign_path.write_bytes(canonical_json_bytes(foreign))
    with pytest.raises(CiReproducibilityError, match="metadata_invalid"):
        build_artifact_selection(
            run_metadata_path=run_metadata,
            artifact_metadata_path=foreign_path,
            repository="EcoreX/EcoreX",
            commit_sha=COMMIT,
            workflow_run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            protected_ref="refs/heads/main",
            now=NOW,
            maximum_age=timedelta(hours=24),
        )


def test_contract_root_rejects_missing_duplicate_and_extra_inputs(tmp_path: Path) -> None:
    missing = _contracts(tmp_path / "missing")
    target = missing / "ecorex-v1-byte-macos-x64"
    (target / "byte-contract.json").unlink()
    target.rmdir()
    with pytest.raises(CiReproducibilityError, match="target_set"):
        read_contracts(missing)

    duplicate = _contracts(tmp_path / "duplicate")
    (duplicate / "ecorex-v1-byte-ubuntu-x64" / "copy.json").write_bytes(_contract())
    with pytest.raises(CiReproducibilityError, match="contents"):
        read_contracts(duplicate)

    extra = _contracts(tmp_path / "extra")
    (extra / "untrusted.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CiReproducibilityError, match="target_set"):
        read_contracts(extra)


def test_contract_reader_rejects_hardlinks_symlinks_and_toctou(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original.json"
    original.write_bytes(_contract())
    hardlink = tmp_path / "hardlink.json"
    os.link(original, hardlink)
    with pytest.raises(CiReproducibilityError, match="ci_contract_invalid"):
        _read_stable_regular_file(
            hardlink, maximum=1024 * 1024, code="ci_contract_invalid"
        )

    symlink = tmp_path / "symlink.json"
    try:
        symlink.symlink_to(original)
    except OSError:
        pass
    else:
        with pytest.raises(CiReproducibilityError, match="ci_contract_invalid"):
            _read_stable_regular_file(
                symlink, maximum=1024 * 1024, code="ci_contract_invalid"
            )

    race = tmp_path / "race.json"
    race.write_bytes(_contract())
    real_read = os.read
    changed = False

    def racing_read(descriptor: int, maximum: int) -> bytes:
        nonlocal changed
        if not changed:
            changed = True
            metadata = race.stat()
            os.utime(
                race,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 10_000_000),
            )
        return real_read(descriptor, maximum)

    monkeypatch.setattr("ecorex.release.ci_reproducibility.os.read", racing_read)
    with pytest.raises(CiReproducibilityError, match="ci_input_changed"):
        _read_stable_regular_file(
            race, maximum=1024 * 1024, code="ci_contract_invalid"
        )


def test_contract_comparison_rejects_one_runner_with_different_bytes(
    tmp_path: Path,
) -> None:
    contracts = _contracts(tmp_path / "contracts")
    changed = json.loads(_contract())
    changed["files"][1]["size_bytes"] += 1
    (
        contracts
        / "ecorex-v1-byte-windows-x64"
        / "byte-contract.json"
    ).write_bytes(canonical_json_bytes(changed))
    metadata = tmp_path / "run.json"
    metadata.write_bytes(canonical_json_bytes(_metadata()))
    artifact_metadata = tmp_path / "artifacts.json"
    artifact_metadata.write_bytes(canonical_json_bytes(_artifact_metadata()))

    with pytest.raises(CiReproducibilityError, match="not_reproducible"):
        build_source_evidence(
            repository_root=ROOT,
            run_metadata_path=metadata,
            artifact_metadata_path=artifact_metadata,
            contracts_root=contracts,
            repository="EcoreX/EcoreX",
            commit_sha=COMMIT,
            workflow_run_id=RUN_ID,
            run_attempt=RUN_ATTEMPT,
            protected_ref="refs/heads/main",
            now=NOW,
            maximum_age=timedelta(hours=24),
        )


def _signed_release_and_candidate(
    tmp_path: Path, evidence: dict[str, object]
) -> tuple[Path, Path, Path, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    signer = Ed25519MemorySigner("release-test-key", private)
    source = tmp_path / "core"
    source.mkdir()
    (source / "runtime.txt").write_text("runtime\n", encoding="utf-8", newline="\n")
    web = tmp_path / "web"
    assets = web / "assets"
    assets.mkdir(parents=True)
    asset_payload = b"export {};\n"
    asset_name = f"app.{hashlib.sha256(asset_payload).hexdigest()[:16]}.js"
    (assets / asset_name).write_bytes(asset_payload)
    (web / "index.html").write_text(
        "<!doctype html><html><head><!--__ECOREX_RUNTIME_CONFIG__--></head>"
        "<body><main>EcoreX</main>"
        f'<script type="module" src="/assets/{asset_name}"></script>'
        "</body></html>\n",
        encoding="utf-8",
        newline="\n",
    )
    web_tree = scan_stage_tree(web).digest
    evidence["canonical_web_bundle_sha256"] = web_tree
    built = ReleaseBuilder(signer).build(
        ReleaseBuildSpec(
            channel=ReleaseChannel.CANARY,
            created_at="2026-07-12T12:00:00Z",
            sources=(
                ReleaseSource(
                    "github-cn",
                    SourceKind.GITHUB_CN_MIRROR,
                    0,
                    "https://mirror.example/ecorex/canary",
                ),
                ReleaseSource(
                    "github",
                    SourceKind.GITHUB_RELEASE,
                    1,
                    "https://github.com/EcoreX/EcoreX/releases/download/canary",
                ),
                ReleaseSource(
                    "cdn",
                    SourceKind.ECOREX_CDN,
                    2,
                    "https://cdn.example/ecorex/canary",
                ),
            ),
            artifacts=(
                ArtifactBuildInput(
                    source_dir=source,
                    kind=ArtifactKind.CORE,
                    platform="windows",
                    architecture="x64",
                ),
            ),
            web_bundle=WebBundleBuildInput(web),
        ),
        tmp_path / "release",
    )
    manifest_payload = built.manifest_path.read_bytes()
    staging = tmp_path / "staging-provenance.json"
    staging.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "status": "passed",
                "workflow_path": ".github/workflows/ecorex-v1-platform-stage.yml",
                "workflow_run_id": 9001,
                "run_attempt": 1,
                "commit_sha": COMMIT,
                "repository": "EcoreX/EcoreX",
                "metadata_sha256": "3" * 64,
            }
        )
    )
    candidate: dict[str, object] = {
        "schema_version": 2,
        "receipt_type": "ecorex-candidate-build",
        "status": "passed",
        "code": None,
        "commit_sha": COMMIT,
        "staging_provenance": {
            "workflow_path": ".github/workflows/ecorex-v1-platform-stage.yml",
            "workflow_run_id": 9001,
            "run_attempt": 1,
            "receipt_sha256": hashlib.sha256(staging.read_bytes()).hexdigest(),
        },
        "release_id": built.manifest.release_id,
        "version": built.manifest.version,
        "channel": built.manifest.channel.value,
        "build_digest": built.manifest.build_digest,
        "python_dependency_lock_sha256": "4" * 64,
        "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "web_tree_sha256": web_tree,
        "stage_receipts": {
            f"{kind}-{platform}-{architecture}": hashlib.sha256(
                f"{kind}-{platform}-{architecture}".encode()
            ).hexdigest()
            for platform, architecture in (
                ("windows", "x64"),
                ("macos", "arm64"),
                ("macos", "x64"),
            )
            for kind in ("core", "bootstrap", *REQUIRED_CAPABILITY_PACK_IDS)
        },
        "artifacts": {
            artifact.artifact_id: {
                "file_name": artifact.file_name,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            }
            for artifact in sorted(
                built.manifest.artifacts, key=lambda item: item.artifact_id
            )
        },
        "signing": {
            "algorithm": "ed25519",
            "key_id": "release-test-key",
            "operation_count": 1,
            "executable_sha256": "5" * 64,
            "adapter_sha256": None,
        },
    }
    candidate["signature"] = {
        "algorithm": "ed25519",
        "key_id": "release-test-key",
        "value": base64.b64encode(
            signer.sign(candidate_receipt_signing_payload(candidate))
        ).decode("ascii"),
    }
    receipt = tmp_path / "candidate.json"
    receipt.write_bytes(canonical_json_bytes(candidate))
    return (
        built.manifest_path,
        receipt,
        staging,
        base64.b64encode(public).decode("ascii"),
    )


def test_release_binder_requires_signed_candidate_manifest_and_equal_web_tree(
    tmp_path: Path,
) -> None:
    evidence = _source_evidence(tmp_path)
    manifest, candidate, _staging, public = _signed_release_and_candidate(
        tmp_path, evidence
    )
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_bytes(canonical_json_bytes(evidence))

    bound = bind_to_candidate(
        evidence_path=evidence_path,
        candidate_receipt_path=candidate,
        release_manifest_path=manifest,
        trusted_public_key=public,
    )

    assert bound["commit_sha"] == COMMIT
    assert bound["canonical_web_bundle_sha256"] == bound["web_tree_sha256"]
    assert bound["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert bound["candidate_receipt_sha256"] == hashlib.sha256(
        candidate.read_bytes()
    ).hexdigest()

    tampered = json.loads(candidate.read_text(encoding="utf-8"))
    tampered["web_tree_sha256"] = "f" * 64
    tampered_path = tmp_path / "tampered-candidate.json"
    tampered_path.write_bytes(canonical_json_bytes(tampered))
    with pytest.raises(CiReproducibilityError, match="binding_invalid"):
        bind_to_candidate(
            evidence_path=evidence_path,
            candidate_receipt_path=tampered_path,
            release_manifest_path=manifest,
            trusted_public_key=public,
        )

    signature_tamper = json.loads(candidate.read_text(encoding="utf-8"))
    signature_tamper["signing"]["operation_count"] += 1
    signature_tamper_path = tmp_path / "signature-tamper.json"
    signature_tamper_path.write_bytes(canonical_json_bytes(signature_tamper))
    with pytest.raises(CiReproducibilityError, match="receipt_untrusted"):
        bind_to_candidate(
            evidence_path=evidence_path,
            candidate_receipt_path=signature_tamper_path,
            release_manifest_path=manifest,
            trusted_public_key=public,
        )


def test_gate_receipt_requires_release_bound_reproducibility(
    tmp_path: Path,
) -> None:
    evidence = _source_evidence(tmp_path)
    manifest, candidate, staging, public = _signed_release_and_candidate(
        tmp_path, evidence
    )
    source = tmp_path / "source-evidence.json"
    source.write_bytes(canonical_json_bytes(evidence))
    bound = bind_to_candidate(
        evidence_path=source,
        candidate_receipt_path=candidate,
        release_manifest_path=manifest,
        trusted_public_key=public,
    )
    bound_path = tmp_path / "release-bound.json"
    bound_path.write_bytes(canonical_json_bytes(bound))
    writer = ROOT / "scripts" / "write-v1-gate-receipts.py"
    common = [
        sys.executable,
        str(writer),
        "--gate",
        "reproducibility",
        "--manifest",
        str(manifest),
        "--candidate-receipt",
        str(candidate),
        "--trusted-public-key",
        public,
        "--staging-provenance",
        str(staging),
        "--expected-staging-run-id",
        "9001",
        "--commit-sha",
        COMMIT,
        "--workflow-run-id",
        "9002",
    ]

    direct = subprocess.run(
        [
            *common,
            "--evidence-file",
            str(source),
            "--output-dir",
            str(tmp_path / "direct"),
        ],
        capture_output=True,
        check=False,
    )
    assert direct.returncode == 1
    assert b"reproducibility_gate_set_invalid" in direct.stderr

    receipts = tmp_path / "receipts"
    accepted = subprocess.run(
        [
            *common,
            "--evidence-file",
            str(bound_path),
            "--source-evidence",
            str(source),
            "--output-dir",
            str(receipts),
        ],
        capture_output=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr.decode(errors="replace")
    receipt = json.loads((receipts / "reproducibility.json").read_text())
    assert receipt["evidence_type"] == "release-bound-reproducibility"
    assert receipt["release_id"] == bound["release_id"]
    assert receipt["build_digest"] == bound["build_digest"]

    for name, field in (
        ("candidate-hash", "candidate_receipt_sha256"),
        ("source-hash", "reproducibility_evidence_sha256"),
        ("web-execution", "canonical_web_bundle_sha256"),
    ):
        forged = json.loads(json.dumps(bound))
        forged[field] = "f" * 64
        forged_path = tmp_path / f"forged-{name}.json"
        forged_path.write_bytes(canonical_json_bytes(forged))
        forged_result = subprocess.run(
            [
                *common,
                "--evidence-file",
                str(forged_path),
                "--source-evidence",
                str(source),
                "--output-dir",
                str(tmp_path / f"forged-{name}"),
            ],
            capture_output=True,
            check=False,
        )
        assert forged_result.returncode == 1

    tampered_candidate = json.loads(candidate.read_text(encoding="utf-8"))
    tampered_candidate["signing"]["operation_count"] += 1
    tampered_candidate_path = tmp_path / "tampered-candidate-for-writer.json"
    tampered_candidate_path.write_bytes(canonical_json_bytes(tampered_candidate))
    tampered_authority = list(common)
    tampered_authority[
        tampered_authority.index("--candidate-receipt") + 1
    ] = str(tampered_candidate_path)
    rejected_candidate = subprocess.run(
        [
            *tampered_authority,
            "--evidence-file",
            str(bound_path),
            "--source-evidence",
            str(source),
            "--output-dir",
            str(tmp_path / "tampered-candidate-receipts"),
        ],
        capture_output=True,
        check=False,
    )
    assert rejected_candidate.returncode == 1
