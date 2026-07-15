"""Bounded GitHub repository administration transport for release governance."""

from __future__ import annotations

import json
import re
from typing import Any, Final, Mapping
from urllib.parse import quote

import httpx

from .repository_readiness import (
    GitHubAdminCredentialProvider,
    ReleaseRepositoryContract,
    RepositoryReadinessError,
    default_release_repository_contract,
)


GITHUB_API_VERSION: Final = "2026-03-10"
_SAFE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SAFE_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAX_JSON_BYTES = 2 * 1024 * 1024


class GitHubRepositoryAdminClient:
    """Bounded repository settings client with idempotent governance writes."""

    def __init__(
        self,
        *,
        owner: str,
        repository: str,
        credentials: GitHubAdminCredentialProvider,
        client: httpx.Client | None = None,
    ) -> None:
        if _SAFE_PART.fullmatch(owner) is None or _SAFE_PART.fullmatch(repository) is None:
            raise ValueError("GitHub repository identity is invalid")
        if not isinstance(credentials, GitHubAdminCredentialProvider):
            raise TypeError("GitHub administrator credential provider is invalid")
        self.owner = owner
        self.repository = repository
        self.credentials = credentials
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=15, read=45, write=45, pool=15),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
        self._oauth_scopes: frozenset[str] = frozenset()

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "GitHubRepositoryAdminClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def repository_path(self) -> str:
        return f"/repos/{quote(self.owner, safe='')}/{quote(self.repository, safe='')}"

    def snapshot(self, contract: ReleaseRepositoryContract | None = None) -> dict[str, Any]:
        expected = contract or default_release_repository_contract()
        repository = self._json_request("GET", self.repository_path, accepted={200})[1]
        if not isinstance(repository, Mapping):
            raise RepositoryReadinessError("github_repository_response_invalid")
        default_branch = repository.get("default_branch")
        if not isinstance(default_branch, str):
            raise RepositoryReadinessError("github_repository_response_invalid")
        branch = self._json_request(
            "GET",
            f"{self.repository_path}/branches/{quote(default_branch, safe='')}",
            accepted={200},
        )[1]
        default_branch_sha = _nested_string(branch, "commit", "sha")

        protection_status, protection_raw = self._json_request(
            "GET",
            f"{self.repository_path}/branches/{quote(default_branch, safe='')}/protection",
            accepted={200, 404},
        )
        protection = (
            _normalize_protection(protection_raw)
            if protection_status == 200
            else {"enabled": False}
        )

        actions_raw = self._json_request(
            "GET", f"{self.repository_path}/actions/permissions", accepted={200}
        )[1]
        workflow_permissions = self._json_request(
            "GET",
            f"{self.repository_path}/actions/permissions/workflow",
            accepted={200},
        )[1]
        actions_permissions = _normalize_actions_permissions(
            actions_raw, workflow_permissions
        )
        if actions_permissions["allowed_actions"] == "selected":
            selected_actions = self._json_request(
                "GET",
                f"{self.repository_path}/actions/permissions/selected-actions",
                accepted={200},
            )[1]
            actions_permissions.update(_normalize_selected_actions(selected_actions))

        workflow_raw = self._json_request(
            "GET", f"{self.repository_path}/actions/workflows?per_page=100", accepted={200}
        )[1]
        workflows = _inventory_by_path(workflow_raw, "workflows", "path", "state")

        environment_list = self._json_request(
            "GET", f"{self.repository_path}/environments?per_page=100", accepted={200}
        )[1]
        environment_names = _inventory_names(environment_list, "environments")
        environments: dict[str, Any] = {}
        for environment in expected.environments:
            if environment.name not in environment_names:
                environments[environment.name] = {"exists": False}
                continue
            encoded = quote(environment.name, safe="")
            raw = self._json_request(
                "GET", f"{self.repository_path}/environments/{encoded}", accepted={200}
            )[1]
            variables_raw = self._json_request(
                "GET",
                f"{self.repository_path}/environments/{encoded}/variables?per_page=100",
                accepted={200},
            )[1]
            secrets_raw = self._json_request(
                "GET",
                f"{self.repository_path}/environments/{encoded}/secrets?per_page=100",
                accepted={200},
            )[1]
            environments[environment.name] = _normalize_environment(
                raw, variables_raw, secrets_raw
            )

        runner_raw = self._json_request(
            "GET", f"{self.repository_path}/actions/runners?per_page=100", accepted={200}
        )[1]
        runners = _normalize_runners(runner_raw)
        return {
            "actions_permissions": actions_permissions,
            "default_branch": default_branch,
            "default_branch_sha": default_branch_sha,
            "environments": environments,
            "oauth_scopes": sorted(self._oauth_scopes),
            "repository": f"{self.owner}/{self.repository}",
            "runners": runners,
            "visibility": repository.get("visibility"),
            "workflows": workflows,
            "branch_protection": protection,
        }

    def resolve_user_id(self, login: str) -> int:
        if _SAFE_LOGIN.fullmatch(login) is None:
            raise ValueError("GitHub reviewer login is invalid")
        value = self._json_request(
            "GET", f"/users/{quote(login, safe='')}", accepted={200}
        )[1]
        identifier = value.get("id") if isinstance(value, Mapping) else None
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 1:
            raise RepositoryReadinessError("github_reviewer_response_invalid")
        return identifier

    def apply_governance(
        self,
        *,
        expected_head: str,
        reviewer_id: int,
        contract: ReleaseRepositoryContract | None = None,
    ) -> None:
        expected = contract or default_release_repository_contract()
        if _SHA.fullmatch(expected_head) is None:
            raise ValueError("expected default-branch head is invalid")
        if isinstance(reviewer_id, bool) or not isinstance(reviewer_id, int) or reviewer_id < 1:
            raise ValueError("GitHub reviewer ID is invalid")
        branch = self._json_request(
            "GET",
            f"{self.repository_path}/branches/{quote(expected.default_branch, safe='')}",
            accepted={200},
        )[1]
        if _nested_string(branch, "commit", "sha") != expected_head:
            raise RepositoryReadinessError("default_branch_head_changed")

        for environment in expected.environments:
            self._json_request(
                "PUT",
                f"{self.repository_path}/environments/{quote(environment.name, safe='')}",
                accepted={200},
                payload={
                    "deployment_branch_policy": {
                        "custom_branch_policies": False,
                        "protected_branches": True,
                    },
                    "prevent_self_review": False,
                    "reviewers": [{"id": reviewer_id, "type": "User"}],
                    "wait_timer": 0,
                },
            )

        self._json_request(
            "PUT",
            f"{self.repository_path}/actions/permissions",
            accepted={204},
            payload={"allowed_actions": "selected", "enabled": True},
        )
        self._json_request(
            "PUT",
            f"{self.repository_path}/actions/permissions/selected-actions",
            accepted={204},
            payload={
                "github_owned_allowed": True,
                "patterns_allowed": [],
                "verified_allowed": False,
            },
        )
        self._json_request(
            "PUT",
            f"{self.repository_path}/actions/permissions/workflow",
            accepted={204},
            payload={
                "can_approve_pull_request_reviews": False,
                "default_workflow_permissions": "read",
            },
        )

        self._json_request(
            "PUT",
            f"{self.repository_path}/branches/{quote(expected.default_branch, safe='')}/protection",
            accepted={200},
            payload=_branch_protection_payload(expected),
        )

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        accepted: set[int],
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[int, Any]:
        if not path.startswith("/") or "//" in path:
            raise ValueError("GitHub API path is invalid")
        token = self.credentials.bearer_token()
        headers = {
            "Accept": "application/vnd.github+json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {token}",
            "User-Agent": "EcoreX-Repository-Readiness/1.0",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        content = None
        if payload is not None:
            content = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            request = self.client.build_request(
                method, f"https://api.github.com{path}", headers=headers, content=content
            )
            response = self.client.send(request, stream=True, follow_redirects=False)
            try:
                if response.is_redirect or response.history:
                    raise RepositoryReadinessError("github_repository_redirect_refused")
                scopes = response.headers.get("x-oauth-scopes")
                if scopes:
                    self._oauth_scopes = frozenset(
                        scope.strip() for scope in scopes.split(",") if scope.strip()
                    )
                if response.status_code not in accepted:
                    raise RepositoryReadinessError(
                        "github_repository_api_rejected",
                        retryable=response.status_code in {408, 425, 429, 502, 503, 504},
                    )
                if response.headers.get("content-encoding", "identity").casefold() != "identity":
                    raise RepositoryReadinessError("github_repository_compressed_response")
                if response.status_code == 204:
                    if any(response.iter_bytes()):
                        raise RepositoryReadinessError(
                            "github_repository_invalid_response"
                        )
                    return response.status_code, None
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if content_type != "application/json":
                    raise RepositoryReadinessError("github_repository_invalid_response")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_JSON_BYTES:
                        raise RepositoryReadinessError("github_repository_response_too_large")
                try:
                    value = json.loads(bytes(body).decode("utf-8"), object_pairs_hook=_unique_object)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    raise RepositoryReadinessError("github_repository_invalid_response") from None
                return response.status_code, value
            finally:
                response.close()
        except RepositoryReadinessError:
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            raise RepositoryReadinessError(
                "github_repository_api_unavailable", retryable=True
            ) from None
        finally:
            token = ""


def _branch_protection_payload(
    contract: ReleaseRepositoryContract,
) -> dict[str, object]:
    return {
        "allow_deletions": False,
        "allow_force_pushes": False,
        "allow_fork_syncing": False,
        "block_creations": False,
        "enforce_admins": True,
        "lock_branch": False,
        "required_conversation_resolution": True,
        "required_linear_history": True,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False,
            "require_last_push_approval": False,
            "required_approving_review_count": 0,
        },
        "required_status_checks": {
            "contexts": sorted(contract.status_checks),
            "strict": True,
        },
        "restrictions": None,
    }


