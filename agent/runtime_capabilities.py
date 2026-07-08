"""Single read-only runtime capability projection for Web and agent surfaces."""

from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.log import logger


SERVICE_VERSION = "web-runtime-goal-s6-v1"
DEFAULT_IMAGEGEN_MODEL = "gpt-image-2-pro"

_VISION_PROVIDER_KEYS = [
    ("openai", "open_ai_api_key"),
    ("gemini", "gemini_api_key"),
    ("minimax", "minimax_api_key"),
    ("zhipu", "zhipu_ai_api_key"),
    ("qianfan", "qianfan_api_key"),
    ("moonshot", "moonshot_api_key"),
    ("dashscope", "dashscope_api_key"),
    ("doubao", "ark_api_key"),
    ("linkai", "linkai_api_key"),
    ("custom", "custom_api_key"),
]

_IMAGE_PROVIDER_KEYS = [
    ("openai", "open_ai_api_key"),
    ("gemini", "gemini_api_key"),
    ("doubao", "ark_api_key"),
    ("dashscope", "dashscope_api_key"),
    ("minimax", "minimax_api_key"),
    ("linkai", "linkai_api_key"),
]


class RuntimeCapabilityRegistry:
    """Collect runtime facts without installing, enabling, or starting MCP servers."""

    def __init__(self, workspace_root: Optional[str] = None, *, probe_installer_status: bool = True):
        self.workspace_root = workspace_root or self._default_workspace_root()
        self.probe_installer_status = bool(probe_installer_status)

    @staticmethod
    def _default_workspace_root() -> str:
        try:
            from common.utils import expand_path
            from config import conf

            return expand_path(conf().get("agent_workspace", "~/EcoreX"))
        except Exception:
            return os.path.expanduser("~/EcoreX")

    def tools_payload(self) -> Dict[str, Any]:
        try:
            from agent.tools.tool_manager import ToolManager

            manager = ToolManager()
            if not getattr(manager, "tool_classes", None):
                manager.load_tools(start_mcp=False)
            ensure_mcp = getattr(manager, "ensure_mcp_configured_loaded", None)
            if callable(ensure_mcp):
                ensure_mcp(wait_seconds=0.0)
            tools = []
            for name, info in manager.list_tools().items():
                if not isinstance(info, dict):
                    info = {}
                tools.append({
                    "name": name,
                    "description": info.get("description", ""),
                    "parameters": info.get("parameters", {}),
                })
            registry = manager.registry_health() if hasattr(manager, "registry_health") else {}
            return {
                "status": "success",
                "source": "runtime-capability-service",
                "serviceVersion": SERVICE_VERSION,
                "tools": tools,
                "toolCount": len(tools),
                "registryStatus": registry.get("status") or ("ready" if tools else "error"),
                "registry": registry,
            }
        except Exception as exc:
            logger.warning(f"[RuntimeCapabilityRegistry] tools snapshot failed: {exc}")
            return {
                "status": "error",
                "source": "runtime-capability-service",
                "serviceVersion": SERVICE_VERSION,
                "message": str(exc),
                "tools": [],
                "toolCount": 0,
                "registryStatus": "error",
                "registry": {"status": "error", "errors": [{"message": str(exc)}]},
            }

    def skills_payload(self) -> Dict[str, Any]:
        try:
            from agent.skills.manager import SkillManager
            from agent.skills.service import SkillService

            manager = SkillManager(custom_dir=str(Path(self.workspace_root) / "skills"))
            service = SkillService(manager)
            skills = service.query()
            return {
                "status": "success",
                "source": "runtime-capability-service",
                "serviceVersion": SERVICE_VERSION,
                "skills": skills,
                "skillCount": len(skills) if isinstance(skills, list) else 0,
            }
        except Exception as exc:
            logger.warning(f"[RuntimeCapabilityRegistry] skills snapshot failed: {exc}")
            return {
                "status": "error",
                "source": "runtime-capability-service",
                "serviceVersion": SERVICE_VERSION,
                "message": str(exc),
                "skills": [],
                "skillCount": 0,
            }

    def optional_abilities_payload(self) -> Dict[str, Any]:
        try:
            from agent.tools.optional_abilities.optional_abilities import OptionalAbilities

            result = OptionalAbilities().execute({"action": "list"})
            payload = getattr(result, "result", result)
            if not isinstance(payload, dict):
                payload = {"status": "error", "message": "optional abilities returned a non-object payload"}
            if self.probe_installer_status:
                payload = _with_installer_status_probes(payload)
            return {
                **payload,
                "status": payload.get("status") or "success",
                "source": "runtime-capability-service",
                "serviceVersion": SERVICE_VERSION,
            }
        except Exception as exc:
            logger.warning(f"[RuntimeCapabilityRegistry] optional abilities snapshot failed: {exc}")
            return {
                "status": "error",
                "source": "runtime-capability-service",
                "serviceVersion": SERVICE_VERSION,
                "message": str(exc),
                "abilities": [],
            }

    def extensions_payload(self, action_plans: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        try:
            from agent.extensions import ExtensionRegistry

            payload = ExtensionRegistry(self.workspace_root).list_extensions()
            extensions = payload.get("extensions") if isinstance(payload, dict) else []
            if not isinstance(extensions, list):
                extensions = []
            plans = _plan_lookup(action_plans or [])
            enriched = [self._enrich_extension(entry, plans) for entry in extensions if isinstance(entry, dict)]
            return {
                **payload,
                "status": payload.get("status") or "success",
                "source": "runtime-capability-service",
                "serviceVersion": SERVICE_VERSION,
                "extensions": enriched,
                "count": len(enriched),
                "summary": _summary_by_type(enriched),
            }
        except Exception as exc:
            logger.warning(f"[RuntimeCapabilityRegistry] extensions snapshot failed: {exc}")
            return {
                "status": "error",
                "source": "runtime-capability-service",
                "serviceVersion": SERVICE_VERSION,
                "message": str(exc),
                "extensions": [],
                "count": 0,
                "summary": {},
            }

    @staticmethod
    def _enrich_extension(entry: Dict[str, Any], plans: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        result = _public_ability_item(entry)
        raw_state = entry.get("capabilityState") if isinstance(entry.get("capabilityState"), dict) else {}
        if raw_state:
            result["capabilityState"] = _public_capability_state(raw_state)
        ability_id = str(result.get("id") or "")
        if ability_id.startswith("ability:"):
            ability_id = ability_id.split(":", 1)[1]
        plan = plans.get(ability_id) or plans.get(str(result.get("packId") or ""))
        if not plan:
            return result
        result["actionPlan"] = _public_action_plan(plan)
        result["runtimeState"] = plan.get("state")
        result["nextAction"] = plan.get("nextAction")
        result["missingItems"] = plan.get("missingItems") or []
        result["retryable"] = bool(plan.get("retryable"))
        result["logRef"] = plan.get("logRef") or result.get("logRef") or {"present": False, "redacted": True}
        if plan.get("targetRef"):
            result["targetRef"] = plan.get("targetRef")
        result["capabilityState"] = plan.get("capabilityState") or result.get("capabilityState") or {}
        return result

    def snapshot(self) -> Dict[str, Any]:
        capabilities = CapabilityService(self).capabilities_payload(include_related=False)
        return {
            "status": "success",
            "source": "runtime-capability-service",
            "serviceVersion": SERVICE_VERSION,
            "workspace": self.workspace_root,
            "tools": self.tools_payload(),
            "skills": self.skills_payload(),
            "extensions": self.extensions_payload(capabilities.get("packs") or []),
            "capabilities": capabilities,
        }


class CapabilityService:
    """Typed action-plan projection over the runtime capability registry."""

    def __init__(self, registry: Optional[RuntimeCapabilityRegistry] = None, workspace_root: Optional[str] = None):
        self.registry = registry or RuntimeCapabilityRegistry(workspace_root)

    def capabilities_payload(self, *, include_related: bool = True) -> Dict[str, Any]:
        abilities_payload = self.registry.optional_abilities_payload()
        raw_abilities = abilities_payload.get("abilities") if isinstance(abilities_payload, dict) else []
        if not isinstance(raw_abilities, list):
            raw_abilities = []
        plans = [self._plan_for_ability(item) for item in raw_abilities if isinstance(item, dict)]
        tools_payload = self.registry.tools_payload() if include_related else None
        skills_payload = self.registry.skills_payload() if include_related else None
        payload = {
            "status": "success" if abilities_payload.get("status") == "success" else abilities_payload.get("status", "error"),
            "source": "runtime-capability-service",
            "serviceVersion": SERVICE_VERSION,
            "workspace": self.registry.workspace_root,
            "generatedAt": abilities_payload.get("generatedAt"),
            "policy": abilities_payload.get("policy"),
            "abilities": plans,
            "packs": plans,
            "abilityDiagnostics": {
                key: value
                for key, value in abilities_payload.items()
                if key not in {"abilities", "source", "serviceVersion"}
            },
            "summary": self._summary(plans),
            "visualWorkflow": self.visual_workflow_payload(
                plans,
                tools_payload=tools_payload,
                skills_payload=skills_payload,
            ),
        }
        if include_related:
            payload["tools"] = tools_payload
            payload["skills"] = skills_payload
            payload["extensions"] = self.registry.extensions_payload(plans)
        return payload

    def diagnose_payload(self) -> Dict[str, Any]:
        payload = self.capabilities_payload(include_related=True)
        return {
            "status": payload.get("status", "success"),
            "source": "runtime-capability-service",
            "serviceVersion": SERVICE_VERSION,
            "workspace": payload.get("workspace"),
            "abilities": {
                "status": payload.get("status", "success"),
                "abilities": payload.get("abilities") or [],
                "summary": payload.get("summary") or {},
                "source": "runtime-capability-service",
            },
            "skills": (payload.get("skills") or {}).get("skills", []),
            "tools": (payload.get("tools") or {}).get("tools", []),
            "extensions": (payload.get("extensions") or {}).get("extensions", []),
            "mcpStatus": (payload.get("tools") or {}).get("registry", {}).get("mcpStatus", {}),
            "summary": payload.get("summary") or {},
        }

    @staticmethod
    def _summary(plans: List[Dict[str, Any]]) -> Dict[str, int]:
        summary = {
            "total": len(plans),
            "ready": 0,
            "missing": 0,
            "repairable": 0,
            "needsConfiguration": 0,
            "discoveryOnly": 0,
            "disabled": 0,
        }
        for plan in plans:
            state = str(plan.get("state") or "")
            if state in {"ready", "installed", "enabled", "loaded"}:
                summary["ready"] += 1
            if state in {"missing_dependency", "not-installed", "failed", "missing_runtime_python"}:
                summary["missing"] += 1
            if plan.get("nextAction") == "repair":
                summary["repairable"] += 1
            if state == "needs_configuration":
                summary["needsConfiguration"] += 1
            if state == "discovery_only":
                summary["discoveryOnly"] += 1
            if state == "disabled":
                summary["disabled"] += 1
        return summary

    def _plan_for_ability(self, item: Dict[str, Any]) -> Dict[str, Any]:
        state = item.get("capabilityState") if isinstance(item.get("capabilityState"), dict) else {}
        pack_id = str(item.get("packId") or item.get("id") or "").strip()
        configured = bool(state.get("configured") or state.get("installed") or item.get("enabled"))
        disabled = item.get("policyMode") == "disabled" or item.get("installAllowed") is False and item.get("agentCanInstall") is False
        configure_only = bool(item.get("configureOnly") or state.get("configureOnly"))
        discovery_only = bool(item.get("discoveryOnly") or state.get("state") == "discovery_only")
        if pack_id == "feishu-lark" and not state.get("installed"):
            discovery_only = True
        raw_state = str(state.get("state") or "").strip()
        missing = _missing_items(item, state)

        if disabled and not state.get("installed"):
            plan_state = "disabled"
        elif configure_only and not configured:
            plan_state = "needs_configuration"
        elif discovery_only:
            plan_state = "discovery_only"
        elif state.get("installed") or raw_state == "installed" or item.get("enabled") and not item.get("packId"):
            plan_state = "ready"
        elif raw_state:
            plan_state = raw_state
        elif item.get("kind") == "built-in":
            plan_state = "ready"
        elif item.get("kind") == "planned":
            plan_state = "planned"
        else:
            plan_state = "not-installed"

        next_action = _next_action(item, state, plan_state)
        message = _diagnostic_summary(item, state, plan_state)
        public_state = _public_capability_state(state)
        log_ref = _log_ref(state.get("logPath"))
        target_ref = _path_ref(state.get("targetDir"))
        result = {
            **_public_ability_item(item),
            "packId": pack_id,
            "state": plan_state,
            "missingItems": missing,
            "nextAction": next_action,
            "actionLabel": _action_label(next_action),
            "retryable": next_action in {"repair", "install", "configure", "discover", "enable"},
            "diagnosticSummary": message,
            "logRef": log_ref,
            "targetRef": target_ref,
            "capabilityState": public_state,
            "actionPlan": {
                "state": plan_state,
                "missingItems": missing,
                "nextAction": next_action,
                "actionLabel": _action_label(next_action),
                "retryable": next_action in {"repair", "install", "configure", "discover", "enable"},
                "diagnosticSummary": message,
                "logRef": log_ref,
                "targetRef": target_ref,
            },
        }
        return result

    def visual_workflow_payload(
        self,
        plans: Optional[List[Dict[str, Any]]] = None,
        *,
        tools_payload: Optional[Dict[str, Any]] = None,
        skills_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        plans = plans or []
        by_pack = {str(plan.get("packId") or plan.get("id") or ""): plan for plan in plans if isinstance(plan, dict)}
        config = _load_runtime_config()
        tool_names = _tool_names(tools_payload)
        skill_names = _skill_names(skills_payload)

        ocr_plan = by_pack.get("fast-ocr") or {}
        ocr = _visual_ocr_state(ocr_plan, tool_names)
        vision = _visual_model_state(
            "vision",
            provider_keys=_VISION_PROVIDER_KEYS,
            route_visible=_route_visible(tool_names, {"vision"}),
            config=config,
        )
        imagegen = _visual_model_state(
            "imagegen",
            provider_keys=_IMAGE_PROVIDER_KEYS,
            route_visible=_route_visible(tool_names, {"imagegen"}) or _route_visible(skill_names, {"image-generation"}),
            config=config,
            model=DEFAULT_IMAGEGEN_MODEL,
        )

        blocking = []
        for item in (ocr, vision, imagegen):
            if item.get("nextAction") not in {"", "none", "use_vision_fallback"}:
                blocking.append({
                    "capability": item.get("capability"),
                    "state": item.get("state"),
                    "nextAction": item.get("nextAction"),
                    "actionLabel": item.get("actionLabel"),
                })
        ready = all(item.get("state") == "ready" for item in (ocr, vision, imagegen))
        degraded = ocr.get("state") != "ready" and vision.get("state") == "ready"
        return {
            "schemaVersion": "visual-workflow-v1",
            "imageInput": {
                "supported": True,
                "autoDetect": True,
                "acceptedMimePrefixes": ["image/"],
                "attachmentTypes": ["image"],
            },
            "ocr": ocr,
            "vision": vision,
            "imagegen": imagegen,
            "overall": {
                "state": "ready" if ready else ("degraded" if degraded else "needs_configuration"),
                "ready": ready,
                "visionFallbackAvailable": bool(ocr.get("state") != "ready" and vision.get("state") == "ready"),
                "blockingItems": blocking,
            },
        }


def _with_installer_status_probes(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Merge S3 installer status facts without installing or repairing anything."""

    abilities = payload.get("abilities")
    if not isinstance(abilities, list):
        return payload
    changed = False
    enriched = []
    for item in abilities:
        if not isinstance(item, dict):
            enriched.append(item)
            continue
        next_item = dict(item)
        state = next_item.get("capabilityState") if isinstance(next_item.get("capabilityState"), dict) else {}
        if _should_probe_installer_status(next_item, state):
            pack_id = str(next_item.get("packId") or next_item.get("id") or "").strip()
            status_state = _installer_status_state(pack_id)
            if status_state:
                next_item["capabilityState"] = {**state, **status_state}
                changed = True
        enriched.append(next_item)
    return {**payload, "abilities": enriched} if changed else payload


def _load_runtime_config() -> Dict[str, Any]:
    try:
        from config import conf

        cfg = conf()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _is_real_key(value: Any) -> bool:
    raw = str(value or "").strip()
    return bool(raw) and raw not in {"YOUR API KEY", "YOUR_API_KEY"}


def _tool_names(payload: Optional[Dict[str, Any]]) -> set[str]:
    tools = payload.get("tools") if isinstance(payload, dict) else []
    names = set()
    if isinstance(tools, list):
        for item in tools:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item.get("name")).strip().lower())
    return names


def _skill_names(payload: Optional[Dict[str, Any]]) -> set[str]:
    skills = payload.get("skills") if isinstance(payload, dict) else []
    names = set()
    if isinstance(skills, list):
        for item in skills:
            if isinstance(item, dict):
                for key in ("name", "id", "toolName"):
                    if item.get(key):
                        names.add(str(item.get(key)).strip().lower())
    return names


def _route_visible(names: set[str], candidates: set[str]) -> bool:
    if not names:
        return False
    return bool({str(item).strip().lower() for item in candidates} & names)


def _configured_provider(config: Dict[str, Any], provider_keys: List[tuple[str, str]]) -> str:
    for provider, key_field in provider_keys:
        if _is_real_key(config.get(key_field)):
            return provider
    return ""


def _visual_ocr_state(plan: Dict[str, Any], tool_names: set[str]) -> Dict[str, Any]:
    state = str(plan.get("state") or "").strip()
    ready = state in {"ready", "installed", "enabled", "loaded"}
    route_visible = _route_visible(tool_names, {"ocr"}) or ready
    if ready:
        return {
            "capability": "ocr",
            "state": "ready",
            "routeVisible": route_visible,
            "missingItems": [],
            "nextAction": "none",
            "actionLabel": _action_label("none"),
            "retryable": False,
            "diagnosticSummary": "Fast OCR is ready for uploaded image text extraction.",
        }
    missing = plan.get("missingItems") or []
    return {
        "capability": "ocr",
        "state": state or "missing_dependency",
        "routeVisible": route_visible,
        "missingItems": missing,
        "nextAction": "repair_fast_ocr",
        "actionLabel": _action_label("repair_fast_ocr"),
        "retryable": True,
        "diagnosticSummary": plan.get("diagnosticSummary") or "Fast OCR runtime dependencies are missing.",
        "sourcePackId": plan.get("packId") or "fast-ocr",
        "logRef": plan.get("logRef") or {"present": False, "redacted": True},
    }


def _visual_model_state(
    capability: str,
    *,
    provider_keys: List[tuple[str, str]],
    route_visible: bool,
    config: Dict[str, Any],
    model: str = "",
) -> Dict[str, Any]:
    provider = _configured_provider(config, provider_keys)
    if not provider:
        result = {
            "capability": capability,
            "state": "needs_provider_credentials",
            "routeVisible": bool(route_visible),
            "providerConfigured": False,
            "configuredProvider": "",
            "missingItems": ["provider_credentials"],
            "nextAction": "configure_model_provider",
            "actionLabel": _action_label("configure_model_provider"),
            "retryable": True,
            "diagnosticSummary": f"{capability} is available but needs model provider credentials.",
        }
        if model:
            result["model"] = model
        return result
    if not route_visible:
        result = {
            "capability": capability,
            "state": "missing_tool",
            "routeVisible": False,
            "providerConfigured": True,
            "configuredProvider": provider,
            "missingItems": ["tool_route"],
            "nextAction": "repair_runtime_tool",
            "actionLabel": _action_label("repair_runtime_tool"),
            "retryable": True,
            "diagnosticSummary": f"{capability} provider credentials are configured, but the tool route is not visible.",
        }
        if model:
            result["model"] = model
        return result
    result = {
        "capability": capability,
        "state": "ready",
        "routeVisible": True,
        "providerConfigured": True,
        "configuredProvider": provider,
        "missingItems": [],
        "nextAction": "none",
        "actionLabel": _action_label("none"),
        "retryable": False,
        "diagnosticSummary": f"{capability} provider credentials and route are ready.",
    }
    if model:
        result["model"] = model
    return result


def _should_probe_installer_status(item: Dict[str, Any], state: Dict[str, Any]) -> bool:
    pack_id = str(item.get("packId") or item.get("id") or "").strip()
    if not pack_id:
        return False
    if pack_id == "feishu-lark":
        return False
    if item.get("discoveryOnly") or state.get("discoveryOnly") or state.get("state") == "discovery_only":
        return False
    if item.get("configureOnly") or state.get("configureOnly"):
        return False
    if state.get("installed") or state.get("state") in {"installed", "ready"}:
        return False
    if state.get("missingModules"):
        return False
    if not (item.get("packId") or item.get("kind") == "capability-pack"):
        return False
    return str(state.get("state") or "").strip() in {"", "not-installed", "unknown", "missing_dependency"}


def _installer_status_state(pack_id: str) -> Optional[Dict[str, Any]]:
    try:
        from agent.tools.optional_abilities import optional_abilities as optional_module

        runtime_root = optional_module.RUNTIME_ROOT
        manifest = optional_module._capability_manifest_path()
        if not manifest:
            return None
        installer = runtime_root / "scripts" / "install-capability.py"
        if not installer.exists():
            installer = runtime_root / "desktop" / "scripts" / "install-capability.py"
        if not installer.exists():
            return None
        state_dir = optional_module._state_dir()
        target_root = optional_module._capability_package_root()
        target_dir = optional_module._capability_target_dir(pack_id)
        state_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "ECOREX_CAPABILITY_STATE_DIR": str(state_dir),
            "ECOREX_CAPABILITY_TARGET_DIR": str(target_root),
        })
        if "ECOREX_STATE_DIR" not in env and state_dir.name == "capability-state":
            env["ECOREX_STATE_DIR"] = str(state_dir.parent)
        result = subprocess.run(
            [
                sys.executable,
                str(installer),
                "--action",
                "status",
                "--pack-id",
                pack_id,
                "--runtime-dir",
                str(runtime_root),
                "--manifest",
                str(manifest),
                "--index-dir",
                str(state_dir),
                "--target-dir",
                str(target_dir),
                "--timeout",
                "30",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            env=env,
            cwd=str(runtime_root),
            timeout=35,
        )
        if result.stdout:
            try:
                payload = json.loads(result.stdout)
                state = payload.get("capabilityState") if isinstance(payload, dict) else None
                if isinstance(state, dict):
                    return state
            except Exception:
                logger.debug(f"[RuntimeCapabilityRegistry] status probe JSON parse failed for {pack_id}")
        state_path = state_dir / f"{pack_id}.json"
        if state_path.exists():
            data = json.loads(state_path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else None
        if result.returncode != 0:
            logger.debug(f"[RuntimeCapabilityRegistry] status probe failed for {pack_id}: {result.returncode}")
    except Exception as exc:
        logger.debug(f"[RuntimeCapabilityRegistry] status probe skipped for {pack_id}: {exc}")
    return None


_SENSITIVE_PATH_KEYS = {
    "logPath",
    "targetDir",
    "stateDir",
    "targetRoot",
    "manifestPath",
    "configPath",
    "stdout",
    "stderr",
    "output",
}


def _public_ability_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in item.items()
        if key not in _SENSITIVE_PATH_KEYS and key != "capabilityState"
    }


def _public_capability_state(state: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "installed",
        "state",
        "updatedAt",
        "message",
        "available",
        "configured",
        "configurationState",
        "builtIn",
        "configureOnly",
        "readOnly",
        "defaultEnabled",
        "installHint",
        "configKey",
        "missingModules",
        "discoveryOnly",
        "sourceConfigured",
        "mirrorConfigured",
        "retryable",
        "nextAction",
        "repairAction",
        "configureAction",
        "policyMode",
        "installAllowed",
        "agentCanInstall",
        "disabledReason",
        "policySource",
        "policyUpdatedAt",
        "packIdRedacted",
    }
    public = {key: value for key, value in state.items() if key in allowed}
    public["logRef"] = _log_ref(state.get("logPath"))
    public["targetRef"] = _path_ref(state.get("targetDir"))
    return public


def _missing_items(item: Dict[str, Any], state: Dict[str, Any]) -> List[str]:
    values = state.get("missingModules") or item.get("missingModules") or item.get("requires") or []
    if isinstance(values, dict):
        values = [str(key) for key, value in values.items() if value in (False, None, "missing")]
    if not isinstance(values, list):
        values = [values]
    return [str(value) for value in values if str(value or "").strip()]


def _next_action(item: Dict[str, Any], state: Dict[str, Any], plan_state: str) -> str:
    if plan_state == "disabled":
        return "blocked"
    if plan_state == "ready":
        return "none"
    if plan_state == "needs_configuration":
        return "configure"
    if plan_state == "discovery_only":
        return "discover"
    if plan_state in {"missing_dependency", "failed", "missing_runtime_python"}:
        return "repair"
    if plan_state == "planned":
        return "plan"
    if item.get("kind") == "optional-runtime" and not item.get("enabled"):
        return "enable"
    if item.get("agentCanInstall") or item.get("packId"):
        return state.get("nextAction") or "install"
    return state.get("nextAction") or "none"


def _action_label(action: str) -> str:
    return {
        "blocked": "Blocked by policy",
        "configure": "Configure",
        "discover": "Discover",
        "enable": "Enable",
        "install": "Install",
        "none": "Ready",
        "plan": "Open plan",
        "repair": "Repair",
        "repair_fast_ocr": "Repair Fast OCR",
        "configure_model_provider": "Configure model provider",
        "repair_runtime_tool": "Repair runtime tool",
    }.get(str(action or ""), "Review")


def _diagnostic_summary(item: Dict[str, Any], state: Dict[str, Any], plan_state: str) -> str:
    for key in ("message", "installHint", "notes", "disabledReason"):
        value = state.get(key) if key in state else item.get(key)
        if value:
            return str(value)
    return f"{item.get('label') or item.get('id') or 'Capability'} state: {plan_state}"


def _log_ref(value: Any) -> Dict[str, Any]:
    return _path_ref(value)


def _path_ref(value: Any) -> Dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        return {"present": False, "redacted": True}
    try:
        path = Path(raw)
        return {
            "present": True,
            "name": path.name,
            "parentName": path.parent.name,
            "redacted": True,
        }
    except Exception:
        return {"present": True, "name": "", "redacted": True}


def _public_action_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "state": plan.get("state"),
        "missingItems": plan.get("missingItems") or [],
        "nextAction": plan.get("nextAction"),
        "actionLabel": plan.get("actionLabel"),
        "retryable": bool(plan.get("retryable")),
        "diagnosticSummary": plan.get("diagnosticSummary") or "",
        "logRef": plan.get("logRef") or {"present": False, "redacted": True},
        "targetRef": plan.get("targetRef") or {"present": False, "redacted": True},
    }


def _plan_lookup(plans: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        for key in (plan.get("id"), plan.get("packId")):
            if key:
                result[str(key)] = plan
    return result


def _summary_by_type(entries: List[Dict[str, Any]]) -> Dict[str, int]:
    summary: Dict[str, int] = {}
    for entry in entries:
        etype = str(entry.get("type") or "unknown")
        summary[etype] = summary.get(etype, 0) + 1
    return summary
