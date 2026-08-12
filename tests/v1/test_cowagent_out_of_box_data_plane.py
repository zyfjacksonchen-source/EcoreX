from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

from ecorex.capabilities import (
    ApprovalRequirement,
    Exposure,
    ToolProviderKind,
    ToolProviderProvenance,
    ToolProviderTrust,
)
from ecorex.capabilities.builtin import builtin_capability_registry
from ecorex.capabilities.cow_local_tools import CowLocalTools
from ecorex.artifacts import ArtifactService
from ecorex.extensions.cow_mcp import CowMCPConfigService
from ecorex.extensions.mcp import discover_mcp_tools
from ecorex.memory import MemoryService
from agent.tools.scheduler.scheduler_service import SchedulerService
from agent.tools.scheduler.task_store import TaskStore
from ecorex.runtime.worker import _cow_workspace_instructions
from ecorex.workspace_content import WorkspaceContentService


def test_cow_first_party_tools_are_direct_without_approval() -> None:
    registry = builtin_capability_registry()
    required = {
        "read",
        "write",
        "edit",
        "ls",
        "search_files",
        "bash",
        "web_fetch",
        "web_search",
        "browser",
        "memory_search",
        "memory_get",
        "scheduler",
        "send",
    }
    specs = {spec.tool_id: spec for spec in registry.all()}
    assert required <= specs.keys()
    assert all(specs[tool_id].default_exposure is Exposure.DIRECT for tool_id in required)
    assert all(
        specs[tool_id].approval_requirement is ApprovalRequirement.NEVER
        for tool_id in required
    )
    assert registry.resolve("shell").tool_id == "bash"
    assert registry.resolve("fetch").tool_id == "web_fetch"
    assert registry.resolve("cdp").tool_id == "browser"


def test_memory_and_knowledge_share_workspace_files(tmp_path: Path) -> None:
    knowledge = WorkspaceContentService(tmp_path, database=None)
    assert (tmp_path / "knowledge" / "index.md").is_file()
    assert (tmp_path / "knowledge" / "log.md").is_file()
    knowledge.create_category("concepts")
    knowledge.create_document("concepts/context.md", "# Context\n\nshared context")
    (tmp_path / "MEMORY.md").write_text("偏好：使用深色模式\n", encoding="utf-8")
    (tmp_path / "memory").mkdir(exist_ok=True)
    (tmp_path / "memory" / "2026-08-11.md").write_text(
        "今天完成 Cow 数据面还原\n", encoding="utf-8"
    )
    skill = tmp_path / "skills" / "report"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: report\ndescription: Build a report from workspace facts.\n---\n\n# Report\n",
        encoding="utf-8",
    )

    tools = CowLocalTools((tmp_path,))
    assert tools.memory_search({"query": "深色模式"})["results"][0]["path"] == "MEMORY.md"
    assert tools.memory_search({"query": "shared context"})["results"][0]["path"] == (
        "knowledge/concepts/context.md"
    )
    assert tools.memory_get({"path": "2026-08-11.md"})["content"] == (
        "今天完成 Cow 数据面还原"
    )

    service = MemoryService(
        tmp_path / "unused.db",
        workspace_root=tmp_path,
        initialize=False,
    )
    page = service.content_page(view="files", page=1)
    assert [item.path for item in page.items] == ["MEMORY.md", "memory/2026-08-11.md"]
    document = service.content_document(view="files", item_id=page.items[0].item_id)
    assert "深色模式" in document.content

    instructions = _cow_workspace_instructions(tmp_path)
    assert instructions is not None
    assert "深色模式" in instructions
    assert "knowledge/index.md" in instructions
    assert "<available_skills>" in instructions
    assert "<name>report</name>" in instructions
    assert "<location>skills/report/SKILL.md</location>" in instructions


