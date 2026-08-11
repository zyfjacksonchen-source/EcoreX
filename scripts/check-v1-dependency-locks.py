#!/usr/bin/env python3
"""Fail CI/Candidate when Python or npm dependency resolution can float."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from typing import Iterable

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
LOCK_ROOT = ROOT / "requirements" / "locks"
PROFILES = ("bootstrap", "cloud", "dev", "platform-stage", "runtime")
GENERATOR_VERSION = "0.11.7"
PLATFORM_PACK_DEPENDENCIES = {
    "greenlet": "3.4.0",
    "numpy": "2.4.6",
    "onnxruntime": "1.23.2",
    "openpyxl": "3.1.5",
    "playwright": "1.52.0",
    "pyee": "13.0.1",
    "pypdf": "6.9.1",
    "python-docx": "1.2.0",
    "python-pptx": "1.0.2",
    "rapidocr-onnxruntime": "1.4.4",
    "reportlab": "4.4.9",
}
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)")
_ACTION_PIN = re.compile(
    r"^\s*uses:\s*([^\s@]+)@([0-9a-f]{40})\s+#\s+(\S+)\s*$"
)
ACTION_LOCK_RELATIVE = Path("requirements/locks/github-actions.json")
_ACTION_LOCK_KEYS = {
    "actions",
    "lock_type",
    "minimum_runner_version",
    "schema_version",
}
_ACTION_ENTRY_KEYS = {
    "commit_sha",
    "release",
    "release_url",
    "repository",
    "runtime",
    "verification",
}
_MINIMUM_NODE24_RUNNER_VERSION = "2.327.1"
_PLATFORM_STAGE_RUNNER_RELATIVE = Path("scripts/run-v1-platform-stage-step.py")
_PLATFORM_STAGE_RUNNER_AST_SHA256 = (
    "b9b1d3bcfd621109f85bc8f345bc239e8b41d24ef34b8e88b4dd575638fde9aa"
)
_PLATFORM_STAGE_WORKFLOW_BINDINGS = (
    "run: python scripts/run-v1-platform-stage-step.py clean-check",
    "run: python scripts/run-v1-platform-stage-step.py install-dependencies",
    "run: python scripts/run-v1-platform-stage-step.py build-web",
)
_PLATFORM_STAGE_COMMAND_CATALOG = {
    "install-dependencies": (
        (
            "install locked platform-stage Python profile",
            "python",
            (
                "scripts/install-v1-python-profile.py",
                "--profile",
                "platform-stage",
            ),
            ".",
        ),
        (
            "validate Python dependency locks",
            "python",
            ("scripts/check-v1-dependency-locks.py",),
            ".",
        ),
        (
            "install managed Chromium",
            "python",
            ("-m", "playwright", "install", "chromium"),
            ".",
        ),
    ),
    "build-web": (
        ("install locked Web dependencies", "npm", ("ci",), "desktop"),
        ("typecheck Web", "npm", ("run", "typecheck"), "desktop"),
        ("build Web", "npm", ("run", "build"), "desktop"),
        ("test built Web", "npm", ("run", "test:v1"), "desktop"),
        (
            "validate tested Web content addresses",
            "python",
            (
                "scripts/check-v1-reproducibility.py",
                "--web-dist",
                "desktop/dist",
            ),
            ".",
        ),
        (
            "validate tested Web bundle",
            "node",
            ("tools/check-v1-bundle.mjs", "dist"),
            "desktop",
        ),
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--write-manifest", action="store_true")
    return parser


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_value() -> dict[str, object]:
    return {
        "schema_version": 1,
        "lock_type": "ecorex-python-hash-lock-set",
        "python": "3.11.9",
        "generator": {
            "name": "uv",
            "version": GENERATOR_VERSION,
            "index": "https://pypi.org/simple",
            "universal": True,
            "hashes": True,
        },
        "profiles": [
            {
                "profile": profile,
                "input": f"{profile}.in",
                "input_sha256": _sha256(LOCK_ROOT / f"{profile}.in"),
                "lock": f"{profile}.lock",
                "lock_sha256": _sha256(LOCK_ROOT / f"{profile}.lock"),
            }
            for profile in PROFILES
        ],
    }


def _write_manifest() -> None:
    result = subprocess.run(
        ("uv", "--version"), capture_output=True, text=True, check=False, shell=False
    )
    if result.returncode != 0 or not result.stdout.strip().startswith(
        f"uv {GENERATOR_VERSION} (9d177269e "
    ):
        # The exact binary provenance is intentionally stricter than merely
        # matching a semantic version. Maintainers must review and update this
        # gate when the resolver itself changes.
        raise ValueError("dependency_lock_generator_unreviewed")
    payload = json.dumps(
        _manifest_value(), ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    (LOCK_ROOT / "manifest.json").write_bytes(payload)


def _logical_requirements(path: Path) -> tuple[str, ...]:
    entries: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or (stripped.startswith("#") and not pending):
            continue
        continued = stripped.endswith("\\")
        if continued:
            stripped = stripped[:-1].strip()
        pending = f"{pending} {stripped}".strip()
        if not continued:
            entries.append(pending)
            pending = ""
    if pending or not entries:
        raise ValueError(f"dependency_lock_syntax_invalid:{path.name}")
    return tuple(entries)


def _exact_version(requirement: Requirement) -> str:
    specifiers = tuple(requirement.specifier)
    if (
        requirement.url is not None
        or len(specifiers) != 1
        or specifiers[0].operator != "=="
        or "*" in specifiers[0].version
    ):
        raise ValueError(f"dependency_not_exact:{requirement.name}")
    return specifiers[0].version


def _lock_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for entry in _logical_requirements(path):
        requirement_text = entry.split(" --hash=", 1)[0].strip()
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement:
            raise ValueError(f"dependency_lock_requirement_invalid:{path.name}") from None
        version = _exact_version(requirement)
        name = canonicalize_name(requirement.name)
        hashes = _HASH.findall(entry)
        if not hashes or len(hashes) != len(set(hashes)):
            raise ValueError(f"dependency_lock_hash_invalid:{path.name}:{name}")
        if name in versions:
            raise ValueError(f"dependency_lock_duplicate:{path.name}:{name}")
        versions[name] = version
    return versions


def _requirements(values: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            raise ValueError("pyproject_dependency_invalid") from None
        version = _exact_version(requirement)
        name = canonicalize_name(requirement.name)
        previous = result.setdefault(name, version)
        if previous != version:
            raise ValueError(f"pyproject_dependency_conflict:{name}")
    return result


def _require_profile(
    profiles: dict[str, dict[str, str]],
    profile: str,
    expected: dict[str, str],
) -> None:
    observed = profiles[profile]
    for name, version in expected.items():
        if observed.get(name) != version:
            raise ValueError(f"dependency_lock_direct_mismatch:{profile}:{name}")


def _validate_python(repo: Path) -> dict[str, dict[str, str]]:
    manifest_path = LOCK_ROOT / "manifest.json"
    expected_manifest = _manifest_value()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("dependency_lock_manifest_invalid") from None
    if manifest != expected_manifest:
        raise ValueError("dependency_lock_manifest_drift")
    profiles = {
        profile: _lock_versions(LOCK_ROOT / f"{profile}.lock")
        for profile in PROFILES
    }
    project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    build = _requirements(project["build-system"]["requires"])
    runtime = _requirements(project["project"]["dependencies"])
    extras = project["project"]["optional-dependencies"]
    dev = _requirements(extras["dev"])
    image_cloud = _requirements(extras["image-cloud"])
    control_cloud = _requirements(extras["control-plane-cloud"])
    cloud = dict(runtime)
    cloud.update(image_cloud)
    cloud.update(control_cloud)
    _require_profile(profiles, "bootstrap", {"pip": "24.0", **build})
    _require_profile(profiles, "runtime", runtime)
    _require_profile(profiles, "dev", {**runtime, **dev})
    _require_profile(profiles, "cloud", cloud)
    _require_profile(
        profiles,
        "platform-stage",
        {**runtime, **PLATFORM_PACK_DEPENDENCIES},
    )
    return profiles


def _validate_node(repo: Path) -> None:
    package = json.loads((repo / "desktop" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((repo / "desktop" / "package-lock.json").read_text(encoding="utf-8"))
    packages = lock.get("packages")
    root = packages.get("") if isinstance(packages, dict) else None
    if (
        lock.get("lockfileVersion") != 3
        or lock.get("requires") is not True
        or not isinstance(root, dict)
        or root.get("dependencies") != package.get("dependencies")
        or root.get("devDependencies") != package.get("devDependencies")
        or root.get("version") != package.get("version")
    ):
        raise ValueError("npm_lock_root_mismatch")
    for name, value in packages.items():
        if not name:
            continue
        if (
            not isinstance(value, dict)
            or value.get("link") is True
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", str(value.get("version") or ""))
            or not str(value.get("resolved") or "").startswith("https://registry.npmjs.org/")
            or re.fullmatch(r"sha512-[A-Za-z0-9+/]+={0,2}", str(value.get("integrity") or "")) is None
        ):
            raise ValueError(f"npm_lock_entry_invalid:{name}")


def _load_action_lock(repo: Path) -> tuple[dict[str, tuple[str, str]], dict[str, object]]:
    path = repo / ACTION_LOCK_RELATIVE
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("github_actions_lock_invalid") from None
    canonical = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    if payload != canonical or not isinstance(value, dict) or set(value) != _ACTION_LOCK_KEYS:
        raise ValueError("github_actions_lock_invalid")
    if (
        value.get("schema_version") != 1
        or value.get("lock_type") != "ecorex-github-actions-lock"
        or value.get("minimum_runner_version") != _MINIMUM_NODE24_RUNNER_VERSION
    ):
        raise ValueError("github_actions_lock_contract_invalid")
    raw_actions = value.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ValueError("github_actions_lock_actions_invalid")
    approved: dict[str, tuple[str, str]] = {}
    observed_order: list[str] = []
    for raw in raw_actions:
        if not isinstance(raw, dict) or set(raw) != _ACTION_ENTRY_KEYS:
            raise ValueError("github_actions_lock_entry_invalid")
        repository = raw.get("repository")
        release = raw.get("release")
        commit_sha = raw.get("commit_sha")
        if (
            not isinstance(repository, str)
            or re.fullmatch(r"actions/[a-z0-9][a-z0-9-]*", repository) is None
            or re.fullmatch(r"v[1-9][0-9]*\.[0-9]+\.[0-9]+", str(release)) is None
            or re.fullmatch(r"[0-9a-f]{40}", str(commit_sha)) is None
            or raw.get("runtime") != "node24"
            or raw.get("verification") != "verified"
            or raw.get("release_url")
            != f"https://github.com/{repository}/releases/tag/{release}"
        ):
            raise ValueError(f"github_actions_lock_entry_invalid:{repository}")
        if repository in approved:
            raise ValueError(f"github_actions_lock_duplicate:{repository}")
        approved[repository] = (str(commit_sha), str(release))
        observed_order.append(repository)
    if observed_order != sorted(observed_order):
        raise ValueError("github_actions_lock_order_invalid")
    return approved, value


def _validate_platform_stage_runner(repo: Path) -> None:
    path = repo / _PLATFORM_STAGE_RUNNER_RELATIVE
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        raise ValueError("platform_stage_runner_invalid") from None
    catalog_assignments = [
        node
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "COMMAND_CATALOG"
            for target in (
                node.targets if isinstance(node, ast.Assign) else (node.target,)
            )
        )
    ]
    if len(catalog_assignments) != 1:
        raise ValueError("platform_stage_runner_catalog_invalid")
    assignment = catalog_assignments[0]
    try:
        catalog = ast.literal_eval(assignment.value)
    except (ValueError, TypeError):
        raise ValueError("platform_stage_runner_catalog_invalid") from None
    if catalog != _PLATFORM_STAGE_COMMAND_CATALOG:
        raise ValueError("platform_stage_runner_catalog_drift")
    canonical_ast = ast.dump(
        tree,
        annotate_fields=True,
        include_attributes=False,
    ).encode("utf-8")
    if hashlib.sha256(canonical_ast).hexdigest() != _PLATFORM_STAGE_RUNNER_AST_SHA256:
        raise ValueError("platform_stage_runner_implementation_drift")


def _validate_platform_stage_workflow_binding(text: str) -> None:
    lines = tuple(line.strip() for line in text.splitlines())
    runner_marker = str(_PLATFORM_STAGE_RUNNER_RELATIVE).replace("\\", "/")
    runner_lines = tuple(line for line in lines if runner_marker in line)
    if runner_lines != _PLATFORM_STAGE_WORKFLOW_BINDINGS:
        raise ValueError("workflow_stage_runner_binding_invalid")
    for binding in _PLATFORM_STAGE_WORKFLOW_BINDINGS:
        if lines.count(binding) != 1:
            raise ValueError("workflow_stage_runner_binding_invalid")
    forbidden_inline_commands = (
        "scripts/install-v1-python-profile.py",
        "scripts/check-v1-dependency-locks.py",
        "playwright install chromium",
        "npm ci",
        "npm run typecheck",
        "npm run test:v1",
        "npm run build",
    )
    if any(command in text for command in forbidden_inline_commands):
        raise ValueError("workflow_stage_runner_bypass_invalid")
    clean_checkout_marker = "          clean: true"
    if text.count(clean_checkout_marker) != 1:
        raise ValueError("workflow_stage_checkout_clean_invalid")
    clean_binding = _PLATFORM_STAGE_WORKFLOW_BINDINGS[0]
    install_binding = _PLATFORM_STAGE_WORKFLOW_BINDINGS[1]
    python_setup_markers = ("- name: Set up Python 3.11",)
    if (
        any(text.count(marker) != 1 for marker in python_setup_markers)
        or any(text.index(marker) >= text.index(clean_binding) for marker in python_setup_markers)
        or text.index(clean_binding) >= text.index(install_binding)
    ):
        raise ValueError("workflow_stage_checkout_status_gate_invalid")


def _validate_workflows(repo: Path) -> dict[str, object]:
    approved_actions, action_lock = _load_action_lock(repo)
    workflow_profiles = {
        "ecorex-v1-pr.yml": {
            "profiles": {"dev": 1},
            "npm_ci": 1,
            "node": True,
        },
        "ecorex-v1-pr-trusted.yml": {
            "profiles": {"dev": 1},
            "npm_ci": 1,
            "node": True,
        },
        "ecorex-v1-ci.yml": {
            "profiles": {"cloud": 1, "dev": 2, "platform-stage": 1},
            "npm_ci": 2,
            "node": True,
        },
        "ecorex-v1-platform-stage.yml": {
            # Dependency commands are deliberately indirect here. The exact
            # workflow bindings and the complete runner AST/catalog are
            # validated below instead of weakening the old inline contract.
            "profiles": {},
            "npm_ci": 0,
            "node": True,
        },
        # Source quality plus the isolated shared-storage and protected soak
        # jobs each install the reviewed dev/cloud pair. Candidate assembly,
        # provenance verification and signed gate finalization use the smaller
        # runtime profile. Publication is deliberately a separate workflow.
        "ecorex-v1-candidate.yml": {
            "profiles": {"cloud": 5, "dev": 3, "runtime": 4},
            "npm_ci": 1,
            "node": True,
        },
        # Public smoke verifies already-built immutable assets. It needs an
        # exact Python toolchain, but must not resolve product dependencies.
        "ecorex-v1-public-bootstrap-smoke.yml": {
            "profiles": {},
            "npm_ci": 0,
            "node": False,
        },
        "emate-v030-macos-universal.yml": {
            "profiles": {"runtime": 2},
            "npm_ci": 1,
            "node": True,
        },
        "emate-2.0-desktop-release.yml": {
            "profiles": {"runtime": 3},
            "npm_ci": 2,
            "node": True,
        },
        "ecorex-v1-online-update.yml": {
            "profiles": {},
            "npm_ci": 0,
            "node": False,
        },
    }
    workflow_root = repo / ".github" / "workflows"
    workflow_paths = tuple(
        sorted(
            (
                *workflow_root.glob("*.yml"),
                *workflow_root.glob("*.yaml"),
            ),
            key=lambda path: path.name,
        )
    )
    observed_names = {path.name for path in workflow_paths}
    expected_names = set(workflow_profiles)
    if observed_names != expected_names:
        unexpected = sorted(observed_names - expected_names)
        missing = sorted(expected_names - observed_names)
        detail = ",".join(
            [
                *(f"unexpected:{name}" for name in unexpected),
                *(f"missing:{name}" for name in missing),
            ]
        )
        raise ValueError(f"workflow_inventory_invalid:{detail}")
    workflows = {
        path.name: path.read_text(encoding="utf-8")
        for path in workflow_paths
    }
    _validate_platform_stage_workflow_binding(
        workflows["ecorex-v1-platform-stage.yml"]
    )
    _validate_platform_stage_runner(repo)
    for name, contract in workflow_profiles.items():
        text = workflows.get(name)
        if text is None or "python -m pip install" in text or re.search(r"\bnpm install\b", text):
            raise ValueError(f"workflow_dependency_install_floating:{name}")
        if text.count("npm ci") != contract["npm_ci"]:
            raise ValueError(f"workflow_npm_ci_missing:{name}")
        if 'python-version: "3.11"' in text or 'python-version: "3.11.9"' not in text:
            raise ValueError(f"workflow_python_toolchain_floating:{name}")
        has_exact_node = 'node-version: "22.23.1"' in text
        if 'node-version: "22"' in text or has_exact_node is not contract["node"]:
            raise ValueError(f"workflow_node_toolchain_floating:{name}")
        if name == "ecorex-v1-platform-stage.yml" and (
            'go-version: "1.26.5"' not in text
            or "platform-staging/bootstrap/go.mod" not in text
        ):
            raise ValueError("workflow_go_toolchain_floating:ecorex-v1-platform-stage.yml")
        for profile, count in contract["profiles"].items():
            marker = f"python scripts/install-v1-python-profile.py --profile {profile}"
            if text.count(marker) != count:
                raise ValueError(f"workflow_lock_profile_missing:{name}:{profile}")
    for name, text in workflows.items():
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if "uses:" not in line:
                continue
            match = _ACTION_PIN.fullmatch(line)
            if match is None:
                raise ValueError(f"workflow_action_not_sha_pinned:{name}")
            action, commit_sha, release = match.groups()
            approved = approved_actions.get(action)
            if approved is None:
                raise ValueError(f"workflow_action_not_approved:{name}:{action}")
            if approved != (commit_sha, release):
                raise ValueError(f"workflow_action_revision_unreviewed:{name}:{action}")
            if action == "actions/checkout" and not any(
                candidate.strip() == "persist-credentials: false"
                for candidate in lines[index + 1 : index + 8]
            ):
                raise ValueError(
                    f"workflow_checkout_persists_credentials:{name}:{index + 1}"
                )
    go_mod = (repo / "platform-staging" / "bootstrap" / "go.mod").read_text(
        encoding="utf-8"
    )
    if go_mod != "module ecorex.local/bootstrap\n\ngo 1.26.0\n":
        raise ValueError("bootstrap_go_module_drift")
    return action_lock


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo.resolve(strict=True)
    if repo != ROOT:
        raise ValueError("dependency_lock_repo_mismatch")
    if args.write_manifest:
        _write_manifest()
    profiles = _validate_python(repo)
    _validate_node(repo)
    action_lock = _validate_workflows(repo)
    result = {
        "status": "passed",
        "manifest_sha256": _sha256(LOCK_ROOT / "manifest.json"),
        "github_actions_lock_sha256": _sha256(repo / ACTION_LOCK_RELATIVE),
        "github_actions": len(action_lock["actions"]),
        "minimum_actions_runner_version": action_lock["minimum_runner_version"],
        "profiles": {name: len(values) for name, values in sorted(profiles.items())},
        "npm_packages": len(json.loads((repo / "desktop" / "package-lock.json").read_text(encoding="utf-8"))["packages"]) - 1,
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
