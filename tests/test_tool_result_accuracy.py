from pathlib import Path
from types import SimpleNamespace

from agent.tools.bash.exit_codes import interpret
from agent.tools.bash.bash import Bash
from agent.tools.browser.browser_service import BrowserService
from agent.tools.tool_manager import ToolManager
from agent.tools.vision.vision import Vision


def test_tool_results_cover_soft_exits_function_bodies_paths_and_restricted_mcp(
    tmp_path: Path,
) -> None:
    assert interpret("rg missing .", 1) == (False, "No matches found")
    assert interpret("find . -name missing", 1) == (
        False,
        "Some directories were inaccessible",
    )
    assert interpret("python broken.py", 1) == (True, None)
    bash = Bash({"cwd": str(tmp_path), "safety_mode": False})
    bash._run_command = lambda *args, **kwargs: SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="",
    )
    bash_result = bash.execute({"command": "rg absent ."})
    assert bash_result.status == "success"
    assert bash_result.result["exit_code"] == 1
    assert "No matches found" in bash_result.result["output"]

    class Page:
        def __init__(self) -> None:
            self.calls = []

        def evaluate(self, expression: str):
            self.calls.append(expression)
            if len(self.calls) == 1:
                raise RuntimeError("Illegal return statement")
            return 42

    service = BrowserService.__new__(BrowserService)
    service._page = Page()
    assert service._do_evaluate("return 42") == {"result": 42}
    assert service._page.calls[-1] == "(() => {\nreturn 42\n})()"

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    vision = Vision({"cwd": str(workspace)})
    assert vision._resolve_path("images/example.png") == str(
        (workspace / "images" / "example.png").resolve()
    )

    manager = object.__new__(ToolManager)
    manager._mcp_tool_instances = {"mcp__remote__unsafe": object()}
    restricted = SimpleNamespace(tools=[], _evolution_restricted=True)
    assert manager.sync_mcp_into_agent(restricted) == ([], [])
    wrapped = SimpleNamespace(tools={}, agent=restricted)
    assert manager.sync_mcp_into_agent(wrapped) == ([], [])
