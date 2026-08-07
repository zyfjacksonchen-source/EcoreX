from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ecorex.release.repository_admin import GitHubRepositoryAdminClient
from ecorex.release.repository_readiness import (
    EnvironmentGitHubAdminCredential,
    RepositoryReadinessError,
    default_release_repository_contract,
    evaluate_release_repository,
)


ROOT = Path(__file__).resolve().parents[2]
HEAD = "a" * 40
GOVERNANCE_CLI = ROOT / "scripts" / "manage-v1-github-release-repository.py"


def _healthy_snapshot() -> dict[str, Any]:
    contract = default_release_repository_contract()
    environments = {
        item.name: {
            "custom_branch_policies": False,
            "exists": True,
            "protected_branches": True,
            "reviewer_count": item.minimum_reviewers,
            "secrets": sorted(item.secrets),
            "variables": sorted(item.variables),
        }
        for item in contract.environments
    }
    runners = [
        {
            "busy": False,
            "labels": sorted(item.labels),
            "name": f"runner-{item.role}",
            "status": "online",
        }
        for item in contract.runners
    ]
    return {
        "actions_permissions": {
            "allowed_actions": "selected",
            "can_approve_pull_request_reviews": False,
            "default_workflow_permissions": "read",
            "enabled": True,
            "github_owned_allowed": True,
            "patterns_allowed": [],
            "verified_allowed": False,
        },
        "branch_protection": {
            "allow_deletions": False,
            "allow_force_pushes": False,
            "conversation_resolution": True,
            "enabled": True,
            "enforce_admins": True,
            "linear_history": True,
            "pull_request_required": True,
            "status_checks": sorted(contract.status_checks),
            "strict": True,
        },
        "default_branch": contract.default_branch,
        "default_branch_sha": HEAD,
        "environments": environments,
        "oauth_scopes": ["repo", "workflow"],
        "repository": "owner/repository",
        "runners": runners,
        "visibility": "private",
        "workflows": {path: "active" for path in contract.workflows},
    }


def test_complete_repository_contract_is_ready_without_secret_values() -> None:
    result = evaluate_release_repository(_healthy_snapshot())

    assert result == {
        "blocking_count": 0,
        "findings": [],
        "ready": True,
        "schema_version": 1,
        "status": "passed",
    }
    serialized = json.dumps(_healthy_snapshot(), sort_keys=True)
    assert "secret-value" not in serialized


def test_empty_remote_state_reports_every_operational_release_layer() -> None:
    snapshot = _healthy_snapshot()
    snapshot.update(
        {
            "branch_protection": {"enabled": False},
            "environments": {},
            "oauth_scopes": ["repo"],
            "runners": [],
            "workflows": {},
        }
    )

    result = evaluate_release_repository(snapshot)
    codes = {item["code"] for item in result["findings"]}

    assert result["ready"] is False
    assert {
        "default_branch_unprotected",
        "environment_missing",
        "runner_role_unavailable",
        "workflow_not_active",
        "workflow_push_scope_missing",
    } <= codes


def test_environment_configuration_is_scoped_and_fail_closed() -> None:
    snapshot = _healthy_snapshot()
    environment = snapshot["environments"]["ecorex-release-signing-stable"]
    environment["reviewer_count"] = 0
    environment["protected_branches"] = False
    environment["variables"].remove("ECOREX_RELEASE_SIGNER_EXECUTABLE")
    environment["secrets"].remove("ECOREX_GITHUB_RELEASE_READ_TOKEN")

    result = evaluate_release_repository(snapshot)
    findings = {
        (item["code"], item["subject"]) for item in result["findings"]
    }

    assert (
        "environment_variable_missing",
        "ecorex-release-signing-stable:ECOREX_RELEASE_SIGNER_EXECUTABLE",
    ) in findings
    assert (
        "environment_secret_missing",
        "ecorex-release-signing-stable:ECOREX_GITHUB_RELEASE_READ_TOKEN",
    ) in findings
    assert (
        "environment_reviewer_missing",
        "ecorex-release-signing-stable",
    ) in findings
    assert (
        "environment_branch_policy_invalid",
        "ecorex-release-signing-stable",
    ) in findings


def test_signing_environment_requires_cross_repository_read_only_token() -> None:
    snapshot = _healthy_snapshot()
    environment = snapshot["environments"]["ecorex-release-signing-stable"]
    environment["secrets"].remove("ECOREX_GITHUB_RELEASE_READ_TOKEN")

    result = evaluate_release_repository(snapshot)

    assert {
        (item["code"], item["subject"]) for item in result["findings"]
    } >= {
        (
            "environment_secret_missing",
            "ecorex-release-signing-stable:ECOREX_GITHUB_RELEASE_READ_TOKEN",
        )
    }
    assert all(
        "ECOREX_GITHUB_RELEASE_TOKEN" not in environment["secrets"]
        for environment in snapshot["environments"].values()
    )


