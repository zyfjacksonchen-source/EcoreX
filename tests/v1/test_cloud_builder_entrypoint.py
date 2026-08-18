from pathlib import Path
import subprocess
import sys


def test_cloud_builder_missing_profile_has_actionable_error() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(root / "scripts/build-v1-linux-cloud-artifact.py"),
            "--help",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "ModuleNotFoundError" not in result.stderr
    assert (
        result.stderr.strip()
        == "cloud_builder_dependencies_missing: use the Python 3.11.9 builder "
        "venv prepared by "
        "'python scripts/install-v1-python-profile.py --profile cloud'"
    )
