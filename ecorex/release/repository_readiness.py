"""GitHub repository governance and release-readiness authority.

The release workflows are intentionally useless when repository governance,
protected Environments, privileged runners or configuration are absent.  This
module turns that operational state into a bounded, machine-readable contract
and provides an idempotent governance bootstrap.  It never accepts or returns
secret values.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any, Mapping, Protocol, runtime_checkable


class RepositoryReadinessError(RuntimeError):
    """Non-sensitive repository readiness or governance failure."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        compensated: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.compensated = compensated


@runtime_checkable
class GitHubAdminCredentialProvider(Protocol):
    def bearer_token(self) -> str: ...


class EnvironmentGitHubAdminCredential:
    """Read an administrator token at request time without exposing it."""

    __slots__ = ("_environment", "_variable")

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        variable: str = "ECOREX_GITHUB_ADMIN_TOKEN",
    ) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", variable) is None:
            raise ValueError("GitHub administrator token environment is invalid")
        self._environment = os.environ if environment is None else environment
        self._variable = variable

    def bearer_token(self) -> str:
        token = self._environment.get(self._variable)
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 4096
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
        ):
            raise RepositoryReadinessError("github_admin_credentials_unavailable")
        return token

    def __repr__(self) -> str:
        return (
            "<EnvironmentGitHubAdminCredential "
            f"variable={self._variable!r} token=<redacted>>"
        )


@dataclass(frozen=True, slots=True)
class EnvironmentContract:
    name: str
    variables: frozenset[str] = frozenset()
    secrets: frozenset[str] = frozenset()
    minimum_reviewers: int = 1


@dataclass(frozen=True, slots=True)
class RunnerContract:
    role: str
    labels: frozenset[str]
    minimum_online: int = 1


@dataclass(frozen=True, slots=True)
class ReleaseRepositoryContract:
    default_branch: str
    workflows: frozenset[str]
    status_checks: frozenset[str]
    environments: tuple[EnvironmentContract, ...]
    runners: tuple[RunnerContract, ...]


_SIGNER_VARIABLES = frozenset(
    {
        "ECOREX_RELEASE_SIGNER_EXECUTABLE",
        "ECOREX_RELEASE_SIGNER_EXECUTABLE_SHA256",
        "ECOREX_RELEASE_SIGNER_ADAPTER",
        "ECOREX_RELEASE_SIGNER_ADAPTER_SHA256",
        "ECOREX_RELEASE_SIGNER_KEY_ID",
        "ECOREX_RELEASE_SIGNER_PUBLIC_KEY",
    }
)
_PUBLICATION_SIGNER_VARIABLES = frozenset(
    {
        "ECOREX_PUBLICATION_SIGNER_EXECUTABLE",
        "ECOREX_PUBLICATION_SIGNER_EXECUTABLE_SHA256",
        "ECOREX_PUBLICATION_SIGNER_ADAPTER",
        "ECOREX_PUBLICATION_SIGNER_ADAPTER_SHA256",
        "ECOREX_PUBLICATION_SIGNER_KEY_ID",
        "ECOREX_PUBLICATION_SIGNER_PUBLIC_KEY",
    }
)
_PUBLICATION_VARIABLES = frozenset(
    {
        "ECOREX_RELEASE_PUBLICATION_CONFIG",
        "ECOREX_BOOTSTRAP_INDEX_PUBLICATION_CONFIG",
        "ECOREX_CONTROL_PLANE_URL",
        "ECOREX_CONTROL_PLANE_HOSTS",
    }
)
_PUBLICATION_SECRETS = frozenset(
    {
        "ECOREX_MIRROR_TOKEN",
        "ECOREX_CDN_TOKEN",
        "ECOREX_BOOTSTRAP_INDEX_TOKEN",
        "ECOREX_CONTROL_PLANE_TOKEN",
    }
)
_STAGE_VARIABLES = frozenset(
    {
        "ECOREX_STAGE_RUNTIME_CONFIG_WINDOWS_X64_BASE64",
        "ECOREX_STAGE_RUNTIME_CONFIG_WINDOWS_X64_SHA256",
        "ECOREX_STAGE_RUNTIME_CONFIG_MACOS_ARM64_BASE64",
        "ECOREX_STAGE_RUNTIME_CONFIG_MACOS_ARM64_SHA256",
        "ECOREX_STAGE_RUNTIME_CONFIG_MACOS_X64_BASE64",
        "ECOREX_STAGE_RUNTIME_CONFIG_MACOS_X64_SHA256",
        "ECOREX_PUBLIC_BOOTSTRAP_INDEX_URL",
        "ECOREX_PUBLICATION_PUBLIC_KEYS_JSON",
    }
)


