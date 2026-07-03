#!/usr/bin/env python3
"""真实发布完整校验入口。

The full release gate first deploys the local v0.2.7 release artifacts to the
configured production server, then runs the production acceptance matrix.
"""

from __future__ import annotations

import importlib.util
import json
import os
import runpy
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION = os.environ.get("ECOREX_ACCEPTANCE_VERSION", "0.2.7")
TARGET = Path(__file__).with_name("smoke-v026-production-agent-product-acceptance.py")
DEPLOY_TARGET = Path(__file__).with_name("deploy-v024-production.py")


def _load_deploy_module():
    spec = importlib.util.spec_from_file_location("deploy_v024_production", DEPLOY_TARGET)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {DEPLOY_TARGET.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _output_path_from_argv() -> Path:
    for index, arg in enumerate(sys.argv):
        if arg == "--output" and index + 1 < len(sys.argv):
            return Path(sys.argv[index + 1])
        if arg.startswith("--output="):
            return Path(arg.split("=", 1)[1])
    return ROOT / "docs" / f"v{VERSION}" / "artifacts" / "production-agent-product-acceptance.json"


def _write_deploy_failure(exc: Exception, deployer=None) -> None:
    output = _output_path_from_argv()
    output.parent.mkdir(parents=True, exist_ok=True)
    message = str(exc)
    if deployer is not None:
        message = deployer.redact(message)
    payload = {
        "status": "FAIL",
        "version": VERSION,
        "scope": "production-agent-product-acceptance",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "errorType": exc.__class__.__name__,
        "error": message[:1000],
        "phase": "production-deploy",
        "commands": getattr(deployer, "commands", []) if deployer is not None else [],
        "redaction": {
            "rawPasswordPersisted": False,
            "rawSecretPersisted": False,
            "rawUrlPersisted": False,
            "rawUserPathPersisted": False,
            "rawOutputPersisted": False,
        },
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": "FAIL",
        "artifact": str(output),
        "phase": "production-deploy",
        "errorType": exc.__class__.__name__,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    skip_deploy = "--skip-deploy" in sys.argv
    if skip_deploy:
        sys.argv = [arg for arg in sys.argv if arg != "--skip-deploy"]
    else:
        os.environ["ECOREX_DEPLOY_VERSION"] = VERSION
        os.environ["ECOREX_PROMOTE_PUBLIC_RELEASE"] = "1"
        deployer = None
        try:
            deployer = _load_deploy_module().ProductionDeploy()
            result = deployer.run()
            if result.get("status") != "PASS":
                raise RuntimeError(f"production deploy failed: {result}")
        except Exception as exc:
            _write_deploy_failure(exc, deployer)
            raise SystemExit(1)
    runpy.run_path(str(TARGET), run_name="__main__")