def test_cow_send_publishes_a_real_user_artifact(tmp_path: Path) -> None:
    from ecorex.capabilities.cow_local_tools import CowSendTool

    source = tmp_path / "report.txt"
    source.write_text("ready", encoding="utf-8")
    artifacts = ArtifactService(tmp_path / "artifacts")
    result = CowSendTool(artifacts, account_id="local-user")(
        {"path": str(source), "message": "报告"}
    )

    artifact = artifacts.get_user_artifact(
        result["artifact_id"], account_id="local-user"
    )
    assert result["type"] == "file_to_send"
    assert result["revision_id"] == artifact.revision_id
    assert artifact.display_name.startswith("report_")
    assert artifact.display_name.endswith(".txt")
    assert artifacts.blobs.read_bytes(artifact.sha256) == b"ready"


def test_cow_scheduler_runs_due_task_from_local_store(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "scheduler" / "tasks.json"))
    due = (datetime.now() - timedelta(seconds=1)).isoformat()
    created = {
        "id": "reminder",
        "name": "reminder",
        "enabled": True,
        "schedule": {"type": "once", "run_at": due},
        "action": {
            "type": "send_message",
            "content": "stand up",
            "receiver": "thread-1",
            "notify_session_id": "thread-1",
        },
        "next_run_at": due,
    }
    store.add_task(created)
    observed: list[str] = []

    def execute(task):
        observed.append(task["action"]["content"])
        return True

    service = SchedulerService(store, execute)
    service._check_and_execute_tasks()
    assert observed == ["stand up"]
    assert store.get_task(created["id"]) is None


def test_cow_mcp_json_loads_stdio_tools_without_enterprise_registration(
    tmp_path: Path,
) -> None:
    server = tmp_path / "mcp_server.py"
    server.write_text(
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "fixture", "version": "1.0", "extra": True},
        }
    elif method == "tools/list":
        result = {"tools": [{
            "name": "echo",
            "inputSchema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            "annotations": {"readOnlyHint": True},
        }]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": request["params"]["arguments"]["text"]}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fixture": {
                        "command": sys.executable,
                        "args": [str(server)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    reloads: list[str] = []
    service = CowMCPConfigService(
        tmp_path,
        runtime_api_version="1.0.0",
        platform=sys.platform,
        architecture="arm64",
        reload_requester=lambda reason: reloads.append(reason) or True,
        poll_seconds=0.01,
    )
    assert service.bindings() == ()

    async def populate_cache() -> None:
        await service.start()
        for _ in range(500):
            if reloads:
                break
            await asyncio.sleep(0.01)
        await service.stop()

    asyncio.run(populate_cache())
    assert reloads == ["cow-mcp:mcp.json"]

    cached = CowMCPConfigService(
        tmp_path,
        runtime_api_version="1.0.0",
        platform=sys.platform,
        architecture="arm64",
    )
    (binding,) = cached.bindings()
    (tool,) = binding.tools
    provider = ToolProviderProvenance(
        kind=ToolProviderKind.MCP,
        provider_id=binding.extension_id,
        revision_id=binding.revision_id,
        trust=ToolProviderTrust.USER_CONFIGURED,
        key_id="user-mcp-config-v1",
        evidence_sha256="0" * 64,
        product_reviewed=False,
    )
    spec = tool.to_tool_spec(binding.extension_id, "1.0.0", provider=provider)
    assert spec.default_exposure is Exposure.DIRECT
    assert spec.approval_requirement is ApprovalRequirement.NEVER
    assert spec.input_schema["$schema"].endswith("draft/2020-12/schema")

    async def invoke() -> str:
        transport = await binding.session_factory("local-user")
        try:
            await discover_mcp_tools(transport)
            response = await transport.exchange(
                {
                    "jsonrpc": "2.0",
                    "id": "call-1",
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"text": "ready"}},
                },
                timeout_seconds=5,
                max_response_bytes=1024 * 1024,
            )
            return response["result"]["content"][0]["text"]
        finally:
            await transport.close()

    assert asyncio.run(invoke()) == "ready"
