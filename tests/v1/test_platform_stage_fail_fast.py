from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run-v1-platform-stage-step.py"
SPEC = importlib.util.spec_from_file_location("v1_platform_stage_step", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
STAGE_STEP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = STAGE_STEP
SPEC.loader.exec_module(STAGE_STEP)


def test_real_first_command_failure_stops_later_success_command(tmp_path: Path) -> None:
    marker = tmp_path / "later-command-ran.txt"
    commands = (
        STAGE_STEP.StageCommand(
            "expected first failure",
            (sys.executable, "-c", "raise SystemExit(23)"),
            tmp_path,
        ),
        STAGE_STEP.StageCommand(
            "must never run",
            (
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')",
            ),
            tmp_path,
        ),
    )

    assert STAGE_STEP.run_commands(commands) == 23
    assert not marker.exists()


def test_successful_commands_run_once_in_declared_order(tmp_path: Path) -> None:
    seen: list[tuple[tuple[str, ...], Path, bool]] = []

    def runner(
        argv: tuple[str, ...], *, cwd: Path, check: bool
    ) -> subprocess.CompletedProcess[object]:
        seen.append((argv, cwd, check))
        return subprocess.CompletedProcess(argv, 0)

    commands = (
        STAGE_STEP.StageCommand("one", ("one",), tmp_path),
        STAGE_STEP.StageCommand("two", ("two",), tmp_path),
    )

    assert STAGE_STEP.run_commands(commands, runner=runner) == 0
    assert seen == [
        (("one",), tmp_path, False),
        (("two",), tmp_path, False),
    ]


def test_platform_stage_workflow_delegates_multicommand_steps_to_runner() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "ecorex-v1-platform-stage.yml"
    ).read_text(encoding="utf-8")

    assert (
        "run: python scripts/run-v1-platform-stage-step.py install-dependencies"
        in workflow
    )
    assert "run: python scripts/run-v1-platform-stage-step.py build-web" in workflow
    assert "python scripts/install-v1-python-profile.py --profile platform-stage" not in workflow
    assert "python -m playwright install chromium" not in workflow
    assert "npm run test:v1" not in workflow
    assert "continue-on-error" not in workflow