def test_signing_live_and_cloud_build_roles_must_not_overlap() -> None:
    snapshot = _healthy_snapshot()
    shared = {
        "busy": False,
        "labels": [
            "self-hosted",
            "linux",
            "windows",
            "arm64",
            "x64",
            "ecorex-release-sign",
            "ecorex-live-acceptance",
            "ecorex-cloud-build",
        ],
        "name": "unsafe-shared-privileged-runner",
        "status": "online",
    }
    snapshot["runners"] = [
        runner
        for runner in snapshot["runners"]
        if runner["name"]
        not in {
            "runner-release-sign",
            "runner-live-acceptance",
            "runner-cloud-build",
        }
    ] + [shared]

    result = evaluate_release_repository(snapshot)

    assert any(
        item["code"] == "privileged_runner_role_overlap"
        for item in result["findings"]
    )


def test_contract_covers_every_protected_workflow_and_environment() -> None:
    contract = default_release_repository_contract()
    workflow_text = {
        path: (ROOT / path).read_text(encoding="utf-8")
        for path in contract.workflows
    }
    combined = "\n".join(workflow_text.values())

    assert set(workflow_text) == {
        ".github/workflows/ecorex-v1-ci.yml",
        ".github/workflows/ecorex-v1-platform-stage.yml",
        ".github/workflows/ecorex-v1-candidate.yml",
    }
    assert (
        "Windows x64 compatibility"
        in default_release_repository_contract().status_checks
    )
    for environment in contract.environments:
        if environment.name.endswith(("-canary", "-stable")):
            assert environment.name.rsplit("-", 1)[0] in combined
        else:
            assert environment.name in combined
        for variable in environment.variables:
            if variable.startswith("ECOREX_STAGE_RUNTIME_CONFIG_"):
                continue
            assert variable in combined
        for secret in environment.secrets:
            assert secret in combined
    for runner in contract.runners:
        assert any(label in combined for label in runner.labels if label.startswith("ecorex-"))


def _response(status: int, value: Any) -> httpx.Response:
    return httpx.Response(
        status,
        headers={
            "content-type": "application/json",
            "x-oauth-scopes": "repo, workflow",
        },
        json=value,
    )


def _empty_response(status: int = 204) -> httpx.Response:
    return httpx.Response(status, headers={"x-oauth-scopes": "repo, workflow"})


