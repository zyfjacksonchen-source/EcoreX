from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_light_release_entry_runs_current_v1_checks() -> None:
    script = ROOT / "scripts" / "真实发布轻量校验.py"
    spec = importlib.util.spec_from_file_location("light_release_validation", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    report = module.build_report()

    assert report["status"] == "passed"
    assert report["check_count"] == 4
    assert [row["path"] for row in report["checks"]] == list(module.CHECKS)
