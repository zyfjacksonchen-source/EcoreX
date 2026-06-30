"""v0.2.5 skill/capability to tool binding contracts.

This module keeps the model-visible skill surface, extension registry, and
release checks on one contract.  A skill is callable only when it has a known
tool mapping, the mapped tool schema is loaded by ToolManager, and the binding
contract describes dependencies, probe, smoke, and failure guidance.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping, Optional

from agent.skills.tool_bridge import normalize_skill_tool_alias, resolve_callable_tool_name


CONTRACT_VERSION = "v0.2.5-skill-tool-binding-v1"
RUNTIME_PROBE_REQUIRED_TOOLS = {"feishu_cli", "tongxin_cli"}


def _dependency(kind: str, name: str, *, required: bool = True, source: str = "ecorex-owned") -> dict[str, Any]:
    return {
        "kind": kind,
        "name": name,
        "required": bool(required),
        "source": source,
    }


def _contract(
    tool_name: str,
    *,
    aliases: Iterable[str],
    purpose_group: str,
    dependencies: Iterable[Mapping[str, Any]],
    probe_action: str = "probe",
    smoke_action: str = "probe",
    failure_prompt: str,
    notes: str = "",
) -> dict[str, Any]:
    return {
        "schemaVersion": CONTRACT_VERSION,
        "toolName": tool_name,
        "aliases": [str(item) for item in aliases],
        "classification": {
            "sourceGroup": "builtin",
            "purposeGroup": purpose_group,
        },
        "dependencies": [dict(item) for item in dependencies],
        "probe": {
            "tool": tool_name,
            "action": probe_action,
            "kind": "readiness",
            "sideEffects": "none",
        },
        "smoke": {
            "tool": tool_name,
            "action": smoke_action,
            "kind": "minimal-runtime",
            "sideEffects": "none",
        },
        "failurePrompt": failure_prompt,
        "notes": notes,
    }


TOOL_BINDING_CONTRACTS: dict[str, dict[str, Any]] = {
    "bash": _contract(
        "bash",
        aliases=("bash", "shell", "terminal"),
        purpose_group="system",
        dependencies=(),
        probe_action="schema",
        smoke_action="schema",
        failure_prompt="Shell execution capability is not ready. Reload built-in tools and verify the bash tool schema is visible.",
    ),
    "read": _contract(
        "read",
        aliases=("read", "file-read"),
        purpose_group="system",
        dependencies=(),
        probe_action="schema",
        smoke_action="schema",
        failure_prompt="File read capability is not ready. Reload built-in tools and verify the read tool schema is visible.",
    ),
    "write": _contract(
        "write",
        aliases=("write", "file-write"),
        purpose_group="system",
        dependencies=(),
        probe_action="schema",
        smoke_action="schema",
        failure_prompt="File write capability is not ready. Reload built-in tools and verify the write tool schema is visible.",
    ),
    "edit": _contract(
        "edit",
        aliases=("edit", "file-edit", "patch"),
        purpose_group="system",
        dependencies=(),
        probe_action="schema",
        smoke_action="schema",
        failure_prompt="File edit capability is not ready. Reload built-in tools and verify the edit tool schema is visible.",
    ),
    "ls": _contract(
        "ls",
        aliases=("ls", "list-files"),
        purpose_group="system",
        dependencies=(),
        probe_action="schema",
        smoke_action="schema",
        failure_prompt="Directory listing capability is not ready. Reload built-in tools and verify the ls tool schema is visible.",
    ),
    "office_documents": _contract(
        "office_documents",
        aliases=("office-documents", "documents", "word", "docx"),
        purpose_group="office",
        dependencies=(
            _dependency("python-module", "docx"),
            _dependency("python-module", "markdownify"),
            _dependency("native-bin", "soffice", required=False),
        ),
        failure_prompt=(
            "Office document capability is not ready. Run the Office/PDF runtime probe "
            "and repair the bundled python modules or LibreOffice renderer before using this skill."
        ),
    ),
    "office_pdf": _contract(
        "office_pdf",
        aliases=("office-pdf", "pdf"),
        purpose_group="office",
        dependencies=(
            _dependency("python-module", "pypdf"),
            _dependency("python-module", "pdfminer"),
            _dependency("python-module", "fitz", required=False),
            _dependency("native-bin", "pdfinfo", required=False),
            _dependency("native-bin", "pdftoppm", required=False),
        ),
        failure_prompt=(
            "PDF capability is not ready. Run office_pdf action=probe and repair missing "
            "parser or render dependencies before claiming PDF QA passed."
        ),
    ),
    "office_presentations": _contract(
        "office_presentations",
        aliases=("office-presentations", "presentations", "powerpoint", "slides", "pptx"),
        purpose_group="office",
        dependencies=(
            _dependency("python-module", "pptx"),
            _dependency("python-module", "PIL"),
            _dependency("native-bin", "soffice", required=False),
        ),
        failure_prompt=(
            "Presentation capability is not ready. Run office_presentations action=probe "
            "and repair bundled presentation or preview dependencies before using this skill."
        ),
    ),
    "office_spreadsheets": _contract(
        "office_spreadsheets",
        aliases=("office-spreadsheets", "spreadsheets", "excel", "xlsx", "csv"),
        purpose_group="office",
        dependencies=(
            _dependency("python-module", "openpyxl"),
            _dependency("python-module", "xlsxwriter"),
            _dependency("native-bin", "soffice", required=False),
        ),
        failure_prompt=(
            "Spreadsheet capability is not ready. Run office_spreadsheets action=probe "
            "and repair bundled workbook modules or render dependencies before using this skill."
        ),
    ),
    "imagegen": _contract(
        "imagegen",
        aliases=("image-generation", "imagegen", "image"),
        purpose_group="image_media",
        dependencies=(
            _dependency("script", "skills/image-generation/scripts/generate.py"),
            _dependency("python-module", "requests"),
            _dependency("python-module", "PIL"),
            _dependency("python-module", "common.image_quality_runtime"),
            _dependency("env-any", "OPENAI_API_KEY|GEMINI_API_KEY|ARK_API_KEY|DASHSCOPE_API_KEY|MINIMAX_API_KEY|LINKAI_API_KEY", required=False),
        ),
        failure_prompt=(
            "Image generation capability is not ready. Run imagegen action=probe and repair "
            "the built-in script, provider configuration, or quality-check runtime."
        ),
    ),
    "feishu_cli": _contract(
        "feishu_cli",
        aliases=("feishu-cli", "lark-cli", "lark", "feishu"),
        purpose_group="collaboration",
        dependencies=(
            _dependency("native-bin", "node"),
            _dependency("native-bin", "npm"),
            _dependency("native-bin", "npx", required=False),
            _dependency("node-package", "@larksuite/cli"),
            _dependency("python-module", "lark_oapi", required=False),
        ),
        probe_action="status",
        smoke_action="status",
        failure_prompt=(
            "Feishu/Lark capability is not ready. Run feishu_cli action=diagnose and let "
            "the agent follow official lark-cli diagnostics before starting auth."
        ),
    ),
    "tongxin_cli": _contract(
        "tongxin_cli",
        aliases=("tongxin-cli", "xin-agent-cli", "tongxin", "xin_agent_cli"),
        purpose_group="data",
        dependencies=(
            _dependency("native-bin", "python"),
            _dependency("script", "xin_agent_cli.py"),
            _dependency("probe-command", "schema"),
            _dependency("probe-command", "project list --source cache --limit 1"),
        ),
        probe_action="status",
        smoke_action="status",
        failure_prompt=(
            "Tongxin CLI capability is not ready. Run tongxin_cli action=diagnose or "
            "auto_configure, then require the read-only schema and data health probes to pass."
        ),
    ),
    "browser": _contract(
        "browser",
        aliases=("browser-cdp", "browser-automation", "browser"),
        purpose_group="automation",
        dependencies=(
            _dependency("native-bin", "node", required=False),
            _dependency("python-module", "playwright", required=False),
        ),
        probe_action="status",
        smoke_action="status",
        failure_prompt=(
            "Browser automation is not ready. Run the browser or host diagnostics probe and "
            "repair CDP/Playwright runtime ownership before using browser actions."
        ),
    ),
    "find": _contract(
        "find",
        aliases=("find", "find-skill"),
        purpose_group="system",
        dependencies=(),
        probe_action="schema",
        smoke_action="schema",
        failure_prompt="Find capability is not ready. Reload built-in tools and verify the find tool schema is visible.",
    ),
    "ecorex_cli": _contract(
        "ecorex_cli",
        aliases=("ecorex-cli", "ecorex_cli"),
        purpose_group="system",
        dependencies=(),
        probe_action="status",
        smoke_action="status",
        failure_prompt="EcoreX CLI capability is not ready. Reload built-in tools and run ecorex_cli status diagnostics.",
    ),
    "host_diagnostics": _contract(
        "host_diagnostics",
        aliases=("host-diagnostics", "host_diagnostics"),
        purpose_group="system",
        dependencies=(),
        probe_action="status",
        smoke_action="status",
        failure_prompt="Host diagnostics capability is not ready. Reload built-in tools and verify host_diagnostics schema visibility.",
    ),
    "optional_abilities": _contract(
        "optional_abilities",
        aliases=("optional-abilities", "optional_abilities"),
        purpose_group="system",
        dependencies=(),
        probe_action="list",
        smoke_action="list",
        failure_prompt="Optional ability registry is not ready. Reload built-in tools and inspect optional_abilities action=list.",
    ),
    "scheduler": _contract(
        "scheduler",
        aliases=("scheduler",),
        purpose_group="automation",
        dependencies=(),
        probe_action="status",
        smoke_action="status",
        failure_prompt="Scheduler capability is not ready. Reload built-in tools and inspect scheduler status.",
    ),
}


ABILITY_CALLABLE_TOOL_ALIASES: dict[str, str] = {
    "find-skill": "find",
    "skill-creator": "",
    "ecorex-cli": "ecorex_cli",
    "host-diagnostics": "host_diagnostics",
    "chrome-devtools-mcp": "",
    "browser-cdp": "browser",
    "browser-automation": "browser",
    "feishu-cli": "feishu_cli",
    "feishu-lark": "feishu_cli",
    "office-pdf": "office_pdf",
    "tongxin-cli": "tongxin_cli",
    "scheduler": "scheduler",
    "self-evolution": "",
}


def tool_binding_contract(tool_name: Any) -> Optional[dict[str, Any]]:
    """Return a copy of the contract for a canonical tool name."""

    key = str(tool_name or "").strip()
    if not key:
        return None
    contract = TOOL_BINDING_CONTRACTS.get(key)
    return copy.deepcopy(contract) if contract else None


def _schema_probe_state(tool_name: str, loaded_tools: set[str]) -> dict[str, Any]:
    visible = bool(tool_name and tool_name in loaded_tools)
    status = "ready" if visible else "missing"
    if visible and tool_name in RUNTIME_PROBE_REQUIRED_TOOLS:
        status = "probe_required"
    return {
        "kind": "tool_manager_schema",
        "source": "ToolManager.list_tools",
        "status": status,
        "schemaVisible": visible,
        "runtimeProbeRequired": tool_name in RUNTIME_PROBE_REQUIRED_TOOLS,
        "redacted": True,
    }


def _normalize_probe_state(tool_name: str, state: Any, loaded_tools: set[str]) -> dict[str, Any]:
    if isinstance(state, Mapping):
        probe = dict(state)
        probe.setdefault("kind", "tool_probe")
        probe.setdefault("source", "tool.execute")
        probe.setdefault("redacted", True)
        if "status" not in probe:
            if probe.get("available") is True or probe.get("ok") is True:
                probe["status"] = "ready"
            elif probe.get("available") is False or probe.get("ok") is False:
                probe["status"] = "missing"
        if tool_name in RUNTIME_PROBE_REQUIRED_TOOLS:
            probe["runtimeProbeRequired"] = True
            if probe.get("runtimeProbePassed") is not True:
                status = str(probe.get("status") or "").strip().lower()
                if status in {"ready", "success", "ok", "configured", "healthy", "installed"} or probe.get("schemaVisible") is True:
                    probe["status"] = "partial"
                    probe["runtimeProbeAccepted"] = False
        return probe
    return _schema_probe_state(tool_name, loaded_tools)


def _status_from_probe(probe_state: Mapping[str, Any], *, enabled: bool) -> tuple[str, bool, str]:
    if not enabled:
        return "disabled", False, "skill or capability policy is disabled"
    status = str(probe_state.get("status") or "").strip().lower()
    if status in {"ready", "success", "ok", "configured", "healthy", "installed"}:
        return "ready", True, "mapped tool contract and schema probe are ready"
    if status in {"partial", "warn", "probe_required", "auth_incomplete", "needs_login", "needs_target_scope"}:
        return "partial", True, "mapped tool is callable but its runtime probe is partial"
    if status in {"missing", "not-installed", "not_installed", "failed", "error", "timeout"}:
        return "missing_dependency", False, "mapped tool probe reports missing runtime dependency"
    if probe_state.get("schemaVisible") is True:
        return "ready", True, "mapped tool schema is visible to the model"
    return "missing_dependency", False, "mapped tool probe did not report readiness"


def skill_tool_binding_surface(
    skill_or_name: Any,
    tool_names: Iterable[str],
    *,
    enabled: bool = True,
    tool_probe_states: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Project the callable tool surface plus the v0.2.5 binding contract."""

    tool_name = resolve_callable_tool_name(skill_or_name)
    loaded_tools = {str(item) for item in (tool_names or [])}
    if not tool_name:
        return {
            "tool": "",
            "toolName": "",
            "schemaVisible": False,
            "toolSchemaCallable": False,
            "callable": False,
            "status": "no_tool_mapping",
            "callableReason": "skill has no EcoreX callable tool mapping",
            "toolBinding": None,
        }

    contract = tool_binding_contract(tool_name)
    if not contract:
        return {
            "tool": tool_name,
            "toolName": tool_name,
            "schemaVisible": tool_name in loaded_tools,
            "toolSchemaCallable": False,
            "callable": False,
            "status": "missing_binding_contract",
            "callableReason": "mapped tool has no v0.2.5 binding contract",
            "toolBinding": {
                "schemaVersion": CONTRACT_VERSION,
                "toolName": tool_name,
                "status": "missing_binding_contract",
                "redacted": True,
            },
        }

    schema_visible = tool_name in loaded_tools
    raw_probe = (tool_probe_states or {}).get(tool_name) if tool_probe_states else None
    probe_state = _normalize_probe_state(tool_name, raw_probe, loaded_tools)
    if not schema_visible:
        binding_status = "tool_not_loaded"
        callable_value = False
        reason = "mapped tool is not loaded in the current agent snapshot"
    else:
        binding_status, callable_value, reason = _status_from_probe(probe_state, enabled=enabled)

    binding = {
        "schemaVersion": CONTRACT_VERSION,
        "toolName": tool_name,
        "status": binding_status,
        "dependencies": copy.deepcopy(contract.get("dependencies") or []),
        "probe": copy.deepcopy(contract.get("probe") or {}),
        "smoke": copy.deepcopy(contract.get("smoke") or {}),
        "failurePrompt": str(contract.get("failurePrompt") or ""),
        "classification": copy.deepcopy(contract.get("classification") or {}),
        "probeState": probe_state,
        "redacted": True,
    }
    return {
        "tool": tool_name,
        "toolName": tool_name,
        "schemaVisible": bool(schema_visible),
        "toolSchemaCallable": bool(schema_visible and contract),
        "callable": bool(callable_value),
        "status": binding_status,
        "callableReason": reason,
        "toolBinding": binding,
    }


