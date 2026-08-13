from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "scripts/deploy-v1-cloud-sidecar.py"


def _help(*, cwd: Path, pythonpath: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if pythonpath is not None:
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(pythonpath), existing) if value
        )
    return subprocess.run(
        [sys.executable, str(ENTRYPOINT), "--help"],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cloud_deployer_entrypoint_prefers_exact_source_over_old_installed_package(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "installed"
    package = installed / "ecorex/deployment"
    package.mkdir(parents=True)
    (installed / "ecorex/__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cloud_sidecar.py").write_text(
        "def main():\n    print('old-installed-deployer')\n    return 0\n",
        encoding="utf-8",
    )

    result = _help(cwd=tmp_path, pythonpath=installed)

    assert result.returncode == 0
    assert "old-installed-deployer" not in result.stdout
    assert "--stage-rollback" in result.stdout
    assert "--replace-previous" in result.stdout


def test_cloud_deployer_entrypoint_keeps_existing_actions() -> None:
    result = _help(cwd=ROOT)

    assert result.returncode == 0
    for action in ("--stage", "--apply", "--rollback"):
        assert action in result.stdout
