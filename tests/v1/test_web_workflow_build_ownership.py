from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _job_body(workflow_name: str, job_name: str, next_job_name: str) -> str:
    source = (WORKFLOWS / workflow_name).read_text(encoding="utf-8")
    start_marker = f"  {job_name}:\n"
    end_marker = f"  {next_job_name}:\n"
    assert source.count(start_marker) == 1
    assert source.count(end_marker) == 1
    return source.split(start_marker, 1)[1].split(end_marker, 1)[0]


def _assert_single_workflow_owned_build_before_tests(job: str) -> None:
    commands = (
        "run: npm ci",
        "run: npm run typecheck",
        "run: npm run build",
        "npm run test:v1",
    )
    positions = [job.index(command) for command in commands]

    assert positions == sorted(positions)
    assert job.count("npm run build") == 1
    assert job.count("npm run test:v1") == 1
    assert job.count("npm ci") == 1
    assert "Every Web and Python contract" in job
    assert "must not rebuild desktop/dist" in job


def _assert_tested_dist_is_immutable(job: str, evidence_root: str) -> None:
    before = f"{evidence_root}/before/byte-contract.json"
    after = f"{evidence_root}/after/byte-contract.json"
    compare = f"--compare-manifests {evidence_root}"

    assert job.count(before) == 1
    assert job.count(after) == 1
    assert job.count(compare) == 1
    assert job.count("--expected-count 2") == 1
    assert job.index("npm run build") < job.index(before)
    assert job.index(before) < job.index("npm run test:v1")
    assert job.index(after) < job.index(compare)


def test_ci_quality_job_builds_once_before_web_and_python_contracts() -> None:
    quality = _job_body("ecorex-v1-ci.yml", "quality", "platform-smoke")

    _assert_single_workflow_owned_build_before_tests(quality)
    _assert_tested_dist_is_immutable(quality, ".ci/web-dist-immutability")
    assert quality.index("npm run test:v1") < quality.index("python -m pytest -q")
    assert quality.index("python -m pytest -q") < quality.index(
        ".ci/web-dist-immutability/after/byte-contract.json"
    )


def test_candidate_quality_job_builds_once_before_all_acceptance_suites() -> None:
    quality = _job_body(
        "ecorex-v1-candidate.yml",
        "quality",
        "image-shared-storage",
    )

    _assert_single_workflow_owned_build_before_tests(quality)
    _assert_tested_dist_is_immutable(
        quality, ".candidate/quality/web-dist-immutability"
    )
    assert quality.index("npm run test:v1") < quality.index("python -m pytest -q")
    assert quality.index("npm run build") < quality.index("npx playwright test")
    assert quality.count("npx playwright test") == 1
    assert quality.index("python scripts/check-v1-server-schema-authority.py") < quality.index(
        ".candidate/quality/web-dist-immutability/after/byte-contract.json"
    )