def default_release_repository_contract() -> ReleaseRepositoryContract:
    """Return the single v1 repository governance contract."""

    signing_variables = _SIGNER_VARIABLES | {
        "ECOREX_RELEASE_MIRROR_BASE_URL",
        "ECOREX_RELEASE_CDN_BASE_URL",
    }
    publication_variables = (
        _SIGNER_VARIABLES | _PUBLICATION_SIGNER_VARIABLES | _PUBLICATION_VARIABLES
    )
    return ReleaseRepositoryContract(
        default_branch="main",
        workflows=frozenset(
            {
                ".github/workflows/ecorex-v1-ci.yml",
                ".github/workflows/ecorex-v1-platform-stage.yml",
                ".github/workflows/ecorex-v1-candidate.yml",
                ".github/workflows/ecorex-v1-promote-candidate.yml",
            }
        ),
        status_checks=frozenset(
            {
                "v1 quality and deterministic build",
                "Windows x64 compatibility",
                "macOS arm64 compatibility",
                "macOS x64 compatibility",
                "Cross-runner byte stability",
            }
        ),
        environments=(
            EnvironmentContract("ecorex-release-stage", _STAGE_VARIABLES),
            EnvironmentContract("ecorex-release-signing-canary", signing_variables),
            EnvironmentContract("ecorex-release-signing-stable", signing_variables),
            EnvironmentContract(
                "ecorex-live-acceptance",
                frozenset(
                    {
                        "ECOREX_LIVE_ACCEPTANCE_EXECUTABLE",
                        "ECOREX_LIVE_ACCEPTANCE_EXECUTABLE_SHA256",
                        "ECOREX_RELEASE_SIGNER_PUBLIC_KEY",
                    }
                ),
            ),
            EnvironmentContract(
                "ecorex-release-publication-canary",
                publication_variables,
                _PUBLICATION_SECRETS,
            ),
            EnvironmentContract(
                "ecorex-release-publication-stable",
                publication_variables,
                _PUBLICATION_SECRETS,
            ),
        ),
        runners=(
            RunnerContract(
                "platform-windows",
                frozenset(
                    {"self-hosted", "windows", "x64", "ecorex-platform-windows"}
                ),
            ),
            RunnerContract(
                "release-sign",
                frozenset({"self-hosted", "linux", "x64", "ecorex-release-sign"}),
            ),
            RunnerContract(
                "live-acceptance",
                frozenset({"self-hosted", "windows", "x64", "ecorex-live-acceptance"}),
            ),
            RunnerContract(
                "release-publication",
                frozenset({"self-hosted", "linux", "x64", "ecorex-release-publish"}),
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class ReadinessFinding:
    code: str
    category: str
    subject: str
    blocking: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "blocking": self.blocking,
            "category": self.category,
            "code": self.code,
            "subject": self.subject,
        }


def evaluate_release_repository(
    snapshot: Mapping[str, Any],
    contract: ReleaseRepositoryContract | None = None,
) -> dict[str, Any]:
    """Evaluate a normalized, non-secret repository snapshot."""

    expected = contract or default_release_repository_contract()
    findings: list[ReadinessFinding] = []

    if snapshot.get("default_branch") != expected.default_branch:
        findings.append(
            ReadinessFinding(
                "default_branch_mismatch", "repository", expected.default_branch
            )
        )

    actions = _mapping(snapshot.get("actions_permissions"))
    for field, required_value, code in (
        ("enabled", True, "actions_disabled"),
        ("allowed_actions", "selected", "actions_allowlist_not_selected"),
        ("github_owned_allowed", True, "github_owned_actions_disabled"),
        ("verified_allowed", False, "verified_creator_actions_allowed"),
        ("default_workflow_permissions", "read", "workflow_default_write_enabled"),
        (
            "can_approve_pull_request_reviews",
            False,
            "workflow_pull_request_approval_enabled",
        ),
    ):
        if actions.get(field) != required_value:
            findings.append(ReadinessFinding(code, "actions", field))
    if _string_set(actions.get("patterns_allowed")):
        findings.append(
            ReadinessFinding(
                "third_party_action_patterns_allowed", "actions", "patterns_allowed"
            )
        )

    scopes = snapshot.get("oauth_scopes")
    if isinstance(scopes, list) and scopes and "workflow" not in scopes:
        findings.append(
            ReadinessFinding(
                "workflow_push_scope_missing", "credential", "workflow"
            )
        )

    workflow_map = _mapping(snapshot.get("workflows"))
    for path in sorted(expected.workflows):
        if workflow_map.get(path) != "active":
            findings.append(ReadinessFinding("workflow_not_active", "workflow", path))

    protection = _mapping(snapshot.get("branch_protection"))
    if not protection.get("enabled"):
        findings.append(
            ReadinessFinding(
                "default_branch_unprotected", "branch", expected.default_branch
            )
        )
    else:
        required_checks = _string_set(protection.get("status_checks"))
        for context in sorted(expected.status_checks - required_checks):
            findings.append(
                ReadinessFinding("required_status_check_missing", "branch", context)
            )
        for field, code in (
            ("strict", "strict_status_checks_disabled"),
            ("enforce_admins", "administrator_bypass_enabled"),
            ("pull_request_required", "pull_request_not_required"),
            ("conversation_resolution", "conversation_resolution_disabled"),
            ("linear_history", "linear_history_disabled"),
        ):
            if protection.get(field) is not True:
                findings.append(
                    ReadinessFinding(code, "branch", expected.default_branch)
                )
        if protection.get("allow_force_pushes") is not False:
            findings.append(
                ReadinessFinding(
                    "force_push_not_blocked", "branch", expected.default_branch
                )
            )
        if protection.get("allow_deletions") is not False:
            findings.append(
                ReadinessFinding(
                    "branch_deletion_not_blocked", "branch", expected.default_branch
                )
            )

    environments = _mapping(snapshot.get("environments"))
    for environment in expected.environments:
        state = _mapping(environments.get(environment.name))
        if state.get("exists") is not True:
            findings.append(
                ReadinessFinding(
                    "environment_missing", "environment", environment.name
                )
            )
            continue
        if (
            state.get("protected_branches") is not True
            or state.get("custom_branch_policies") is not False
        ):
            findings.append(
                ReadinessFinding(
                    "environment_branch_policy_invalid",
                    "environment",
                    environment.name,
                )
            )
        reviewers = state.get("reviewer_count")
        if (
            isinstance(reviewers, bool)
            or not isinstance(reviewers, int)
            or reviewers < environment.minimum_reviewers
        ):
            findings.append(
                ReadinessFinding(
                    "environment_reviewer_missing", "environment", environment.name
                )
            )
        present_variables = _string_set(state.get("variables"))
        for variable in sorted(environment.variables - present_variables):
            findings.append(
                ReadinessFinding(
                    "environment_variable_missing",
                    "environment-variable",
                    f"{environment.name}:{variable}",
                )
            )
        present_secrets = _string_set(state.get("secrets"))
        for secret in sorted(environment.secrets - present_secrets):
            findings.append(
                ReadinessFinding(
                    "environment_secret_missing",
                    "environment-secret",
                    f"{environment.name}:{secret}",
                )
            )

    runners = snapshot.get("runners")
    normalized_runners = runners if isinstance(runners, list) else []
    role_matches: dict[str, set[str]] = {}
    for required_runner in expected.runners:
        matches: set[str] = set()
        for runner in normalized_runners:
            if not isinstance(runner, Mapping) or runner.get("status") != "online":
                continue
            labels = {value.casefold() for value in _string_set(runner.get("labels"))}
            if {value.casefold() for value in required_runner.labels} <= labels:
                name = runner.get("name")
                if isinstance(name, str) and name:
                    matches.add(name)
        role_matches[required_runner.role] = matches
        if len(matches) < required_runner.minimum_online:
            findings.append(
                ReadinessFinding(
                    "runner_role_unavailable", "runner", required_runner.role
                )
            )

    isolated = (
        role_matches.get("platform-windows", set()),
        role_matches.get("release-sign", set()),
        role_matches.get("release-publication", set()),
        role_matches.get("live-acceptance", set()),
    )
    if any(left & right for index, left in enumerate(isolated) for right in isolated[index + 1 :]):
        findings.append(
            ReadinessFinding(
                "privileged_runner_role_overlap",
                "runner",
                "platform/sign/live/publication",
            )
        )

    ordered = sorted(
        findings, key=lambda item: (item.category, item.subject, item.code)
    )
    return {
        "blocking_count": sum(item.blocking for item in ordered),
        "findings": [item.as_dict() for item in ordered],
        "ready": not any(item.blocking for item in ordered),
        "schema_version": 1,
        "status": "passed" if not ordered else "blocked",
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_set(value: Any) -> set[str]:
    return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()


__all__ = [
    "EnvironmentContract",
    "EnvironmentGitHubAdminCredential",
    "GitHubAdminCredentialProvider",
    "ReadinessFinding",
    "ReleaseRepositoryContract",
    "RepositoryReadinessError",
    "RunnerContract",
    "default_release_repository_contract",
    "evaluate_release_repository",
]
