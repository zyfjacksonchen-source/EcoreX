import contextlib
import importlib.util
import io
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_audit_module():
    script = ROOT / "scripts" / "audit-v022-goal-completion.py"
    spec = importlib.util.spec_from_file_location("audit_v022_goal_completion_for_tests", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v022_goal_completion_audit_reports_complete_release_goal():
    module = _load_audit_module()

    audit = module.build_completion_audit()

    assert audit["status"] == "PASS"
    assert audit["complete"] is True
    assert audit["releaseGate"]["errors"] == []
    assert audit["releaseGate"]["blockers"] == []
    assert audit["completionBlockers"] == []
    assert audit["incompleteRequirements"] == []

    proven_ids = {item["id"] for item in audit["requirements"] if item["status"] == "PROVEN"}
    for requirement_id in (
        "backend-runtime-source-of-truth",
        "frontend-projection-recovery",
        "feishu-im-auth-observability",
        "admin-audit-capability-policy",
        "image-generation-runtime-jobs",
        "scheduler-projection-management",
        "web-ux-session-markdown-status-runcenter",
        "release-target-deploy-rollback",
    ):
        assert requirement_id in proven_ids


def test_v022_goal_completion_audit_require_complete_exits_zero_when_complete(tmp_path):
    module = _load_audit_module()
    artifact = tmp_path / "goal-completion-audit.json"

    with contextlib.redirect_stdout(io.StringIO()) as stdout:
        exit_code = module.main(["--json", "--artifact", str(artifact), "--require-complete"])

    payload = json.loads(stdout.getvalue())
    artifact_payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload == artifact_payload
    assert payload["status"] == "PASS"
    assert payload["complete"] is True


def test_v022_goal_completion_audit_has_pass_path_with_promoted_release_gate():
    module = _load_audit_module()
    acceptance_rows = module._load_acceptance_rows()
    matrix_summary = module._load_matrix_summary()
    release_gate = module._load_release_gate_result()

    acceptance_rows["R22-12"] = dict(acceptance_rows["R22-12"], status="PASS")
    release_gate = {
        **release_gate,
        "status": "PASS",
        "releasable": True,
        "errors": [],
        "blockers": [],
    }

    audit = module.build_completion_audit(
        acceptance_rows=acceptance_rows,
        matrix_summary=matrix_summary,
        release_gate=release_gate,
    )

    assert audit["status"] == "PASS"
    assert audit["complete"] is True
    assert audit["incompleteRequirements"] == []
    assert audit["completionBlockers"] == []


def test_v022_goal_completion_audit_sanitizes_release_gate_errors():
    module = _load_audit_module()
    acceptance_rows = module._load_acceptance_rows()
    matrix_summary = module._load_matrix_summary()
    release_gate = module._load_release_gate_result()
    release_gate = {
        **release_gate,
        "errors": [
            "target failed for raw-target.example with C:/secret-key.pem and https://raw-target.example/app",
        ],
        "blockers": [
            {
                "id": "raw-target.example/unsafe",
                "surface": "release",
                "reason": "raw-target.example C:/secret-key.pem https://raw-target.example/app",
            }
        ],
    }

    audit = module.build_completion_audit(
        acceptance_rows=acceptance_rows,
        matrix_summary=matrix_summary,
        release_gate=release_gate,
    )

    serialized = json.dumps(audit, sort_keys=True)
    for raw in ("raw-target", "secret-key", "https://raw-target"):
        assert raw not in serialized
    assert audit["status"] == "ERROR"
    assert audit["complete"] is False
    assert audit["releaseGate"]["errors"][0]["errorType"] == "release-gate-error"
    assert "errorHash" in audit["releaseGate"]["errors"][0]
    assert audit["completionBlockers"][0]["reason"] == "redacted-release-blocker-reason"
    assert "reasonHash" in audit["completionBlockers"][0]


def test_v022_goal_completion_audit_requires_reviewed_matrix_for_proven_requirements():
    module = _load_audit_module()
    acceptance_rows = module._load_acceptance_rows()
    matrix_summary = {**module._load_matrix_summary(), "status": "LOCAL-PASS-REVIEW-PENDING"}
    release_gate = module._load_release_gate_result()

    audit = module.build_completion_audit(
        acceptance_rows=acceptance_rows,
        matrix_summary=matrix_summary,
        release_gate=release_gate,
    )

    requirements = {item["id"]: item for item in audit["requirements"]}
    assert requirements["backend-runtime-source-of-truth"]["status"] == "INCOMPLETE"
    assert any(
        "harness matrix status is LOCAL-PASS-REVIEW-PENDING" == gap
        for gap in requirements["backend-runtime-source-of-truth"]["gaps"]
    )


def test_v022_goal_completion_audit_hashes_host_like_blocker_ids_and_surfaces():
    module = _load_audit_module()
    acceptance_rows = module._load_acceptance_rows()
    matrix_summary = module._load_matrix_summary()
    release_gate = module._load_release_gate_result()
    release_gate = {
        **release_gate,
        "blockers": [
            {
                "id": "raw-target.example",
                "surface": "raw-target.example",
                "reason": "raw-target.example",
            }
        ],
    }

    audit = module.build_completion_audit(
        acceptance_rows=acceptance_rows,
        matrix_summary=matrix_summary,
        release_gate=release_gate,
    )

    serialized = json.dumps(audit, sort_keys=True)
    assert "raw-target" not in serialized
    assert audit["completionBlockers"][0]["id"].startswith("release-blocker-")
    assert audit["completionBlockers"][0]["surface"].startswith("release-")