def test_governance_apply_is_head_fenced_and_idempotent_put_only() -> None:
    writes: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/branches/main"):
            return _response(200, {"commit": {"sha": HEAD}})
        if request.method == "GET" and "/environments/" in request.url.path:
            return _response(404, {"message": "Not Found"})
        if request.method == "PUT":
            writes.append((request.url.path, json.loads(request.content)))
            if "/actions/permissions" in request.url.path:
                return _empty_response()
            return _response(200, {"ok": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, trust_env=False) as http:
        client = GitHubRepositoryAdminClient(
            owner="owner",
            repository="repository",
            credentials=EnvironmentGitHubAdminCredential(
                {"TOKEN": "github-token"}, variable="TOKEN"
            ),
            client=http,
        )
        client.apply_governance(expected_head=HEAD, reviewer_id=123)

    contract = default_release_repository_contract()
    assert len(writes) == len(contract.environments) + 4
    environment_writes = [item for item in writes if "/environments/" in item[0]]
    assert len(environment_writes) == len(contract.environments)
    assert all(
        payload["deployment_branch_policy"]
        == {"custom_branch_policies": False, "protected_branches": True}
        for _, payload in environment_writes
    )
    action_writes = [item for item in writes if "/actions/permissions" in item[0]]
    assert action_writes == [
        (
            "/repos/owner/repository/actions/permissions",
            {"allowed_actions": "selected", "enabled": True},
        ),
        (
            "/repos/owner/repository/actions/permissions/selected-actions",
            {
                "github_owned_allowed": True,
                "patterns_allowed": [],
                "verified_allowed": False,
            },
        ),
        (
            "/repos/owner/repository/actions/permissions/workflow",
            {
                "can_approve_pull_request_reviews": False,
                "default_workflow_permissions": "read",
            },
        ),
    ]
    protection = writes[-1][1]
    assert protection["enforce_admins"] is True
    assert protection["allow_force_pushes"] is False
    assert protection["required_status_checks"] == {
        "contexts": sorted(contract.status_checks),
        "strict": True,
    }


def test_billing_plan_failure_is_typed_and_compensates_new_environment() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith("/branches/main"):
            return _response(200, {"commit": {"sha": HEAD}})
        if request.method == "GET" and "/environments/" in request.url.path:
            return _response(404, {"message": "Not Found"})
        if request.method == "PUT" and "/environments/" in request.url.path:
            return _response(
                422,
                {
                    "message": (
                        "Failed to create the environment protection rule. "
                        "Please ensure the billing plan supports the required "
                        "reviewers protection rule."
                    ),
                    "status": "422",
                },
            )
        if request.method == "DELETE" and "/environments/" in request.url.path:
            return _empty_response()
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        client = GitHubRepositoryAdminClient(
            owner="owner",
            repository="repository",
            credentials=EnvironmentGitHubAdminCredential(
                {"TOKEN": "github-token"}, variable="TOKEN"
            ),
            client=http,
        )
        with pytest.raises(RepositoryReadinessError) as captured:
            client.apply_governance(expected_head=HEAD, reviewer_id=123)

    assert captured.value.code == "github_environment_reviewers_plan_unsupported"
    assert captured.value.retryable is False
    assert captured.value.compensated is True
    assert requests == [
        ("GET", "/repos/owner/repository/branches/main"),
        ("GET", "/repos/owner/repository/environments/ecorex-release-stage"),
        ("PUT", "/repos/owner/repository/environments/ecorex-release-stage"),
        ("DELETE", "/repos/owner/repository/environments/ecorex-release-stage"),
    ]


def test_billing_plan_failure_never_deletes_an_existing_environment() -> None:
    deletes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deletes
        if request.method == "GET" and request.url.path.endswith("/branches/main"):
            return _response(200, {"commit": {"sha": HEAD}})
        if request.method == "GET" and "/environments/" in request.url.path:
            return _response(200, {"name": "ecorex-release-stage"})
        if request.method == "PUT" and "/environments/" in request.url.path:
            return _response(
                422,
                {
                    "message": (
                        "Failed to create the environment protection rule. "
                        "Please ensure the billing plan supports the required "
                        "reviewers protection rule."
                    )
                },
            )
        if request.method == "DELETE":
            deletes += 1
            return _empty_response()
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        client = GitHubRepositoryAdminClient(
            owner="owner",
            repository="repository",
            credentials=EnvironmentGitHubAdminCredential(
                {"TOKEN": "github-token"}, variable="TOKEN"
            ),
            client=http,
        )
        with pytest.raises(RepositoryReadinessError) as captured:
            client.apply_governance(expected_head=HEAD, reviewer_id=123)

    assert captured.value.code == "github_environment_reviewers_plan_unsupported"
    assert captured.value.compensated is False
    assert deletes == 0


def test_failed_environment_compensation_is_explicit_and_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/branches/main"):
            return _response(200, {"commit": {"sha": HEAD}})
        if request.method == "GET" and "/environments/" in request.url.path:
            return _response(404, {"message": "Not Found"})
        if request.method == "PUT" and "/environments/" in request.url.path:
            return _response(422, {"message": "validation failed"})
        if request.method == "DELETE" and "/environments/" in request.url.path:
            return _response(503, {"message": "unavailable"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        client = GitHubRepositoryAdminClient(
            owner="owner",
            repository="repository",
            credentials=EnvironmentGitHubAdminCredential(
                {"TOKEN": "github-token"}, variable="TOKEN"
            ),
            client=http,
        )
        with pytest.raises(RepositoryReadinessError) as captured:
            client.apply_governance(expected_head=HEAD, reviewer_id=123)

    assert captured.value.code == "github_environment_partial_cleanup_failed"
    assert captured.value.retryable is True
    assert captured.value.compensated is False


def test_governance_cli_records_compensation_without_remote_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location("repository_governance_cli", GOVERNANCE_CLI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def snapshot(self, _contract: object) -> dict[str, Any]:
            return _healthy_snapshot()

        def resolve_user_id(self, _login: str) -> int:
            return 123

        def apply_governance(self, **_kwargs: object) -> None:
            raise RepositoryReadinessError(
                "github_environment_reviewers_plan_unsupported",
                compensated=True,
            )

    monkeypatch.setattr(module, "GitHubRepositoryAdminClient", FakeClient)
    monkeypatch.setattr(
        module,
        "EnvironmentGitHubAdminCredential",
        lambda **_kwargs: object(),
    )
    output = tmp_path / "governance.json"

    exit_code = module.main(
        [
            "bootstrap",
            "--repository",
            "owner/repository",
            "--confirm-repository",
            "owner/repository",
            "--expected-head",
            HEAD,
            "--reviewer-login",
            "reviewer",
            "--output",
            str(output),
        ]
    )
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert report["status"] == "failed"
    assert report["action"] == "none"
    assert report["error"] == "github_environment_reviewers_plan_unsupported"
    assert report["compensated"] is True
    assert "billing" not in json.dumps(report).casefold()


def test_snapshot_reads_environment_reviewers_without_secret_values() -> None:
    contract = default_release_repository_contract()
    environment_contracts = {item.name: item for item in contract.environments}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/owner/repository":
            return _response(
                200,
                {
                    "default_branch": "main",
                    "visibility": "private",
                },
            )
        if path == "/repos/owner/repository/branches/main":
            return _response(200, {"commit": {"sha": HEAD}})
        if path.endswith("/branches/main/protection"):
            return _response(404, {"message": "not protected"})
        if path.endswith("/actions/permissions/workflow"):
            return _response(
                200,
                {
                    "can_approve_pull_request_reviews": False,
                    "default_workflow_permissions": "read",
                },
            )
        if path.endswith("/actions/permissions/selected-actions"):
            return _response(
                200,
                {
                    "github_owned_allowed": True,
                    "patterns_allowed": [],
                    "verified_allowed": False,
                },
            )
        if path.endswith("/actions/permissions"):
            return _response(200, {"allowed_actions": "selected", "enabled": True})
        if path.endswith("/actions/workflows"):
            return _response(
                200,
                {
                    "total_count": len(contract.workflows),
                    "workflows": [
                        {"path": item, "state": "active"}
                        for item in sorted(contract.workflows)
                    ],
                },
            )
        if path.endswith("/environments"):
            return _response(
                200,
                {
                    "total_count": len(contract.environments),
                    "environments": [
                        {"name": item.name} for item in contract.environments
                    ],
                },
            )
        marker = "/environments/"
        if marker in path:
            tail = path.split(marker, 1)[1]
            name = tail.split("/", 1)[0]
            item = environment_contracts[name]
            if tail.endswith("/variables"):
                return _response(
                    200,
                    {
                        "total_count": len(item.variables),
                        "variables": [
                            {"name": value} for value in sorted(item.variables)
                        ],
                    },
                )
            if tail.endswith("/secrets"):
                return _response(
                    200,
                    {
                        "total_count": len(item.secrets),
                        "secrets": [
                            {"name": value} for value in sorted(item.secrets)
                        ],
                    },
                )
            return _response(
                200,
                {
                    "deployment_branch_policy": {
                        "custom_branch_policies": False,
                        "protected_branches": True,
                    },
                    "protection_rules": [
                        {
                            "reviewers": [
                                {"reviewer": {"id": 123}, "type": "User"}
                            ],
                            "type": "required_reviewers",
                        }
                    ],
                },
            )
        if path.endswith("/actions/runners"):
            runners = [
                {
                    "busy": False,
                    "labels": [{"name": label} for label in sorted(item.labels)],
                    "name": f"runner-{item.role}",
                    "status": "online",
                }
                for item in contract.runners
            ]
            return _response(
                200, {"runners": runners, "total_count": len(runners)}
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        client = GitHubRepositoryAdminClient(
            owner="owner",
            repository="repository",
            credentials=EnvironmentGitHubAdminCredential(
                {"TOKEN": "github-token"}, variable="TOKEN"
            ),
            client=http,
        )
        snapshot = client.snapshot(contract)

    assert all(
        environment["reviewer_count"] == 1
        for environment in snapshot["environments"].values()
    )
    assert all(
        set(snapshot["environments"][item.name]["secrets"]) == item.secrets
        for item in contract.environments
    )
    result = evaluate_release_repository(snapshot, contract)
    assert {item["code"] for item in result["findings"]} == {
        "default_branch_unprotected"
    }


def test_governance_apply_refuses_default_branch_race_before_any_write() -> None:
    writes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        if request.method == "GET":
            return _response(200, {"commit": {"sha": "b" * 40}})
        writes += 1
        return _response(200, {})

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        client = GitHubRepositoryAdminClient(
            owner="owner",
            repository="repository",
            credentials=EnvironmentGitHubAdminCredential(
                {"TOKEN": "github-token"}, variable="TOKEN"
            ),
            client=http,
        )
        with pytest.raises(RepositoryReadinessError, match="default_branch_head_changed"):
            client.apply_governance(expected_head=HEAD, reviewer_id=123)

    assert writes == 0


def test_admin_credential_never_reveals_token() -> None:
    credential = EnvironmentGitHubAdminCredential(
        {"TOKEN": "super-sensitive-token"}, variable="TOKEN"
    )

    assert credential.bearer_token() == "super-sensitive-token"
    assert "super-sensitive-token" not in repr(credential)
    assert "<redacted>" in repr(credential)
