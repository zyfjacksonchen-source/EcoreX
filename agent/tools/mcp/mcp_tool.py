from agent.tools.base_tool import BaseTool, ToolResult
from common.log import logger
import json
import re


def _mask_sensitive(value) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    text = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-***", text)
    text = re.sub(r"gh[pousr]_[A-Za-z0-9_]{12,}", "ghp_***", text)
    text = re.sub(
        r"(?i)(api[_-]?key|token|password|secret|authorization)(\"?\s*[:=]\s*\"?)[^\",\s&}]+",
        lambda m: f"{m.group(1)}{m.group(2)}***",
        text,
    )
    return text[:1000]


class McpTool(BaseTool):
    """
    将单个 MCP 工具包装为 BaseTool。
    一个 MCP Server 可以提供多个工具，每个工具对应一个 McpTool 实例。
    """

    def __init__(self, client, tool_schema: dict, server_name: str, public_name: str = None):
        """
        :param client: 该工具所属的 McpClient 实例
        :param tool_schema: MCP 返回的工具描述，格式：
            {"name": str, "description": str, "inputSchema": dict}
        :param server_name: Server 名称，用于日志
        """
        self.client = client
        self.server_name = server_name
        self.remote_name = tool_schema["name"]
        self.name = public_name or self.remote_name
        self.description = tool_schema.get("description", "")
        self.params = tool_schema.get("inputSchema", {})

    def execute(self, params: dict) -> ToolResult:
        logger.info(
            f"[McpTool] server={self.server_name} tool={self.remote_name} "
            f"public={self.name} params={_mask_sensitive(params)}"
        )
        try:
            result = self.client.call_tool(
                self.remote_name,
                params,
                cancel_event=getattr(self, "cancel_event", None),
            )
            return ToolResult.success(result)
        except Exception as e:
            logger.error(f"[McpTool] server={self.server_name} tool={self.name} error: {e}")
            return ToolResult.fail(str(e))
