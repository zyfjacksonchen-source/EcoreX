#!/usr/bin/env python3
"""Write the v0.2.5 EcoreX runtime dependency manifest."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import tarfile
import zipfile
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "v0.2.5-runtime-manifest-v1"

PYTHON_MODULES = {
    "aiohttp": "aiohttp",
    "requests": "requests",
    "chardet": "chardet",
    "numpy": "numpy",
    "Pillow": "PIL",
    "pypdf": "pypdf",
    "pdfminer.six": "pdfminer",
    "python-docx": "docx",
    "python-pptx": "pptx",
    "openpyxl": "openpyxl",
    "xlsxwriter": "xlsxwriter",
    "markdownify": "markdownify",
    "reportlab": "reportlab",
    "PyMuPDF": "fitz",
    "rapidocr-onnxruntime": "rapidocr_onnxruntime",
    "python-dotenv": "dotenv",
    "PyYAML": "yaml",
    "croniter": "croniter",
    "click": "click",
    "qrcode": "qrcode",
    "json-repair": "json_repair",
    "playwright": "playwright",
    "lark-oapi": "lark_oapi",
    "web.py": "web",
    "legacy-cgi": "legacy_cgi",
}


def rel(path: pathlib.Path, root: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        raise ValueError(f"path is outside manifest root: {path}")


def first_existing(paths: list[pathlib.Path]) -> pathlib.Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def first_existing_file(paths: list[pathlib.Path]) -> pathlib.Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def package_dirs(runtime: pathlib.Path) -> list[pathlib.Path]:
    return [
        runtime / "python" / "Lib" / "site-packages",
        runtime / "python" / "lib" / "python3.11" / "site-packages",
        runtime / "venv" / "Lib" / "site-packages",
        runtime / "venv" / "lib" / "python3.11" / "site-packages",
    ]


def module_exists(runtime: pathlib.Path, module: str) -> pathlib.Path | None:
    parts = module.split(".")
    for root in package_dirs(runtime):
        package = root.joinpath(*parts)
        module_file = root.joinpath(*parts).with_suffix(".py")
        package_init = package / "__init__.py"
        if package_init.is_file():
            return package_init
        if module_file.is_file():
            return module_file
    return None


def normalized_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


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


def valid_wheel_file(path: pathlib.Path) -> bool:
    members = readable_zip_members(path)
    return any(name.endswith(".dist-info/WHEEL") for name in members)


def valid_cpython_archive(path: pathlib.Path) -> bool:
    name = path.name.lower()
    if not (name.startswith("cpython-") and name.endswith(".tar.gz")):
        return False
    members = readable_tar_members(path)
    return any(
        re.fullmatch(r"python(?:3(?:\.\d+)?)?(?:\.exe)?", pathlib.PurePosixPath(member.replace("\\", "/")).name.lower())
        for member in members
    )


def valid_executable_archive(path: pathlib.Path, executable: str) -> bool:
    name = path.name.lower()
    if name.endswith(".zip"):
        members = readable_zip_members(path)
    elif name.endswith((".tgz", ".tar.gz", ".tar.xz")):
        members = readable_tar_members(path)
    else:
        return False
    allowed = {executable}
    if executable in {"node", "npm", "npx", "lark-cli"}:
        allowed.update({f"{executable}.exe", f"{executable}.cmd", f"{executable}.sh"})
    return any(pathlib.PurePosixPath(member.replace("\\", "/")).name.lower() in allowed for member in members)


def wheel_names(package_root: pathlib.Path) -> set[str]:
    names: set[str] = set()
    for wheel in package_root.rglob("*.whl"):
        if not valid_wheel_file(wheel):
            continue
        dist = wheel.name.split("-", 1)[0]
        if dist:
            names.add(normalized_distribution_name(dist))
    return names


def wheel_files_by_name(package_root: pathlib.Path) -> dict[str, list[pathlib.Path]]:
    files: dict[str, list[pathlib.Path]] = {}
    for wheel in package_root.rglob("*.whl"):
        if not valid_wheel_file(wheel):
            continue
        dist = wheel.name.split("-", 1)[0]
        if dist:
            files.setdefault(normalized_distribution_name(dist), []).append(wheel)
    return files


def wheel_names_under(root: pathlib.Path) -> set[str]:
    names: set[str] = set()
    if not root.is_dir():
        return names
    for wheel in root.glob("*.whl"):
        if not valid_wheel_file(wheel):
            continue
        dist = wheel.name.split("-", 1)[0]
        if dist:
            names.add(normalized_distribution_name(dist))
    return names


def is_archive_file(path: pathlib.Path) -> bool:
    name = path.name.lower()
    return name.endswith((".zip", ".tgz", ".tar.gz", ".tar.xz"))


def python_archive_architectures(archives: list[pathlib.Path]) -> list[str]:
    architectures: set[str] = set()
    for archive in archives:
        name = archive.name.lower()
        matches = []
        if "aarch64" in name or "arm64" in name:
            matches.append("mac-arm64")
        if "x86_64" in name or "x64" in name or "amd64" in name:
            matches.append("mac-x64")
        if len(matches) == 1:
            architectures.add(matches[0])
    return sorted(architectures)


def python_archive_files(package_root: pathlib.Path) -> list[pathlib.Path]:
    archives: dict[pathlib.Path, pathlib.Path] = {}
    for root in (package_root / "python", package_root):
        if not root.is_dir():
            continue
        for path in root.glob("cpython-*.tar.gz"):
            if path.is_file() and valid_cpython_archive(path):
                archives[path.resolve()] = path
    return sorted(archives.values())


def python_status(runtime: pathlib.Path, package_root: pathlib.Path, platform: str) -> dict[str, Any]:
    bundled = first_existing_file([
        runtime / "python" / "python.exe",
        runtime / "python" / "bin" / "python3",
        runtime / "venv" / "Scripts" / "python.exe",
        runtime / "venv" / "bin" / "python",
    ])
    archives = python_archive_files(package_root)
    if bundled:
        return {"status": "bundled", "path": rel(bundled, runtime)}
    if archives:
        architectures = python_archive_architectures(archives)
        if platform == "macos-universal" and not {"mac-arm64", "mac-x64"}.issubset(set(architectures)):
            return {
                "status": "missing",
                "archives": [rel(path, package_root) for path in archives],
                "architectures": architectures,
                "missingArchitectures": sorted({"mac-arm64", "mac-x64"} - set(architectures)),
            }
        return {
            "status": "installer-bundled",
            "archives": [rel(path, package_root) for path in archives],
            "architectures": architectures,
        }
    if platform == "linux-service":
        return {"status": "external-required", "command": "python3"}
    return {"status": "missing"}


def executable_status(runtime: pathlib.Path, name: str, package_root: pathlib.Path, platform: str) -> dict[str, Any]:
    suffixes = [".exe", ".cmd", ""] if platform.startswith("windows") else ["", ".sh"]
    candidates: list[pathlib.Path] = []
    for directory in (
        runtime / "bin",
        runtime / "tools" / "bin",
        runtime / "node",
        runtime / "node" / "bin",
        runtime / "tools" / "node",
        runtime / "tools" / "node" / "bin",
        runtime / "node_modules" / ".bin",
        runtime / "tools" / "lark-cli" / "bin",
        runtime / "tools" / "lark-cli" / "node_modules" / ".bin",
        runtime / "python" / "Lib" / "site-packages" / "playwright" / "driver",
    ):
        for suffix in suffixes:
            candidates.append(directory / f"{name}{suffix}")
    found = first_existing([path for path in candidates if path.is_file()])
    if found:
        return {"status": "bundled", "path": rel(found, runtime)}
    archives = (
        sorted(path for path in (package_root / "node").glob("*") if path.is_file() and is_archive_file(path) and valid_executable_archive(path, name))
        if name in {"node", "npm", "npx"} and (package_root / "node").is_dir()
        else []
    )
    if archives:
        return {"status": "installer-bundled", "archives": [rel(path, package_root) for path in archives]}
    return {"status": "missing"}


def native_status(runtime: pathlib.Path, name: str, platform: str) -> dict[str, Any]:
    candidates = [
        runtime / "tools" / "bin" / name,
        runtime / "tools" / "poppler" / "bin" / name,
        runtime / "tools" / "libreoffice" / "program" / name,
        runtime / "tools" / "tesseract" / name,
    ]
    if platform.startswith("windows"):
        candidates.extend(path.with_suffix(".exe") for path in list(candidates))
    found = first_existing([path for path in candidates if path.is_file()])
    return {"status": "bundled", "path": rel(found, runtime)} if found else {"status": "missing"}


def python_packages(runtime: pathlib.Path, package_root: pathlib.Path, platform: str) -> dict[str, dict[str, Any]]:
    wheels = wheel_names(package_root)
    wheel_files = wheel_files_by_name(package_root)
    mac_arm_wheels = wheel_names_under(package_root / "wheelhouse" / "mac-arm64")
    mac_x64_wheels = wheel_names_under(package_root / "wheelhouse" / "mac-x64")
    mac_arm_files = wheel_files_by_name(package_root / "wheelhouse" / "mac-arm64")
    mac_x64_files = wheel_files_by_name(package_root / "wheelhouse" / "mac-x64")
    result: dict[str, dict[str, Any]] = {}
    for package, module in PYTHON_MODULES.items():
        found = module_exists(runtime, module)
        if found:
            result[module] = {"package": package, "status": "bundled", "path": rel(found, runtime)}
            continue
        normalized = normalized_distribution_name(package)
        if platform == "linux-service":
            result[module] = {"package": package, "status": "external-required"}
            continue
        if platform == "macos-universal" and normalized in mac_arm_wheels and normalized in mac_x64_wheels:
            result[module] = {
                "package": package,
                "status": "installer-bundled",
                "wheelhouse": ["mac-arm64", "mac-x64"],
                "archives": [
                    rel(path, package_root)
                    for path in sorted(mac_arm_files.get(normalized, []) + mac_x64_files.get(normalized, []))
                ],
            }
            continue
        if platform != "macos-universal" and normalized in wheels:
            result[module] = {
                "package": package,
                "status": "installer-bundled",
                "archives": [rel(path, package_root) for path in sorted(wheel_files.get(normalized, []))],
            }
            continue
        if platform == "linux-service":
            result[module] = {"package": package, "status": "external-required"}
        else:
            result[module] = {"package": package, "status": "missing"}
    return result


def missing_modules(packages: dict[str, dict[str, Any]], modules: list[str], *, allow_install_ready: bool) -> list[str]:
    allowed = {"bundled"} | ({"installer-bundled", "external-required"} if allow_install_ready else set())
    return [module for module in modules if packages.get(module, {}).get("status") not in allowed]


def modules_all_bundled(packages: dict[str, dict[str, Any]], modules: list[str]) -> bool:
    return all(packages.get(module, {}).get("status") == "bundled" for module in modules)


def readiness_status(*, missing: list[str], all_bundled: bool, allow_install_ready: bool) -> str:
    if missing:
        return "missing_dependency"
    return "ready" if all_bundled else "install-ready"


def tool_states(dependencies: dict[str, Any], platform: str) -> dict[str, Any]:
    packages = dependencies["pythonPackages"]
    allow_install_ready = platform in {"macos-universal", "linux-service"}
    office_modules = ["pypdf", "pdfminer", "docx", "pptx", "openpyxl", "xlsxwriter", "markdownify", "reportlab", "fitz"]
    ocr_modules = ["PIL", "rapidocr_onnxruntime"]
    office_missing = missing_modules(packages, office_modules, allow_install_ready=allow_install_ready)
    ocr_missing = missing_modules(packages, ocr_modules, allow_install_ready=allow_install_ready)
    npx_missing = dependencies["executables"]["npx"]["status"] == "missing"
    node_missing = dependencies["executables"]["node"]["status"] == "missing"
    playwright_missing = missing_modules(packages, ["playwright"], allow_install_ready=allow_install_ready)
    lark_oapi_missing = missing_modules(packages, ["lark_oapi"], allow_install_ready=allow_install_ready)
    tongxin_script = dependencies["toolFiles"]["tongxinCli"]["status"]
    browser_missing = (["node"] if node_missing else []) + (["npx"] if npx_missing else []) + playwright_missing
    browser_all_bundled = (
        dependencies["executables"]["node"]["status"] == "bundled"
        and dependencies["executables"]["npx"]["status"] == "bundled"
        and modules_all_bundled(packages, ["playwright"])
    )
    feishu_all_bundled = (
        dependencies["executables"]["lark-cli"]["status"] == "bundled"
        and modules_all_bundled(packages, ["lark_oapi"])
    )
    return {
        "office_pdf": {
            "status": readiness_status(
                missing=office_missing,
                all_bundled=modules_all_bundled(packages, office_modules),
                allow_install_ready=allow_install_ready,
            ),
            "missingDependencies": office_missing,
        },
        "ocr": {
            "status": readiness_status(
                missing=ocr_missing,
                all_bundled=modules_all_bundled(packages, ocr_modules),
                allow_install_ready=allow_install_ready,
            ),
            "missingDependencies": ocr_missing,
        },
        "browser_mcp": {
            "status": readiness_status(
                missing=browser_missing,
                all_bundled=browser_all_bundled,
                allow_install_ready=allow_install_ready,
            ),
            "missingDependencies": browser_missing,
        },
        "feishu_cli": {
            "status": (
                "discovery-only"
                if dependencies["executables"]["lark-cli"]["status"] == "missing"
                else readiness_status(
                    missing=lark_oapi_missing,
                    all_bundled=feishu_all_bundled,
                    allow_install_ready=allow_install_ready,
                )
            ),
            "missingDependencies": lark_oapi_missing,
        },
        "tongxin_cli": {
            "status": "configure-required" if tongxin_script == "missing" else "ready",
            "missingDependencies": [] if tongxin_script != "missing" else ["xin_agent_cli.py"],
        },
    }


def build_manifest(runtime: pathlib.Path, package_root: pathlib.Path, version: str, platform: str) -> dict[str, Any]:
    dependencies = {
        "python": python_status(runtime, package_root, platform),
        "executables": {
            "node": executable_status(runtime, "node", package_root, platform),
            "npm": executable_status(runtime, "npm", package_root, platform),
            "npx": executable_status(runtime, "npx", package_root, platform),
            "lark-cli": executable_status(runtime, "lark-cli", package_root, platform),
        },
        "pythonPackages": python_packages(runtime, package_root, platform),
        "nativeBins": {
            "pdfinfo": native_status(runtime, "pdfinfo", platform),
            "pdftoppm": native_status(runtime, "pdftoppm", platform),
            "soffice": native_status(runtime, "soffice", platform),
            "tesseract": native_status(runtime, "tesseract", platform),
        },
        "toolFiles": {
            "tongxinCli": (
                {"status": "bundled", "path": rel(first_existing([runtime / "tools" / "tongxin" / "xin_agent_cli.py"]) or runtime, runtime)}
                if (runtime / "tools" / "tongxin" / "xin_agent_cli.py").is_file()
                else {"status": "missing"}
            )
        },
    }
    states = tool_states(dependencies, platform)
    runtime_ready = dependencies["python"]["status"] == "bundled"
    install_ready = dependencies["python"]["status"] in {"bundled", "installer-bundled", "external-required"}
    return {
        "schemaVersion": SCHEMA_VERSION,
        "product": "EcoreX",
        "version": version,
        "platform": platform,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dependencyPolicy": {
            "ownedRuntimeDefault": True,
            "systemPathDefault": False,
            "readyRequiresProbe": True,
            "missingDependencyStatus": "missing_dependency",
        },
        "runtimeDependencies": dependencies,
        "toolStates": states,
        "releaseGate": {
            "runtimeReady": runtime_ready,
            "installReady": install_ready,
            "readyToolsNeverHaveMissingDependencies": all(
                state.get("status") != "ready" or not state.get("missingDependencies")
                for state in states.values()
            ),
        },
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--package-root", default="")
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform", required=True, choices=["windows-x64", "macos-universal", "linux-service"])
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    runtime = pathlib.Path(args.runtime_root).resolve()
    package_root = pathlib.Path(args.package_root).resolve() if args.package_root else runtime.parent.resolve()
    if not runtime.is_dir():
        raise SystemExit(f"runtime root not found: {runtime}")
    output = pathlib.Path(args.output).resolve() if args.output else runtime / "runtime-manifest.json"
    manifest = build_manifest(runtime, package_root, args.version, args.platform)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output), "platform": args.platform}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
