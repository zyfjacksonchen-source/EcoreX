#!/usr/bin/env python3
"""Fail CI/Candidate when Python or npm dependency resolution can float."""

from __future__ import annotations

import argparse
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
    "onnxruntime": "1.26.0",
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
_ACTION_PIN = re.compile(r"^\s*uses:\s*[^\s@]+@([0-9a-f]{40})(?:\s+#.*)?$")


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


def _validate_workflows(repo: Path) -> None:
    workflows = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((repo / ".github" / "workflows").glob("ecorex-v1-*.yml"))
    }
    expected_profiles = {
        "ecorex-v1-ci.yml": {"cloud": 1, "dev": 2},
        "ecorex-v1-platform-stage.yml": {"platform-stage": 1},
        # Source quality plus the isolated shared-storage and protected soak
        # jobs each install the reviewed dev/cloud pair.  Candidate assembly,
        # The provenance verifier, Candidate assembly, publication, signed
        # gate finalization and promotion use the smaller runtime profile.
        "ecorex-v1-candidate.yml": {"cloud": 3, "dev": 3, "runtime": 5},
    }
    for name, expected in expected_profiles.items():
        text = workflows.get(name)
        if text is None or "python -m pip install" in text or re.search(r"\bnpm install\b", text):
            raise ValueError(f"workflow_dependency_install_floating:{name}")
        if text.count("npm ci") < (2 if name == "ecorex-v1-ci.yml" else 1):
            raise ValueError(f"workflow_npm_ci_missing:{name}")
        if 'python-version: "3.11"' in text or 'python-version: "3.11.9"' not in text:
            raise ValueError(f"workflow_python_toolchain_floating:{name}")
        if 'node-version: "22"' in text or 'node-version: "22.23.1"' not in text:
            raise ValueError(f"workflow_node_toolchain_floating:{name}")
        if name == "ecorex-v1-platform-stage.yml" and (
            'go-version: "1.26.5"' not in text
            or "platform-staging/bootstrap/go.mod" not in text
        ):
            raise ValueError("workflow_go_toolchain_floating:ecorex-v1-platform-stage.yml")
        for profile, count in expected.items():
            marker = f"python scripts/install-v1-python-profile.py --profile {profile}"
            if text.count(marker) != count:
                raise ValueError(f"workflow_lock_profile_missing:{name}:{profile}")
    for name, text in workflows.items():
        for line in text.splitlines():
            if "uses:" in line and _ACTION_PIN.fullmatch(line) is None:
                raise ValueError(f"workflow_action_not_sha_pinned:{name}")
    go_mod = (repo / "platform-staging" / "bootstrap" / "go.mod").read_text(
        encoding="utf-8"
    )
    if go_mod != "module ecorex.local/bootstrap\n\ngo 1.26.0\n":
        raise ValueError("bootstrap_go_module_drift")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo.resolve(strict=True)
    if repo != ROOT:
        raise ValueError("dependency_lock_repo_mismatch")
    if args.write_manifest:
        _write_manifest()
    profiles = _validate_python(repo)
    _validate_node(repo)
    _validate_workflows(repo)
    result = {
        "status": "passed",
        "manifest_sha256": _sha256(LOCK_ROOT / "manifest.json"),
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