def _normalize_protection(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RepositoryReadinessError("github_protection_response_invalid")
    status = _mapping(value.get("required_status_checks"))
    contexts = status.get("contexts")
    if not isinstance(contexts, list):
        contexts = []
    return {
        "allow_deletions": _enabled(value.get("allow_deletions")),
        "allow_force_pushes": _enabled(value.get("allow_force_pushes")),
        "conversation_resolution": _enabled(value.get("required_conversation_resolution")),
        "enabled": True,
        "enforce_admins": _enabled(value.get("enforce_admins")),
        "linear_history": _enabled(value.get("required_linear_history")),
        "pull_request_required": value.get("required_pull_request_reviews") is not None,
        "status_checks": sorted(value for value in contexts if isinstance(value, str)),
        "strict": status.get("strict") is True,
    }


def _normalize_actions_permissions(value: Any, workflow: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(workflow, Mapping):
        raise RepositoryReadinessError("github_actions_permissions_invalid")
    allowed = value.get("allowed_actions")
    default_permissions = workflow.get("default_workflow_permissions")
    approve = workflow.get("can_approve_pull_request_reviews")
    if (
        not isinstance(value.get("enabled"), bool)
        or allowed not in {"all", "local_only", "selected"}
        or default_permissions not in {"read", "write"}
        or not isinstance(approve, bool)
    ):
        raise RepositoryReadinessError("github_actions_permissions_invalid")
    return {
        "allowed_actions": allowed,
        "can_approve_pull_request_reviews": approve,
        "default_workflow_permissions": default_permissions,
        "enabled": value["enabled"],
        "github_owned_allowed": None,
        "patterns_allowed": [],
        "verified_allowed": None,
    }


def _normalize_selected_actions(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RepositoryReadinessError("github_actions_permissions_invalid")
    github_owned = value.get("github_owned_allowed")
    verified = value.get("verified_allowed")
    patterns = value.get("patterns_allowed")
    if (
        not isinstance(github_owned, bool)
        or not isinstance(verified, bool)
        or not isinstance(patterns, list)
        or any(not isinstance(item, str) for item in patterns)
    ):
        raise RepositoryReadinessError("github_actions_permissions_invalid")
    return {
        "github_owned_allowed": github_owned,
        "patterns_allowed": sorted(patterns),
        "verified_allowed": verified,
    }


def _normalize_environment(value: Any, variables: Any, secrets: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RepositoryReadinessError("github_environment_response_invalid")
    policy = _mapping(value.get("deployment_branch_policy"))
    protection_rules = value.get("protection_rules")
    if not isinstance(protection_rules, list):
        raise RepositoryReadinessError("github_environment_response_invalid")
    reviewer_count = 0
    for rule in protection_rules:
        if not isinstance(rule, Mapping):
            raise RepositoryReadinessError("github_environment_response_invalid")
        if rule.get("type") != "required_reviewers":
            continue
        reviewers = rule.get("reviewers")
        if not isinstance(reviewers, list):
            raise RepositoryReadinessError("github_environment_response_invalid")
        reviewer_count += len(reviewers)
    return {
        "custom_branch_policies": policy.get("custom_branch_policies"),
        "exists": True,
        "protected_branches": policy.get("protected_branches"),
        "reviewer_count": reviewer_count,
        "secrets": sorted(_inventory_names(secrets, "secrets")),
        "variables": sorted(_inventory_names(variables, "variables")),
    }


def _normalize_runners(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping) or not isinstance(value.get("runners"), list):
        raise RepositoryReadinessError("github_runner_response_invalid")
    runners = value["runners"]
    total = value.get("total_count")
    if isinstance(total, bool) or not isinstance(total, int) or total != len(runners):
        raise RepositoryReadinessError("github_runner_inventory_truncated")
    result: list[dict[str, Any]] = []
    for runner in runners:
        if not isinstance(runner, Mapping):
            raise RepositoryReadinessError("github_runner_response_invalid")
        labels = runner.get("labels")
        if not isinstance(labels, list):
            raise RepositoryReadinessError("github_runner_response_invalid")
        result.append(
            {
                "busy": runner.get("busy") is True,
                "labels": sorted(
                    label["name"]
                    for label in labels
                    if isinstance(label, Mapping) and isinstance(label.get("name"), str)
                ),
                "name": runner.get("name"),
                "status": runner.get("status"),
            }
        )
    return result


def _inventory_by_path(value: Any, key: str, path_key: str, value_key: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not isinstance(value.get(key), list):
        raise RepositoryReadinessError("github_inventory_response_invalid")
    items = value[key]
    total = value.get("total_count")
    if isinstance(total, bool) or not isinstance(total, int) or total != len(items):
        raise RepositoryReadinessError("github_inventory_truncated")
    result: dict[str, str] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise RepositoryReadinessError("github_inventory_response_invalid")
        path, state = item.get(path_key), item.get(value_key)
        if not isinstance(path, str) or not isinstance(state, str) or path in result:
            raise RepositoryReadinessError("github_inventory_response_invalid")
        result[path] = state
    return result


def _inventory_names(value: Any, key: str) -> set[str]:
    if not isinstance(value, Mapping) or not isinstance(value.get(key), list):
        raise RepositoryReadinessError("github_inventory_response_invalid")
    items = value[key]
    total = value.get("total_count")
    if isinstance(total, bool) or not isinstance(total, int) or total != len(items):
        raise RepositoryReadinessError("github_inventory_truncated")
    result: set[str] = set()
    for item in items:
        name = item.get("name") if isinstance(item, Mapping) else None
        if not isinstance(name, str) or not name or name in result:
            raise RepositoryReadinessError("github_inventory_response_invalid")
        result.add(name)
    return result


def _nested_string(value: Any, *keys: str) -> str:
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            raise RepositoryReadinessError("github_repository_response_invalid")
        current = current.get(key)
    if not isinstance(current, str):
        raise RepositoryReadinessError("github_repository_response_invalid")
    return current


def _enabled(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("enabled") is True



def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


__all__ = ["GITHUB_API_VERSION", "GitHubRepositoryAdminClient"]
