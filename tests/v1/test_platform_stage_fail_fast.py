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


def test_clean_checkout_uses_git_porcelain_without_bash(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    seen: list[tuple[tuple[str, ...], Path, bool, bool]] = []

    def runner(
        argv: tuple[str, ...], *, cwd: Path, check: bool, capture_output: bool
    ) -> subprocess.CompletedProcess[bytes]:
        seen.append((argv, cwd, check, capture_output))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(STAGE_STEP.shutil, "which", lambda name: "git-fixed")

    assert STAGE_STEP.check_clean_checkout(root=tmp_path, runner=runner) == 0
    assert seen == [
        (
            (
                "git-fixed",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
            tmp_path,
            False,
            True,
        )
    ]


def test_clean_checkout_rejects_dirty_tree_and_preexisting_dist(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    calls = 0

    def dirty_runner(
        argv: tuple[str, ...], *, cwd: Path, check: bool, capture_output: bool
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(argv, 0, stdout=b"?? untracked\n", stderr=b"")

    monkeypatch.setattr(STAGE_STEP.shutil, "which", lambda name: "git-fixed")

    assert STAGE_STEP.check_clean_checkout(root=tmp_path, runner=dirty_runner) == 75
    (tmp_path / "desktop" / "dist").mkdir(parents=True)
    assert STAGE_STEP.check_clean_checkout(root=tmp_path, runner=dirty_runner) == 75
    assert calls == 1


def test_clean_web_stage_builds_one_dist_before_testing_it(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    seen: list[tuple[str, ...]] = []
    build_count = 0
    commands = tuple(
        STAGE_STEP.StageCommand(label, (executable, *arguments), tmp_path)
        for label, executable, arguments, cwd in STAGE_STEP.COMMAND_CATALOG[
            "build-web"
        ]
    )

    def runner(
        argv: tuple[str, ...], *, cwd: Path, check: bool
    ) -> subprocess.CompletedProcess[object]:
        nonlocal build_count
        assert cwd == tmp_path
        assert check is False
        seen.append(argv)
        if argv[1:] == ("run", "build"):
            assert not dist.exists()
            build_count += 1
            dist.mkdir()
            (dist / "index.html").write_text("<html></html>", encoding="utf-8")
        if argv[1:] == ("run", "test:v1"):
            assert dist.is_dir(), "GA tests must consume the dist built in this stage"
        if argv == ("node", "tools/check-v1-bundle.mjs", "dist"):
            assert dist.is_dir(), "the read-only bundle gate must inspect the tested dist"
        if argv == (
            "python",
            "scripts/check-v1-reproducibility.py",
            "--web-dist",
            "desktop/dist",
        ):
            assert dist.is_dir(), "content-address validation must inspect the tested dist"
        return subprocess.CompletedProcess(argv, 0)

    assert not dist.exists()
    assert (
        STAGE_STEP.run_build_web_commands(
            commands,
            dist_root=dist,
            runner=runner,
            expected_commands=commands,
        )
        == 0
    )
    assert seen == [
        ("npm", "ci"),
        ("npm", "run", "typecheck"),
        ("npm", "run", "build"),
        ("npm", "run", "test:v1"),
        (
            "python",
            "scripts/check-v1-reproducibility.py",
            "--web-dist",
            "desktop/dist",
        ),
        ("node", "tools/check-v1-bundle.mjs", "dist"),
    ]
    assert build_count == 1


def _fake_web_commands(tmp_path: Path) -> tuple[object, ...]:
    return tuple(
        STAGE_STEP.StageCommand(label, (executable, *arguments), tmp_path)
        for label, executable, arguments, cwd in STAGE_STEP.COMMAND_CATALOG[
            "build-web"
        ]
    )


def test_web_stage_rejects_dist_created_by_npm_ci(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    seen: list[tuple[str, ...]] = []

    def runner(
        argv: tuple[str, ...], *, cwd: Path, check: bool
    ) -> subprocess.CompletedProcess[object]:
        seen.append(argv)
        if argv == ("npm", "ci"):
            dist.mkdir()
        return subprocess.CompletedProcess(argv, 0)

    commands = _fake_web_commands(tmp_path)
    assert (
        STAGE_STEP.run_build_web_commands(
            commands,
            dist_root=dist,
            runner=runner,
            expected_commands=commands,
        )
        == 73
    )
    assert seen == [("npm", "ci")]


def test_web_stage_rejects_truncated_command_sequence(tmp_path: Path) -> None:
    commands = _fake_web_commands(tmp_path)
    assert (
        STAGE_STEP.run_build_web_commands(
            commands[:-1],
            dist_root=tmp_path / "dist",
            runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0),
            expected_commands=commands,
        )
        == 76
    )


def test_web_stage_rejects_reordered_command_sequence(tmp_path: Path) -> None:
    commands = _fake_web_commands(tmp_path)
    reordered = (commands[0], commands[2], commands[1], *commands[3:])
    assert (
        STAGE_STEP.run_build_web_commands(
            reordered,
            dist_root=tmp_path / "dist",
            runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0),
            expected_commands=commands,
        )
        == 76
    )


def test_web_stage_rejects_test_mutation_before_bundle_validation(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    seen: list[tuple[str, ...]] = []

    def runner(
        argv: tuple[str, ...], *, cwd: Path, check: bool
    ) -> subprocess.CompletedProcess[object]:
        seen.append(argv)
        if argv[1:] == ("run", "build"):
            dist.mkdir()
            (dist / "index.html").write_text("built", encoding="utf-8")
        elif argv[1:] == ("run", "test:v1"):
            (dist / "index.html").write_text("mutated", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    commands = _fake_web_commands(tmp_path)
    assert (
        STAGE_STEP.run_build_web_commands(
            commands,
            dist_root=dist,
            runner=runner,
            expected_commands=commands,
        )
        == 74
    )
    assert ("node", "tools/check-v1-bundle.mjs", "dist") not in seen


def test_web_stage_rejects_bundle_validator_mutation(tmp_path: Path) -> None:
    dist = tmp_path / "dist"

    def runner(
        argv: tuple[str, ...], *, cwd: Path, check: bool
    ) -> subprocess.CompletedProcess[object]:
        if argv[1:] == ("run", "build"):
            dist.mkdir()
            (dist / "index.html").write_text("built", encoding="utf-8")
        elif argv == ("node", "tools/check-v1-bundle.mjs", "dist"):
            (dist / "index.html").write_text("validator mutation", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    commands = _fake_web_commands(tmp_path)
    assert (
        STAGE_STEP.run_build_web_commands(
            commands,
            dist_root=dist,
            runner=runner,
            expected_commands=commands,
        )
        == 74
    )


def test_platform_stage_workflow_delegates_multicommand_steps_to_runner() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "ecorex-v1-platform-stage.yml"
    ).read_text(encoding="utf-8")

    assert (
        "run: python scripts/run-v1-platform-stage-step.py clean-check"
        in workflow
    )
    assert (
        "run: python scripts/run-v1-platform-stage-step.py install-dependencies"
        in workflow
    )
    assert "run: python scripts/run-v1-platform-stage-step.py build-web" in workflow
    assert "python scripts/install-v1-python-profile.py --profile platform-stage" not in workflow
    assert "python -m playwright install chromium" not in workflow
    assert "npm run test:v1" not in workflow
    assert "clean: true" in workflow
    assert (
        "- name: Prove checkout workspace is clean before dependency installation\n"
        "        run: python scripts/run-v1-platform-stage-step.py clean-check"
        in workflow
    )
    assert "continue-on-error" not in workflow
