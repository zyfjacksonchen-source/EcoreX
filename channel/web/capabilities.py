"""Capability, tool, skill, and extension Web handlers."""

import json
import os

import web

from channel.web.handler_support import (
    get_workspace_root,
    public_error_payload,
    public_exception_message,
    public_exception_summary,
    require_auth,
    web_body_log_summary,
)
from common.log import logger


def _runtime_capability_registry(*, probe_installer_status=True):
    from agent.runtime_capabilities import RuntimeCapabilityRegistry

    workspace_root = get_workspace_root()
    try:
        return RuntimeCapabilityRegistry(workspace_root, probe_installer_status=probe_installer_status)
    except TypeError as exc:
        if "probe_installer_status" not in str(exc):
            raise
        return RuntimeCapabilityRegistry(workspace_root)


class CapabilitiesHandler:
    def GET(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            from agent.runtime_capabilities import CapabilityService

            payload = CapabilityService(_runtime_capability_registry(probe_installer_status=True)).capabilities_payload()
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] capability status failed: {web_body_log_summary(exc)}")
            return json.dumps({
                "status": "error",
                "message": public_exception_message("Capability status unavailable.", exc),
                **public_exception_summary(exc),
            }, ensure_ascii=False)


class ExtensionsHandler:
    def GET(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            from agent.runtime_capabilities import CapabilityService

            registry = _runtime_capability_registry(probe_installer_status=True)
            plans = CapabilityService(registry).capabilities_payload(include_related=False).get("packs") or []
            return json.dumps(registry.extensions_payload(plans), ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] extensions status failed: {web_body_log_summary(exc)}")
            return json.dumps({
                "status": "error",
                "message": public_exception_message("Extensions status unavailable.", exc),
                **public_exception_summary(exc),
                "extensions": [],
            }, ensure_ascii=False)


class ToolsHandler:
    def GET(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            return json.dumps(_runtime_capability_registry().tools_payload(), ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] Tools API error: {web_body_log_summary(exc)}")
            return json.dumps(public_error_payload("Request failed.", exc), ensure_ascii=False)


class SkillsHandler:
    def GET(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            return json.dumps(_runtime_capability_registry().skills_payload(), ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] Skills API error: {web_body_log_summary(exc)}")
            return json.dumps(public_error_payload("Request failed.", exc), ensure_ascii=False)

    def POST(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            from agent.skills.manager import SkillManager
            from agent.skills.service import SkillService

            body = json.loads(web.data())
            action = body.get("action")
            name = body.get("name")
            if not action or not name:
                return json.dumps({"status": "error", "message": "action and name are required"})
            workspace_root = get_workspace_root()
            manager = SkillManager(custom_dir=os.path.join(workspace_root, "skills"))
            service = SkillService(manager)
            if action == "open":
                service.open({"name": name})
            elif action == "close":
                service.close({"name": name})
            else:
                return json.dumps({"status": "error", "message": f"unknown action: {action}"})
            return json.dumps({"status": "success"}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] Skills POST error: {web_body_log_summary(exc)}")
            return json.dumps(public_error_payload("Request failed.", exc), ensure_ascii=False)
