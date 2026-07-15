from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_v1_design_system_gate() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-v1-design-system.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_artifact_actions_are_progressive_for_pointer_keyboard_and_touch() -> None:
    css = (ROOT / "desktop/src/v1/styles/features.css").read_text(encoding="utf-8")
    component = (ROOT / "desktop/src/v1/components/ArtifactShelf.tsx").read_text(
        encoding="utf-8"
    )

    base = css[css.index(".ex-artifact-actions {") :]
    base = base[: base.index("}\n")]
    assert "opacity: 0" in base
    assert "pointer-events: none" in base
    assert ".ex-artifact:focus-within .ex-artifact-actions" in css
    assert "@media (hover: hover)" in css
    assert ".ex-artifact:hover .ex-artifact-actions" in css
    assert "@media (pointer: coarse)" in css
    assert ".ex-artifact-actions > :not(.ex-artifact-more)" in css
    assert 'useMediaMatch("(pointer: coarse)")' in component
    assert "asSheet={coarsePointer}" in component
