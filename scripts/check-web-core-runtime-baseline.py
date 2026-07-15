#!/usr/bin/env python3
"""Capture and optionally enforce the EcoreX Web core runtime baseline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "web-runtime-goal" / "artifacts" / "S0-web-core-runtime-current.json"
SCHEMA_VERSION = "web-core-runtime-baseline-v1"

CORE_EXECUTABLES = {
    "python": {
        "requiredFor": ["all Python tools", "Web service", "diagnostics"],
        "repairAction": "repair_core_python",
    },
    "node": {
        "requiredFor": ["MCP bootstrap", "browser tooling", "Feishu/Lark CLI install"],
        "repairAction": "repair_core_node",
    },
    "npm": {
        "requiredFor": ["capability package installs", "Feishu/Lark CLI install"],
        "repairAction": "repair_core_node",
    },
    "npx": {
        "requiredFor": ["MCP bootstrap", "Chrome DevTools MCP"],
        "repairAction": "repair_core_node",
    },
}

CORE_PYTHON_PACKAGES = {
    "pip": ("Python package installation", "repair_core_python"),
    "PIL": ("image preprocessing, OCR, image quality checks", "repair_fast_ocr"),
    "rapidocr_onnxruntime": ("local OCR", "repair_fast_ocr"),
    "onnxruntime": ("local OCR runtime", "repair_fast_ocr"),
    "playwright": ("browser automation package", "repair_browser_automation"),
    "lark_oapi": ("Feishu/Lark SDK", "repair_feishu_core"),
    "pypdf": ("PDF parsing", "repair_office_pdf_python"),
    "pdfminer": ("PDF text extraction", "repair_office_pdf_python"),
    "docx": ("Word parsing", "repair_office_pdf_python"),
    "pptx": ("PowerPoint parsing", "repair_office_pdf_python"),
    "openpyxl": ("Excel parsing", "repair_office_pdf_python"),
    "xlsxwriter": ("Excel generation", "repair_office_pdf_python"),
    "markdownify": ("HTML/document markdown conversion", "repair_office_pdf_python"),
    "reportlab": ("PDF generation", "repair_office_pdf_python"),
    "fitz": ("PDF/image rendering via PyMuPDF", "repair_office_pdf_python"),
}

TOOL_ENTRYPOINTS = {
    "vision": Path("agent") / "tools" / "vision" / "vision.py",
    "ocr": Path("agent") / "tools" / "ocr" / "ocr.py",
    "imagegen": Path("agent") / "tools" / "imagegen" / "imagegen.py",
}

PLAYWRIGHT_CHROMIUM_PATTERNS = [
    "playwright-browsers/chromium-*/chrome-linux/chrome",
    "playwright-browsers/chromium-*/chrome-linux*/chrome",
    "playwright-browsers/chromium_headless_shell-*/chrome-linux/headless_shell",
    "playwright-browsers/chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell",
    "playwright-browsers/chromium-*/chrome-win/chrome.exe",
    "playwright-browsers/chromium-*/chrome-win*/chrome.exe",
    "playwright-browsers/chromium_headless_shell-*/chrome-headless-shell-win*/chrome-headless-shell.exe",
    "playwright-browsers/chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    "playwright-browsers/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
    "playwright-browsers/chromium_headless_shell-*/chrome-headless-shell-mac*/chrome-headless-shell",
]

OPTIONAL_REPAIRABLE = {
    "lark-cli": ("Feishu/Lark structured CLI", "repair_feishu_lark_cli"),
    "soffice": ("Office document rendering", "repair_office_native_backend"),
    "libreoffice": ("Office document rendering", "repair_office_native_backend"),
    "pdftoppm": ("PDF rasterization", "repair_office_native_backend"),
    "pdftocairo": ("PDF rasterization", "repair_office_native_backend"),
    "pdfinfo": ("PDF metadata/probe", "repair_office_native_backend"),
    "tesseract": ("native OCR fallback", "repair_ocr_native_backend"),
}

CREDENTIAL_CHECKS = {
    "openai_image_provider": {
        "env": ["OPENAI_API_KEY", "OPEN_AI_API_KEY"],
        "requiredFor": ["imagegen gpt-image-2-pro", "vision OpenAI-compatible route"],
        "nextAction": "configure_model_provider",
    },
    "vision_provider": {
        "env": ["OPENAI_API_KEY", "OPEN_AI_API_KEY", "ZHIPU_AI_API_KEY", "GEMINI_API_KEY", "QIANFAN_AK"],
        "requiredFor": ["vision image analysis"],
        "nextAction": "configure_model_provider",
    },
}


def _redact_path(value: str) -> str:
    if not value:
        return ""
    text = str(value).replace("\\", "/")
    home = str(Path.home()).replace("\\", "/").rstrip("/")
    if home and text.lower().startswith(home.lower()):
        return "%USERPROFILE%" + text[len(home):]
    return text


def _replace_known_roots(value: str, runtime_root: Path, state_root: Path) -> str:
    text = str(value or "").replace("\\", "/")
    roots = [
        ("%STATE_ROOT%", str(state_root).replace("\\", "/").rstrip("/")),
        ("%RUNTIME_ROOT%", str(runtime_root).replace("\\", "/").rstrip("/")),
    ]
    for marker, root in roots:
        if root and text.lower().startswith(root.lower()):
            suffix = text[len(root):]
            return marker + suffix
    return _redact_path(text)


def _redact_report_paths(value: Any, runtime_root: Path, state_root: Path) -> Any:
    if isinstance(value, dict):
        return {key: _redact_report_paths(item, runtime_root, state_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_report_paths(item, runtime_root, state_root) for item in value]
    if isinstance(value, str):
        return _replace_known_roots(value, runtime_root, state_root)
    return value


def _count_by(rows: Iterable[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _dependency_row(
    *,
    name: str,
    category: str,
    dependency_type: str,
    available: bool,
    source: str,
    path: str = "",
    required_for: Iterable[str] = (),
    repair_action: str = "",
    status: str = "",
) -> Dict[str, Any]:
    computed_status = status or ("ready" if available else f"missing_{dependency_type.replace('-', '_')}")
    blocking = category == "coreRequired" and computed_status != "ready"
    return {
        "name": name,
        "category": category,
        "dependencyType": dependency_type,
        "status": computed_status,
        "available": bool(available),
        "source": source,
        "path": str(path or "").replace("\\", "/"),
        "requiredFor": list(required_for),
        "repairable": bool(repair_action),
        "repairAction": repair_action,
        "blocking": blocking,
    }


def _runtime_provider(runtime_root: Path, state_root: Path):
    sys.path.insert(0, str(ROOT))
    from common.runtime_dependencies import RuntimeDependencyProvider

    return RuntimeDependencyProvider(runtime_root=runtime_root, state_root=state_root)


def _capture_executables(provider, include_system_path: bool) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for name, meta in CORE_EXECUTABLES.items():
        dependency = provider.python(allow_system_path=include_system_path) if name == "python" else provider.resolve_executable(name, allow_system_path=include_system_path)
        rows.append(_dependency_row(
            name=name,
            category="coreRequired",
            dependency_type="executable" if name != "python" else "python",
            available=dependency.available,
            source=dependency.source,
            path=dependency.path,
            required_for=meta["requiredFor"],
            repair_action=meta["repairAction"],
        ))
    return rows


def _capture_python_packages(provider, include_system_path: bool) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for module_name, (required_for, repair_action) in CORE_PYTHON_PACKAGES.items():
        dependency = provider.resolve_python_package(module_name, allow_system_path=include_system_path)
        rows.append(_dependency_row(
            name=module_name,
            category="coreRequired",
            dependency_type="python-package",
            available=dependency.available,
            source=dependency.source,
            path=dependency.path,
            required_for=[required_for],
            repair_action=repair_action,
            status="ready" if dependency.available else "missing_package",
        ))
    return rows


def _capture_tool_entrypoints(runtime_root: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for name, source_path in TOOL_ENTRYPOINTS.items():
        if source_path.is_absolute():
            path = source_path
        else:
            path = runtime_root / source_path
        available = path.is_file()
        rows.append(_dependency_row(
            name=name,
            category="coreRequired",
            dependency_type="tool-entrypoint",
            available=available,
            source="ecorex-bundled" if available else "missing",
            path=str(path) if available else "",
            required_for=[f"{name} tool invocation"],
            repair_action="repair_core_tools",
            status="ready" if available else "missing_tool_entrypoint",
        ))
    return rows


def _capture_browser_runtime(runtime_root: Path, state_root: Path) -> List[Dict[str, Any]]:
    matches: List[Path] = []
    for root in (runtime_root, state_root):
        for pattern in PLAYWRIGHT_CHROMIUM_PATTERNS:
            matches.extend(root.glob(pattern))
    path = str(matches[0]) if matches else ""
    source = "ecorex-bundled-playwright" if matches and runtime_root in (matches[0], *matches[0].parents) else "ecorex-managed-playwright"
    return [_dependency_row(
        name="playwright_chromium",
        category="coreRequired",
        dependency_type="browser-runtime",
        available=bool(matches),
        source=source if matches else "missing",
        path=path,
        required_for=["browser/CDP fallback automation"],
        repair_action="repair_browser_automation",
        status="ready" if matches else "missing_browser_runtime",
    )]


def _capture_optional(provider, include_system_path: bool) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for name, (required_for, repair_action) in OPTIONAL_REPAIRABLE.items():
        dependency = provider.resolve_executable(name, allow_system_path=include_system_path)
        rows.append(_dependency_row(
            name=name,
            category="optionalRepairable",
            dependency_type="native-runtime",
            available=dependency.available,
            source=dependency.source,
            path=dependency.path,
            required_for=[required_for],
            repair_action=repair_action,
            status="ready" if dependency.available else "missing_native_runtime",
        ))
    return rows


def _capture_credentials() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for name, meta in CREDENTIAL_CHECKS.items():
        keys = [key for key in meta["env"] if os.environ.get(key)]
        rows.append({
            "name": name,
            "category": "credentialRequired",
            "dependencyType": "credential",
            "status": "ready" if keys else "missing_model_credentials",
            "available": bool(keys),
            "source": "environment" if keys else "missing",
            "path": "",
            "requiredFor": list(meta["requiredFor"]),
            "repairable": True,
            "repairAction": meta["nextAction"],
            "blocking": False,
            "credentialPresent": bool(keys),
            "credentialEnvKeys": ["***" for _ in keys],
        })
    return rows


def capture_report(args: argparse.Namespace) -> Dict[str, Any]:
    runtime_root = args.runtime_root.resolve()
    state_root = args.state_root.resolve()
    provider = _runtime_provider(runtime_root, state_root)
    dependencies: List[Dict[str, Any]] = []
    dependencies.extend(_capture_executables(provider, args.include_system_path))
    dependencies.extend(_capture_python_packages(provider, args.include_system_path))
    dependencies.extend(_capture_tool_entrypoints(runtime_root))
    dependencies.extend(_capture_browser_runtime(runtime_root, state_root))
    dependencies.extend(_capture_optional(provider, args.include_system_path))
    dependencies.extend(_capture_credentials())
    blocking = [item for item in dependencies if item.get("blocking")]
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "runtimeRoot": str(runtime_root),
        "stateRoot": str(state_root),
        "systemPathIncluded": bool(args.include_system_path),
        "categories": {
            "coreRequired": "Must be ready for a Web runtime release.",
            "defaultPreinstalled": "Expected to ship or install by default, but not represented separately yet.",
            "optionalRepairable": "May be absent when a repair action exists.",
            "credentialRequired": "Requires user/admin model or connector credentials; absence is not a package failure.",
        },
        "dependencies": dependencies,
        "summary": {
            "total": len(dependencies),
            "ready": sum(1 for item in dependencies if item.get("status") == "ready"),
            "blocking": len(blocking),
            "blockingNames": [str(item.get("name")) for item in blocking],
            "categoryCounts": _count_by(dependencies, "category"),
            "statusCounts": _count_by(dependencies, "status"),
            "repairActionCounts": _count_by((item for item in dependencies if item.get("repairAction")), "repairAction"),
            "releaseReady": not blocking,
        },
    }
    return _redact_report_paths(report, runtime_root, state_root)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=ROOT, help="Runtime root to inspect.")
    parser.add_argument("--state-root", type=Path, default=ROOT / "state", help="Writable Web state root to inspect.")
    parser.add_argument("--include-system-path", action="store_true", help="Include host PATH in diagnostics. Release gates should not use this.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Write JSON report here.")
    parser.add_argument("--no-write", action="store_true", help="Print JSON to stdout instead of writing a report.")
    parser.add_argument("--strict", action="store_true", help="Exit nonzero when coreRequired dependencies are missing.")
    return parser.parse_args(argv[1:])


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    if args.strict and args.include_system_path:
        print("ERROR --strict cannot be combined with --include-system-path; strict Web release gates must be owned-runtime only.", file=sys.stderr)
        return 2
    report = capture_report(args)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.no_write:
        print(payload, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(str(args.output))
    if args.strict and report.get("summary", {}).get("blocking"):
        blocking_names = ", ".join(report.get("summary", {}).get("blockingNames") or [])
        if blocking_names:
            if len(blocking_names) > 600:
                blocking_names = blocking_names[:597] + "..."
            print(f"ERROR blocking Web core dependencies: {blocking_names}", file=sys.stderr)
        print(
            f"ERROR Web core runtime baseline has {report['summary']['blocking']} blocking gaps. Report: {args.output}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
