#!/usr/bin/env python3
"""Check a v0.2.5 EcoreX runtime dependency manifest."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import tarfile
import zipfile
from typing import Any


SCHEMA_VERSION = "v0.2.5-runtime-manifest-v1"
PLATFORM_PYTHON_STATUS = {
    "windows-x64": {"bundled"},
    "macos-universal": {"bundled", "installer-bundled"},
    "linux-service": {"bundled", "external-required"},
}
EXPECTED_PACKAGE_BY_MODULE = {
    "aiohttp": "aiohttp",
    "requests": "requests",
    "chardet": "chardet",
    "numpy": "numpy",
    "PIL": "Pillow",
    "pypdf": "pypdf",
    "pdfminer": "pdfminer.six",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "openpyxl": "openpyxl",
    "xlsxwriter": "xlsxwriter",
    "markdownify": "markdownify",
    "reportlab": "reportlab",
    "fitz": "PyMuPDF",
    "rapidocr_onnxruntime": "rapidocr-onnxruntime",
    "dotenv": "python-dotenv",
    "yaml": "PyYAML",
    "croniter": "croniter",
    "click": "click",
    "qrcode": "qrcode",
    "json_repair": "json-repair",
    "playwright": "playwright",
    "lark_oapi": "lark-oapi",
    "web": "web.py",
    "legacy_cgi": "legacy-cgi",
}
PYTHON_PACKAGE_PATH_PREFIXES = (
    "python/Lib/site-packages/",
    "python/lib/python3.11/site-packages/",
    "venv/Lib/site-packages/",
    "venv/lib/python3.11/site-packages/",
)
PYTHON_EXECUTABLE_PATHS = {
    "windows-x64": {"python/python.exe", "venv/Scripts/python.exe"},
    "macos-universal": {"python/bin/python", "python/bin/python3", "venv/bin/python"},
    "linux-service": {"python/bin/python", "python/bin/python3", "venv/bin/python"},
}
EXECUTABLE_PATH_PREFIXES = (
    "bin/",
    "tools/bin/",
    "node/",
    "node/bin/",
    "tools/node/",
    "tools/node/bin/",
    "node_modules/.bin/",
    "tools/lark-cli/bin/",
    "tools/lark-cli/node_modules/.bin/",
    "python/Lib/site-packages/playwright/driver/",
)
NATIVE_BIN_PATH_PREFIXES = (
    "tools/bin/",
    "tools/poppler/bin/",
    "tools/libreoffice/program/",
    "tools/tesseract/",
)
OFFICE_MODULES = ("pypdf", "pdfminer", "docx", "pptx", "openpyxl", "xlsxwriter", "markdownify", "reportlab", "fitz")
OCR_MODULES = ("PIL", "rapidocr_onnxruntime")


class ManifestError(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def load_manifest(path: pathlib.Path) -> dict[str, Any]:
    raw = path.read_bytes()
    require(not raw.startswith(b"\xef\xbb\xbf"), f"{path} has a UTF-8 BOM")
    data = json.loads(raw.decode("utf-8"))
    require(isinstance(data, dict), "manifest must be a JSON object")
    return data


def require_relative_manifest_paths(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            next_path = f"{path}.{key}"
            if key == "path":
                require(isinstance(item, str) and item.strip(), f"{next_path} must be a nonempty string")
            if key == "archives":
                require(isinstance(item, list) and item, f"{next_path} must be a nonempty list")
                require(all(isinstance(entry, str) and entry.strip() for entry in item), f"{next_path} must contain only nonempty strings")
            if key in {"path", "archives"}:
                require_relative_manifest_paths(item, next_path)
            else:
                require_relative_manifest_paths(item, next_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            require_relative_manifest_paths(item, f"{path}[{index}]")
    elif isinstance(value, str) and (path.endswith(".path") or ".archives" in path):
        normalized = value.replace("\\", "/")
        require(not pathlib.PurePosixPath(normalized).is_absolute(), f"{path} must be relative")
        require(":" not in normalized, f"{path} must not contain a drive or URL separator")
        require(not normalized.startswith("../") and "/../" not in normalized, f"{path} must not traverse parents")


def require_manifest_references_exist(value: Any, *, runtime_root: pathlib.Path, package_root: pathlib.Path, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "path" and isinstance(item, str):
                target = runtime_root / item.replace("\\", "/")
                require(target.exists(), f"{path}.{key} does not exist: {item}")
                if (
                    path.endswith(".runtimeDependencies.python")
                    or ".executables." in path
                    or ".nativeBins." in path
                    or ".toolFiles." in path
                    or ".pythonPackages." in path
                ):
                    require(target.is_file(), f"{path}.{key} must reference a file: {item}")
            elif key == "archives":
                require_manifest_references_exist(item, runtime_root=runtime_root, package_root=package_root, path=f"{path}.{key}")
            else:
                require_manifest_references_exist(item, runtime_root=runtime_root, package_root=package_root, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str) and ".archives" in path:
                target = package_root / item.replace("\\", "/")
                require(target.is_file(), f"{path}[{index}] does not exist or is not a file: {item}")
            else:
                require_manifest_references_exist(item, runtime_root=runtime_root, package_root=package_root, path=f"{path}[{index}]")


def require_status_evidence(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        status = value.get("status")
        if status == "bundled":
            require(isinstance(value.get("path"), str) and value.get("path").strip(), f"{path} bundled status requires path")
        elif status == "installer-bundled":
            archives = value.get("archives")
            wheelhouse = value.get("wheelhouse")
            has_archives = isinstance(archives, list) and bool(archives)
            has_wheelhouse = ".pythonPackages." in path and isinstance(wheelhouse, list) and bool(wheelhouse)
            require(has_archives or has_wheelhouse, f"{path} installer-bundled status requires archives or wheelhouse evidence")
            if ".pythonPackages." not in path:
                require(has_archives, f"{path} installer-bundled executable/runtime status requires archives evidence")
        for key, item in value.items():
            require_status_evidence(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            require_status_evidence(item, f"{path}[{index}]")


def normalized_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def archive_distribution(path: str) -> str:
    name = pathlib.PurePosixPath(path.replace("\\", "/")).name
    if not name.lower().endswith(".whl") or "-" not in name:
        return ""
    return normalized_distribution_name(name.split("-", 1)[0])


def readable_zip_members(path: pathlib.Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            return [name for name in archive.namelist() if name and not name.endswith("/")]
    except Exception:
        return []


def readable_tar_members(path: pathlib.Path) -> list[str]:
    try:
        with tarfile.open(path, "r:*") as archive:
            return [member.name for member in archive.getmembers() if member.isfile()]
    except Exception:
        return []


def wheel_file_matches(path: pathlib.Path, expected_dist: str) -> bool:
    members = readable_zip_members(path)
    if not any(name.endswith(".dist-info/WHEEL") for name in members):
        return False
    dist_info_dirs = [
        pathlib.PurePosixPath(name.replace("\\", "/")).parts[0]
        for name in members
        if ".dist-info/" in name.replace("\\", "/")
    ]
    if not dist_info_dirs:
        return False
    normalized_expected = normalized_distribution_name(expected_dist)
    for dist_info in dist_info_dirs:
        base = dist_info.removesuffix(".dist-info")
        package_name = base.split("-", 1)[0]
        if normalized_distribution_name(package_name) == normalized_expected:
            return True
    return False


def cpython_archive_matches(path: pathlib.Path) -> bool:
    name = path.name.lower()
    if not (name.startswith("cpython-") and name.endswith(".tar.gz")):
        return False
    members = readable_tar_members(path)
    return any(
        re.fullmatch(r"python(?:3(?:\.\d+)?)?(?:\.exe)?", pathlib.PurePosixPath(member.replace("\\", "/")).name.lower())
        for member in members
    )


def executable_archive_matches_file(path: pathlib.Path, executable: str) -> bool:
    name = path.name.lower()
    if name.endswith(".zip"):
        members = readable_zip_members(path)
    elif name.endswith((".tgz", ".tar.gz", ".tar.xz")):
        members = readable_tar_members(path)
    else:
        return False
    allowed = {executable, f"{executable}.exe", f"{executable}.cmd", f"{executable}.sh"}
    return any(pathlib.PurePosixPath(member.replace("\\", "/")).name.lower() in allowed for member in members)


def require_manifest_archive_contents(data: dict[str, Any], package_root: pathlib.Path) -> None:
    deps = data.get("runtimeDependencies") or {}
    python = deps.get("python") or {}
    if python.get("status") == "installer-bundled":
        for archive in python.get("archives") or []:
            path = package_root / str(archive).replace("\\", "/")
            require(cpython_archive_matches(path), f"python installer archive is not a readable CPython payload: {archive}")

    for executable, metadata in (deps.get("executables") or {}).items():
        if isinstance(metadata, dict) and metadata.get("status") == "installer-bundled":
            for archive in metadata.get("archives") or []:
                path = package_root / str(archive).replace("\\", "/")
                require(executable_archive_matches_file(path, str(executable)), f"installer archive for {executable} lacks executable payload: {archive}")

    for module, metadata in (deps.get("pythonPackages") or {}).items():
        if isinstance(metadata, dict) and metadata.get("status") == "installer-bundled":
            expected_package = EXPECTED_PACKAGE_BY_MODULE.get(str(module)) or str(metadata.get("package") or module)
            expected_dist = normalized_distribution_name(expected_package)
            for archive in metadata.get("archives") or []:
                path = package_root / str(archive).replace("\\", "/")
                require(wheel_file_matches(path, expected_dist), f"wheel archive for {module} lacks matching wheel metadata: {archive}")


def module_path_matches(module: str, path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    lowered = normalized.lower()
    module_path = module.replace(".", "/")
    module_lower = module_path.lower()
    for prefix in PYTHON_PACKAGE_PATH_PREFIXES:
        prefix_lower = prefix.lower()
        if not lowered.startswith(prefix_lower):
            continue
        remainder = normalized[len(prefix) :].strip("/")
        remainder_lower = remainder.lower()
        return (
            remainder_lower == module_lower + ".py"
            or remainder_lower == module_lower + "/__init__.py"
        )
    return False


def python_archive_architecture_paths(archives: list[str]) -> dict[str, set[str]]:
    architecture_paths: dict[str, set[str]] = {"mac-arm64": set(), "mac-x64": set()}
    for archive in archives:
        name = pathlib.PurePosixPath(archive.replace("\\", "/")).name.lower()
        if not (name.startswith("cpython-") and name.endswith(".tar.gz")):
            continue
        matches = []
        if "aarch64" in name or "arm64" in name:
            matches.append("mac-arm64")
        if "x86_64" in name or "x64" in name or "amd64" in name:
            matches.append("mac-x64")
        if len(matches) == 1:
            architecture_paths[matches[0]].add(archive)
    return architecture_paths


def python_path_matches(path: str, platform: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    allowed = PYTHON_EXECUTABLE_PATHS.get(platform, set())
    return normalized in allowed


def basename(path: str) -> str:
    return pathlib.PurePosixPath(path.replace("\\", "/")).name.lower()


def prefixed(path: str, prefixes: tuple[str, ...]) -> bool:
    lowered = path.replace("\\", "/").strip("/").lower()
    return any(lowered.startswith(prefix.lower()) for prefix in prefixes)


def executable_path_matches(name: str, path: str, platform: str) -> bool:
    base = basename(path)
    allowed = {name}
    if platform.startswith("windows"):
        allowed.update({f"{name}.exe", f"{name}.cmd"})
    else:
        allowed.add(f"{name}.sh")
    return base in allowed and prefixed(path, EXECUTABLE_PATH_PREFIXES)


def native_bin_path_matches(name: str, path: str, platform: str) -> bool:
    base = basename(path)
    allowed = {name}
    if platform.startswith("windows"):
        allowed.add(f"{name}.exe")
    return base in allowed and prefixed(path, NATIVE_BIN_PATH_PREFIXES)


def executable_archive_matches(name: str, archive: str) -> bool:
    normalized = archive.replace("\\", "/").strip("/")
    base = basename(normalized)
    is_archive = base.endswith((".zip", ".tgz", ".tar.gz", ".tar.xz"))
    if not is_archive:
        return False
    if name in {"node", "npm", "npx"}:
        return normalized.lower().startswith("node/") and "node" in base
    return name == "lark-cli" and normalized.lower().startswith("tools/lark-cli/")


def require_runtime_dependency_identity(deps: dict[str, Any], platform: str) -> None:
    python = deps.get("python") or {}
    if python.get("status") == "bundled":
        require(python_path_matches(str(python.get("path") or ""), platform), "bundled Python path does not match runtime interpreter layout")

    executables = deps.get("executables") or {}
    for name, metadata in executables.items():
        if not isinstance(metadata, dict):
            continue
        if metadata.get("status") == "bundled":
            require(executable_path_matches(str(name), str(metadata.get("path") or ""), platform), f"bundled executable {name} path does not match executable identity")
        if metadata.get("status") == "installer-bundled":
            archives = metadata.get("archives") or []
            require(all(executable_archive_matches(str(name), archive) for archive in archives), f"installer archive evidence for {name} does not match executable identity")

    native_bins = deps.get("nativeBins") or {}
    for name, metadata in native_bins.items():
        if isinstance(metadata, dict) and metadata.get("status") == "bundled":
            require(native_bin_path_matches(str(name), str(metadata.get("path") or ""), platform), f"native bin {name} path does not match binary identity")

    tongxin = (deps.get("toolFiles") or {}).get("tongxinCli") or {}
    if tongxin.get("status") == "bundled":
        require(
            str(tongxin.get("path") or "").replace("\\", "/").strip("/") == "tools/tongxin/xin_agent_cli.py",
            "tongxinCli path must be tools/tongxin/xin_agent_cli.py",
        )


def packages_are_bundled(packages: dict[str, Any], modules: tuple[str, ...]) -> bool:
    return all((packages.get(module) or {}).get("status") == "bundled" for module in modules)


def allowed_package_statuses(platform: str) -> set[str]:
    if platform == "windows-x64":
        return {"bundled"}
    if platform == "macos-universal":
        return {"bundled", "installer-bundled"}
    return {"bundled", "external-required"}


def package_missing(packages: dict[str, Any], modules: tuple[str, ...], platform: str) -> list[str]:
    allowed = allowed_package_statuses(platform)
    return [module for module in modules if (packages.get(module) or {}).get("status") not in allowed]


def expected_status(*, missing: list[str], all_bundled: bool) -> str:
    if missing:
        return "missing_dependency"
    return "ready" if all_bundled else "install-ready"


def require_tool_state(state: dict[str, Any], tool_name: str, expected: str, missing: list[str]) -> None:
    actual_status = str(state.get("status") or "")
    actual_missing = sorted(str(item) for item in (state.get("missingDependencies") or []))
    expected_missing = sorted(missing)
    require(actual_status == expected, f"{tool_name} status must be {expected}, got {actual_status}")
    require(actual_missing == expected_missing, f"{tool_name} missingDependencies must be {expected_missing}, got {actual_missing}")


def require_tool_dependency_consistency(deps: dict[str, Any], states: dict[str, Any], platform: str) -> None:
    packages = deps.get("pythonPackages") or {}
    executables = deps.get("executables") or {}
    tool_files = deps.get("toolFiles") or {}

    office_missing = package_missing(packages, OFFICE_MODULES, platform)
    require_tool_state(
        states.get("office_pdf") or {},
        "office_pdf",
        expected_status(missing=office_missing, all_bundled=packages_are_bundled(packages, OFFICE_MODULES)),
        office_missing,
    )

    ocr_missing = package_missing(packages, OCR_MODULES, platform)
    require_tool_state(
        states.get("ocr") or {},
        "ocr",
        expected_status(missing=ocr_missing, all_bundled=packages_are_bundled(packages, OCR_MODULES)),
        ocr_missing,
    )

    browser_missing = []
    if (executables.get("node") or {}).get("status") not in {"bundled", "installer-bundled"}:
        browser_missing.append("node")
    if (executables.get("npx") or {}).get("status") not in {"bundled", "installer-bundled"}:
        browser_missing.append("npx")
    browser_missing.extend(package_missing(packages, ("playwright",), platform))
    browser_all_bundled = (
        (executables.get("node") or {}).get("status") == "bundled"
        and (executables.get("npx") or {}).get("status") == "bundled"
        and packages_are_bundled(packages, ("playwright",))
    )
    require_tool_state(
        states.get("browser_mcp") or {},
        "browser_mcp",
        expected_status(missing=browser_missing, all_bundled=browser_all_bundled),
        browser_missing,
    )

    lark_status = (executables.get("lark-cli") or {}).get("status")
    feishu_package_missing = package_missing(packages, ("lark_oapi",), platform)
    if lark_status == "missing":
        require_tool_state(states.get("feishu_cli") or {}, "feishu_cli", "discovery-only", feishu_package_missing)
    else:
        feishu_missing = ([] if lark_status in {"bundled", "installer-bundled"} else ["lark-cli"]) + feishu_package_missing
        feishu_all_bundled = lark_status == "bundled" and packages_are_bundled(packages, ("lark_oapi",))
        require_tool_state(
            states.get("feishu_cli") or {},
            "feishu_cli",
            expected_status(missing=feishu_missing, all_bundled=feishu_all_bundled),
            feishu_missing,
        )

    tongxin_status = (tool_files.get("tongxinCli") or {}).get("status")
    expected_tongxin = "ready" if tongxin_status == "bundled" else "configure-required"
    expected_tongxin_missing = [] if tongxin_status == "bundled" else ["xin_agent_cli.py"]
    require_tool_state(states.get("tongxin_cli") or {}, "tongxin_cli", expected_tongxin, expected_tongxin_missing)


def require_python_package_archive_semantics(packages: dict[str, Any], platform: str) -> None:
    for module, metadata in packages.items():
        if not isinstance(metadata, dict):
            continue
        status = metadata.get("status")
        if platform == "windows-x64":
            require(status in {"bundled", "missing"}, f"windows python package {module} cannot use {status} evidence")
        elif platform == "macos-universal":
            require(status in {"bundled", "installer-bundled", "missing"}, f"macOS python package {module} cannot use {status} evidence")
        elif platform == "linux-service":
            require(status in {"bundled", "external-required", "missing"}, f"linux-service python package {module} cannot use {status} evidence")
        expected_package = EXPECTED_PACKAGE_BY_MODULE.get(module)
        if expected_package:
            require(metadata.get("package") == expected_package, f"python package {module} package field must be {expected_package}")
        if metadata.get("status") == "bundled":
            require(module_path_matches(module, str(metadata.get("path") or "")), f"python package {module} bundled path does not match module")
            continue
        if metadata.get("status") != "installer-bundled":
            continue
        package_name = str(metadata.get("package") or module)
        expected_dist = normalized_distribution_name(package_name)
        archives = metadata.get("archives") or []
        require(isinstance(archives, list) and archives, f"python package {module} installer-bundled requires wheel archives")
        for archive in archives:
            require(isinstance(archive, str), f"python package {module} archive must be a string")
            require(archive.lower().endswith(".whl"), f"python package {module} archive must be a wheel: {archive}")
            require(archive_distribution(archive) == expected_dist, f"python package {module} archive distribution mismatch: {archive}")
        if platform == "macos-universal":
            normalized_archives = [item.replace("\\", "/") for item in archives]
            require(any("/wheelhouse/mac-arm64/" in f"/{item}" for item in normalized_archives), f"python package {module} missing mac-arm64 wheel archive")
            require(any("/wheelhouse/mac-x64/" in f"/{item}" for item in normalized_archives), f"python package {module} missing mac-x64 wheel archive")


def check_manifest(
    data: dict[str, Any],
    *,
    expected_platform: str | None = None,
    expected_version: str | None = None,
    runtime_root: pathlib.Path | None = None,
    package_root: pathlib.Path | None = None,
) -> None:
    require(data.get("schemaVersion") == SCHEMA_VERSION, f"schemaVersion must be {SCHEMA_VERSION}")
    require(data.get("product") == "EcoreX", "product must be EcoreX")
    require_relative_manifest_paths(data)
    require_status_evidence(data)
    if runtime_root is not None and package_root is not None:
        require_manifest_references_exist(data, runtime_root=runtime_root, package_root=package_root)
        require_manifest_archive_contents(data, package_root)
    if expected_version:
        require(str(data.get("version") or "") == expected_version, f"version must be {expected_version}")
    platform = str(data.get("platform") or "")
    if expected_platform:
        require(platform == expected_platform, f"platform must be {expected_platform}")
    require(platform in PLATFORM_PYTHON_STATUS, f"unknown platform {platform!r}")

    policy = data.get("dependencyPolicy") or {}
    require(policy.get("ownedRuntimeDefault") is True, "ownedRuntimeDefault must be true")
    require(policy.get("systemPathDefault") is False, "systemPathDefault must be false")
    require(policy.get("readyRequiresProbe") is True, "readyRequiresProbe must be true")

    deps = data.get("runtimeDependencies") or {}
    require_runtime_dependency_identity(deps, platform)
    python = deps.get("python") or {}
    require(python.get("status") in PLATFORM_PYTHON_STATUS[platform], f"{platform} python status is not acceptable: {python.get('status')}")
    if platform == "macos-universal" and python.get("status") == "installer-bundled":
        architecture_paths = python_archive_architecture_paths(list(python.get("archives") or []))
        require(architecture_paths["mac-arm64"], "macOS Python archive must include mac-arm64 cpython archive")
        require(architecture_paths["mac-x64"], "macOS Python archive must include mac-x64 cpython archive")
        require(
            architecture_paths["mac-arm64"].isdisjoint(architecture_paths["mac-x64"]),
            "macOS Python archives must use distinct architecture-specific files",
        )

    packages = deps.get("pythonPackages") or {}
    require(isinstance(packages, dict) and packages, "pythonPackages must be populated")
    require_python_package_archive_semantics(packages, platform)
    for required in ("web", "chardet", "numpy", "playwright", "pypdf", "docx", "pptx", "openpyxl", "fitz", "lark_oapi"):
        require(required in packages, f"pythonPackages missing {required}")
        status = packages[required].get("status")
        if platform == "windows-x64":
            require(status == "bundled", f"windows package {required} must be bundled, got {status}")
        elif platform == "macos-universal":
            require(status in {"bundled", "installer-bundled"}, f"macOS package {required} cannot use external-required evidence")
        elif platform == "linux-service":
            require(status in {"bundled", "external-required"}, f"linux-service package {required} cannot use installer-bundled evidence")
        else:
            require(status in {"bundled", "installer-bundled", "external-required"}, f"{platform} package {required} unexpectedly missing")
        if platform == "macos-universal" and status == "installer-bundled":
            wheelhouse = set(packages[required].get("wheelhouse") or [])
            require({"mac-arm64", "mac-x64"}.issubset(wheelhouse), f"macOS package {required} must include both wheelhouses")
            require(isinstance(packages[required].get("archives"), list) and packages[required].get("archives"), f"macOS package {required} must include wheel archive evidence")

    executables = deps.get("executables") or {}
    for name in ("node", "npm", "npx", "lark-cli"):
        require(name in executables, f"executables missing {name}")
        require(executables[name].get("status") in {"bundled", "installer-bundled", "missing"}, f"bad executable status for {name}")

    native_bins = deps.get("nativeBins") or {}
    for name in ("pdfinfo", "pdftoppm", "soffice", "tesseract"):
        require(name in native_bins, f"nativeBins missing {name}")
        require(native_bins[name].get("status") in {"bundled", "missing"}, f"bad native bin status for {name}")

    tool_files = deps.get("toolFiles") or {}
    require("tongxinCli" in tool_files, "toolFiles missing tongxinCli")
    require(tool_files["tongxinCli"].get("status") in {"bundled", "missing"}, "bad tool file status for tongxinCli")

    states = data.get("toolStates") or {}
    require(isinstance(states, dict) and states, "toolStates must be populated")
    for tool_name, state in states.items():
        status = str(state.get("status") or "")
        missing = list(state.get("missingDependencies") or [])
        require(status, f"{tool_name} missing status")
        require(status != "ready" or not missing, f"{tool_name} is ready with missing dependencies: {missing}")
    require_tool_dependency_consistency(deps, states, platform)
    if executables.get("node", {}).get("status") == "missing" or executables.get("npx", {}).get("status") == "missing":
        require(states.get("browser_mcp", {}).get("status") != "ready", "browser_mcp cannot be ready when node/npx is missing")
    if executables.get("lark-cli", {}).get("status") == "missing":
        require(states.get("feishu_cli", {}).get("status") != "ready", "feishu_cli cannot be ready when lark-cli is missing")

    gate = data.get("releaseGate") or {}
    require(gate.get("installReady") is True, "releaseGate.installReady must be true")
    require(gate.get("readyToolsNeverHaveMissingDependencies") is True, "readyToolsNeverHaveMissingDependencies must be true")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("--platform", default="")
    parser.add_argument("--version", default="")
    parser.add_argument("--runtime-root", default="")
    parser.add_argument("--package-root", default="")
    args = parser.parse_args(argv)
    path = pathlib.Path(args.manifest)
    runtime_root = pathlib.Path(args.runtime_root).resolve() if args.runtime_root else path.resolve().parent
    package_root = pathlib.Path(args.package_root).resolve() if args.package_root else runtime_root.parent
    try:
        check_manifest(
            load_manifest(path),
            expected_platform=args.platform or None,
            expected_version=args.version or None,
            runtime_root=runtime_root,
            package_root=package_root,
        )
    except ManifestError as exc:
        print(f"FAIL {path}: {exc}", file=sys.stderr)
        return 1
    print(f"PASS {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
