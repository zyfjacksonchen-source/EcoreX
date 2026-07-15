#!/usr/bin/env python3
"""Smoke real agent tool-call execution without depending on a live LLM."""

from __future__ import annotations

import json
import hashlib
import sys
import time
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]


class FakeAgent:
    def __init__(self):
        self.last_usage = None
        self.skill_manager = None
        self.memory_manager = None

    def refresh_skills(self):
        return None

    def _estimate_message_tokens(self, message):
        return max(1, len(json.dumps(message, ensure_ascii=False)) // 4)

    def _get_model_context_window(self):
        return 128000


class ToolCallingModel:
    def __init__(self, tool_name: str, arguments: Dict[str, Any], final_text: str):
        self.model = "fake-tool-calling-model"
        self.tool_name = tool_name
        self.arguments = arguments
        self.final_text = final_text
        self.calls: List[dict] = []

    def call_stream(self, request):
        tool_names = []
        for tool in request.tools or []:
            if isinstance(tool, dict):
                tool_names.append(str(tool.get("name") or tool.get("function", {}).get("name") or ""))
        self.calls.append({
            "toolNames": sorted(name for name in tool_names if name),
            "toolCount": len(tool_names),
            "hasTools": bool(request.tools),
        })
        if len(self.calls) == 1:
            yield {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": f"call_{self.tool_name}",
                            "function": {
                                "name": self.tool_name,
                                "arguments": json.dumps(self.arguments, ensure_ascii=False),
                            },
                        }]
                    },
                    "finish_reason": None,
                }]
            }
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
            return
        yield {"choices": [{"delta": {"content": self.final_text}, "finish_reason": None}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


def _result_status(events: list[dict], tool_name: str) -> tuple[str, Any]:
    for event in events:
        if event.get("event") == "tool_execution_end" and event.get("tool_name") == tool_name:
            return str(event.get("status")), event.get("result")
    return "missing", None


def run_scenario(label: str, tool, requested_name: str, args: Dict[str, Any], prompt: str, expected_tool: str) -> dict:
    from agent.protocol.agent_stream import AgentStreamExecutor

    events: list[dict] = []
    def collect_event(event: dict) -> None:
        data = event.get("data") if isinstance(event, dict) else {}
        events.append({
            "event": event.get("type") if isinstance(event, dict) else "unknown",
            **(data or {}),
        })

    model = ToolCallingModel(requested_name, args, f"{label} complete")
    executor = AgentStreamExecutor(
        agent=FakeAgent(),
        model=model,
        system_prompt="You are a tool-using test agent.",
        tools=[tool],
        max_turns=3,
        on_event=collect_event,
        messages=[],
    )
    final = executor.run_stream(prompt)
    status, result = _result_status(events, expected_tool)
    first_schema_names = set(model.calls[0]["toolNames"]) if model.calls else set()
    return {
        "label": label,
        "status": "PASS" if status == "success" and expected_tool in first_schema_names else "FAIL",
        "requestedTool": requested_name,
        "executedTool": expected_tool,
        "toolExecutionStatus": status,
        "firstSchemaContains": expected_tool in first_schema_names,
        "schemaToolCount": model.calls[0]["toolCount"] if model.calls else 0,
        "eventCount": len(events),
        "finalHash": hashlib.sha256(str(final or "").encode("utf-8")).hexdigest()[:12],
        "resultSummary": _public_summary(result),
    }


def _public_summary(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: value.get(key)
            for key in ("status", "urlCount", "provider", "model", "available", "authState")
            if key in value
        }
    text = str(value or "")
    return {"textLength": len(text), "textHash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]}


def main() -> int:
    sys.path.insert(0, str(ROOT))

    from agent.tools.bash.bash import Bash
    from agent.tools.feishu_cli.feishu_cli import FeishuCli
    from agent.tools.ocr.ocr import OcrTool
    from agent.tools.optional_abilities.optional_abilities import OptionalAbilities
    from common.log import logger

    logger.setLevel("ERROR")

    scenarios = [
        run_scenario(
            "bash executes real command",
            Bash({"cwd": str(ROOT), "safety_mode": False, "timeout": 10}),
            "shell",
            {"command": "echo ecorex_real_tool_call_ok", "timeout": 10},
            "run a shell command",
            "bash",
        ),
        run_scenario(
            "ocr text url returns browser handoff",
            OcrTool({"cwd": str(ROOT)}),
            "ocr",
            {"action": "extract_urls", "text": "open http://xhslink.com/o/8IkhCq7byEL"},
            "extract URL from text",
            "ocr",
        ),
        run_scenario(
            "optional abilities list callable",
            OptionalAbilities(),
            "optional_abilities",
            {"action": "list"},
            "list abilities",
            "optional_abilities",
        ),
        run_scenario(
            "feishu_cli status callable",
            FeishuCli({"cwd": str(ROOT), "timeout": 10}),
            "feishu_cli",
            {"action": "status", "timeout": 10},
            "check Feishu status",
            "feishu_cli",
        ),
    ]
    failed = [item for item in scenarios if item["status"] != "PASS"]
    result = {
        "status": "PASS" if not failed else "FAIL",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scenario": "real-tool-invocation-smoke",
        "checks": scenarios,
        "failed": [item["label"] for item in failed],
        "redacted": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
