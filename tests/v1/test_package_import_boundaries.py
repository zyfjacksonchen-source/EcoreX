"""Cold-import contracts for independently deployable v1 package boundaries."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "statement",
    (
        "import ecorex.update",
        "import ecorex.control_plane",
        "import ecorex.gateway",
        "from ecorex.runtime import RuntimeComposition",
        "from ecorex.runtime import AgentTurnWorker, AgentWorkerSupervisor",
        "import ecorex.update; from ecorex.runtime import RuntimeComposition",
        "import ecorex.update; from ecorex.runtime import AgentTurnWorker",
    ),
)
def test_public_packages_are_cold_importable_in_isolation(statement: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", statement],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
