from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Set


IMAGEGEN_COMPANION_GROUPS = {"browser", "web", "ocr", "workspace"}
IMAGEGEN_WORKSPACE_TOOLS = {"read", "ls", "find"}


@dataclass
class ToolRouterPolicy:
    """Backend-owned tool exposure policy used by v0.3.0 routing.

    The model schema budget can be small, but the policy must remain explicit:
    semantic image work gets the imagegen route, while evidence-gathering tools
    may remain visible when the user intent asks for links, browser, OCR, or
    local workspace discovery. Shell stays out of semantic image work.
    """

    imagegen_companion_groups: Set[str] = field(default_factory=lambda: set(IMAGEGEN_COMPANION_GROUPS))
    imagegen_workspace_tools: Set[str] = field(default_factory=lambda: set(IMAGEGEN_WORKSPACE_TOOLS))

    def companion_groups_for_imagegen(self, intent_groups: Iterable[str]) -> Set[str]:
        return set(intent_groups or set()).intersection(self.imagegen_companion_groups)

    def allows_imagegen_companion_tool(self, tool_name: str, group: str, companion_groups: Iterable[str]) -> bool:
        groups = set(companion_groups or set())
        lowered = str(tool_name or "").strip().lower()
        if group in groups and group != "core":
            return True
        return "workspace" in groups and lowered in self.imagegen_workspace_tools

    def selection_metadata(self, selected: Dict[str, object], deferred: Dict[str, object], reasons: Dict[str, str]) -> Dict[str, object]:
        return {
            "selected_tools": sorted(selected.keys()),
            "deferred_tools": sorted(deferred.keys()),
            "selected_count": len(selected),
            "deferred_count": len(deferred),
            "selection_reasons": dict(reasons),
        }
