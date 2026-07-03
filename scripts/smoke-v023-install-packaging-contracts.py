#!/usr/bin/env python3
"""Source-contract smoke for v0.2.3 local installer packaging.

This guards the post-release install hotfix: Feishu/Lark optional dependencies
must not block first-run install, and macOS local packages must not bundle
RapidOCR/Lark wheelhouses into the core offline dependency set.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def check(label: str, ok: bool) -> dict[str, Any]:
    return {"label": label, "status": "PASS" if ok else "FAIL"}


def main() -> int:
    packaging = read("scripts/prepare-ecorex-webui-local-release.ps1")
    install_ps1 = read("deploy/ecorex-site/install-webui.ps1")
    web_release = read("scripts/prepare-ecorex-web-release.ps1")
    runtime_packs = read("runtime-packs/core-requirements.txt")
    runtime_copy = read("desktop/runtime/ecorex-runtime/core-requirements.txt")
    capabilities = json.loads(read("runtime-packs/capabilities.json"))
    packs = {item.get("id"): item for item in capabilities.get("packs", [])}

    checks = [
        check(
            "windows package preinstalls lark_oapi before first-run",
            'Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "lark_oapi"' in packaging
            and 'PackageSpec "lark-oapi>=1.5.5"' in packaging
            and 'PYTHONNOUSERSITE = "1"' in packaging
            and 'PYTHONDONTWRITEBYTECODE = "1"' in packaging
            and "& $python -s -m pip install" in packaging
            and "--no-compile" in packaging
            and "--timeout 60" in packaging
            and "--retries 2" in packaging
            and '& $Python -s -c "import importlib.util' in packaging
            and 'Ensure-PythonDependency -Python $python -StateDir $stateDir -ModuleName "lark_oapi"' not in packaging,
        ),
        check(
            "windows package strips pycache after preinstalling dependencies",
            'Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "lark_oapi"' in packaging
            and 'Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "lark_oapi"' in packaging
            and packaging.index('Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "lark_oapi"')
            < packaging.index('Remove-GeneratedNoise -Root $winRuntime', packaging.index('Install-WindowsRuntimeDependency -RuntimeDir $winRuntime -ModuleName "lark_oapi"')),
        ),
        check(
            "windows first-run installer does not run pip",
            "function Ensure-PythonDependency" not in packaging
            and "python-deps-install.last.log" not in packaging,
        ),
        check(
            "windows bootstrap prefers curl accelerated resumable download",
            "function Try-SaveUrlWithCurl" in install_ps1
            and "Get-Command curl.exe" in install_ps1
            and '"--continue-at", "-"' in install_ps1
            and "falling back to PowerShell streaming download" in install_ps1,
        ),
        check(
            "windows bootstrap avoids Expand-Archive cleanup race",
            "function Expand-EcoreXZip" in install_ps1
            and "function ConvertTo-EcoreXLongPath" in install_ps1
            and "Blocked unsafe zip entry" in install_ps1
            and "$source.CopyTo($target)" in install_ps1
            and "Expand-EcoreXZip -ZipPath $packagePath -DestinationPath $extractRoot" in install_ps1
            and "Expand-Archive" not in install_ps1,
        ),
        check(
            "mac local installer keeps lark-oapi while pruning rapidocr",
            "function New-LocalMacCoreRequirements" in packaging
            and 'StartsWith("rapidocr-onnxruntime")' in packaging
            and 'StartsWith("lark-oapi")' not in packaging,
        ),
        check(
            "mac wheelhouse download uses pruned local requirements",
            "Invoke-PipDownload -Platform \"macosx_11_0_arm64\" -Destination $wheelArm -RequirementsPath $macLocalCoreRequirements" in packaging
            and "Invoke-PipDownload -Platform \"macosx_11_0_x86_64\" -Destination $wheelX64 -RequirementsPath $macLocalCoreRequirements" in packaging,
        ),
        check(
            "local WebUI packaging overlays current root config ABI",
            '"config.py"' in packaging
            and '"config-template.json"' in packaging
            and "runtime-packs/capabilities.json" in packaging
            and "runtime-packs/core-requirements.txt" in packaging,
        ),
        check(
            "web service release overlays current root config ABI",
            'Join-Path $repoRoot "config.py"' in web_release
            and 'Join-Path $repoRoot "config-template.json"' in web_release
            and 'Join-Path $runtimePackRoot "core-requirements.txt"' in web_release,
        ),
        check("source core requirements still declare lark-oapi", "lark-oapi>=1.5.5" in runtime_packs and "lark-oapi>=1.5.5" in runtime_copy),
        check("source core requirements still declare rapidocr", "rapidocr-onnxruntime" in runtime_packs),
        check(
            "fast-ocr capability remains declared for on-demand install/detection",
            "fast-ocr" in packs
            and "rapidocr-onnxruntime" in (packs["fast-ocr"].get("requirements") or [])
            and "rapidocr_onnxruntime" in (packs["fast-ocr"].get("moduleChecks") or []),
        ),
    ]
    failed = [row["label"] for row in checks if row["status"] != "PASS"]
    result = {
        "status": "PASS" if not failed else "FAIL",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checks": checks,
        "failed": failed,
        "redacted": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