def resolve_ability_tool_name(ability_or_name: Any) -> Optional[str]:
    """Return the canonical tool for an optional ability/capability pack."""

    candidates: list[Any] = []
    if isinstance(ability_or_name, Mapping):
        candidates.extend([
            ability_or_name.get("id"),
            ability_or_name.get("packId"),
            ability_or_name.get("label"),
            ability_or_name.get("name"),
        ])
    else:
        candidates.append(ability_or_name)
    for candidate in candidates:
        key = normalize_skill_tool_alias(candidate)
        if key in ABILITY_CALLABLE_TOOL_ALIASES:
            value = ABILITY_CALLABLE_TOOL_ALIASES[key]
            return value or None
        resolved = resolve_callable_tool_name(str(candidate or ""))
        if resolved:
            return resolved
    return None


def ability_tool_binding_surface(
    ability_or_name: Any,
    tool_names: Iterable[str],
    *,
    enabled: bool = True,
    tool_probe_states: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Project an optional ability onto the same tool binding contract surface."""

    tool_name = resolve_ability_tool_name(ability_or_name)
    if not tool_name:
        return {
            "tool": "",
            "toolName": "",
            "schemaVisible": False,
            "toolSchemaCallable": False,
            "callable": False,
            "status": "no_tool_mapping",
            "callableReason": "capability has no EcoreX callable tool mapping",
            "toolBinding": None,
        }
    return skill_tool_binding_surface(
        {"callable-tool": tool_name},
        tool_names,
        enabled=enabled,
        tool_probe_states=tool_probe_states,
    )


def release_contract_errors(surface: Mapping[str, Any]) -> list[str]:
    """Validate one projected surface for release-gate usage."""

    errors: list[str] = []
    binding = surface.get("toolBinding") if isinstance(surface.get("toolBinding"), Mapping) else None
    tool_name = str(surface.get("toolName") or surface.get("tool") or "")
    if not tool_name:
        return errors
    if not binding:
        return [f"{tool_name}: missing toolBinding payload"]
    for key in ("dependencies", "probe", "smoke", "failurePrompt", "classification", "probeState"):
        if key not in binding or binding.get(key) in (None, "", {}):
            errors.append(f"{tool_name}: missing binding field {key}")
    status = str(binding.get("status") or "")
    if status in {"missing_binding_contract", "tool_not_loaded"}:
        errors.append(f"{tool_name}: missing binding contract")
    if status == "ready" and not binding.get("probeState"):
        errors.append(f"{tool_name}: ready without probeState")
    return errors
