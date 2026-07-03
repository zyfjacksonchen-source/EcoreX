"""File preview/stat/read Web handlers."""

import json
import mimetypes
import os
import urllib.parse

import web

from channel.web.handler_support import public_error_payload, require_auth, web_body_log_summary
from common.log import logger
from config import conf


def _legacy_web_channel():
    from channel.web import web_channel

    return web_channel


class UploadsHandler:
    def GET(self, file_name):
        require_auth()
        wc = _legacy_web_channel()
        try:
            upload_dir = os.path.realpath(wc._get_upload_dir())
            full_path = os.path.realpath(os.path.join(upload_dir, file_name))
            if not wc._is_within_directory(upload_dir, full_path):
                raise web.notfound()
            if not os.path.isfile(full_path):
                raise web.notfound()
            content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
            web.header("Content-Type", content_type)
            web.header("Cache-Control", "public, max-age=86400")
            with open(full_path, "rb") as handle:
                return handle.read()
        except web.HTTPError:
            raise
        except Exception as exc:
            logger.error(f"[WebChannel] Error serving upload: {web_body_log_summary(exc)}")
            raise web.notfound()


class FileServeHandler:
    def GET(self):
        require_auth()
        wc = _legacy_web_channel()
        try:
            params = web.input(path="")
            raw_path = params.path
            if not raw_path:
                raise web.notfound()
            workspace_root = os.path.realpath(os.path.expanduser(conf().get("agent_workspace", "~/cow")))
            expanded_raw_path = os.path.expanduser(raw_path)
            raw_was_absolute = os.path.isabs(expanded_raw_path)
            file_path = expanded_raw_path if raw_was_absolute else os.path.join(workspace_root, expanded_raw_path.lstrip("/\\"))
            file_path = os.path.realpath(file_path)
            upload_root = os.path.realpath(wc._get_upload_dir())
            allowed_roots = wc._web_file_preview_roots(workspace_root, upload_root)
            within_preview_root = any(wc._is_within_directory(root, file_path) for root in allowed_roots)
            if not within_preview_root and not (raw_was_absolute and wc._desktop_runtime_token_matches()):
                raise web.notfound()
            try:
                from common.ecorex_tool_permissions import get_tool_permission_broker

                decision = get_tool_permission_broker().authorize_file_access(
                    "read",
                    file_path,
                    cwd=workspace_root,
                )
            except Exception as exc:
                logger.warning(f"[WebChannel] file permission check failed: {web_body_log_summary(exc)}")
                raise web.notfound()
            if not wc._decision_allowed(decision):
                raise web.notfound()
            if not os.path.isfile(file_path):
                raise web.notfound()
            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            file_name = os.path.basename(file_path)
            from urllib.parse import quote

            web.header("Content-Type", content_type)
            web.header("Content-Disposition", f"inline; filename*=UTF-8''{quote(file_name)}")
            web.header("Cache-Control", "public, max-age=3600")
            with open(file_path, "rb") as handle:
                return handle.read()
        except web.HTTPError:
            raise
        except Exception as exc:
            logger.error(f"[WebChannel] Error serving file: {web_body_log_summary(exc)}")
            raise web.notfound()


class FileStatHandler:
    def POST(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        wc = _legacy_web_channel()
        try:
            raw = web.data() or b"{}"
            if len(raw) > 64 * 1024:
                return json.dumps({"status": "error", "path": "", "exists": False, "message": "payload too large"}, ensure_ascii=False)
            body = json.loads(raw)
            path_value = str(body.get("path") or body.get("file_path") or "").strip()
            if not path_value:
                return json.dumps({"status": "error", "path": "", "exists": False, "message": "path is required"}, ensure_ascii=False)
            if path_value.startswith("/api/file"):
                parsed = urllib.parse.urlparse(path_value)
                query = urllib.parse.parse_qs(parsed.query)
                path_value = (query.get("path") or [""])[0] or path_value
            if path_value.startswith("http://") or path_value.startswith("https://"):
                return json.dumps({"status": "remote", "path": path_value, "exists": True}, ensure_ascii=False)

            resolved = wc._resolve_web_local_path(path_value)
            try:
                from common.ecorex_tool_permissions import get_tool_permission_broker

                decision = get_tool_permission_broker().authorize_file_access(
                    "read",
                    resolved,
                    cwd=wc._get_workspace_root(),
                )
                if not wc._decision_allowed(decision):
                    return json.dumps({
                        "status": "denied",
                        "path": path_value,
                        "exists": False,
                        "message": wc._decision_reason(decision, "file stat blocked by permissions"),
                    }, ensure_ascii=False)
            except Exception as exc:
                logger.warning(f"[WebChannel] file stat permission check failed: {web_body_log_summary(exc)}")
                return json.dumps({"status": "error", "path": path_value, "exists": False, "message": "file stat permission check failed"}, ensure_ascii=False)

            if not os.path.exists(resolved):
                return json.dumps({"status": "missing", "path": path_value, "exists": False, "message": "path not found"}, ensure_ascii=False)

            is_file = os.path.isfile(resolved)
            payload = {
                "status": "success",
                "path": resolved,
                "exists": True,
                "isFile": is_file,
                "isDirectory": os.path.isdir(resolved),
                "mimeType": mimetypes.guess_type(resolved)[0] or "",
            }
            if is_file:
                payload["sizeBytes"] = os.path.getsize(resolved)
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] file stat error: {web_body_log_summary(exc)}")
            return json.dumps(public_error_payload("File status failed.", exc, path="", exists=False), ensure_ascii=False)


class FileJsonHandler:
    def POST(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        wc = _legacy_web_channel()
        try:
            raw = web.data() or b"{}"
            if len(raw) > 64 * 1024:
                return json.dumps({"status": "error", "path": "", "message": "payload too large"}, ensure_ascii=False)
            body = json.loads(raw)
            path_value = str(body.get("path") or body.get("file_path") or "").strip()
            if not path_value:
                return json.dumps({"status": "error", "path": "", "message": "path is required"}, ensure_ascii=False)
            if path_value.startswith("/api/file"):
                parsed = urllib.parse.urlparse(path_value)
                query = urllib.parse.parse_qs(parsed.query)
                path_value = (query.get("path") or [""])[0] or path_value
            if path_value.startswith("http://") or path_value.startswith("https://"):
                return json.dumps({"status": "error", "path": path_value, "message": "remote JSON status is not supported"}, ensure_ascii=False)

            resolved = wc._resolve_web_local_path(path_value)
            try:
                from common.ecorex_tool_permissions import get_tool_permission_broker

                decision = get_tool_permission_broker().authorize_file_access(
                    "read",
                    resolved,
                    cwd=wc._get_workspace_root(),
                )
                if not wc._decision_allowed(decision):
                    return json.dumps({
                        "status": "denied",
                        "path": path_value,
                        "message": wc._decision_reason(decision, "file JSON read blocked by permissions"),
                    }, ensure_ascii=False)
            except Exception as exc:
                logger.warning(f"[WebChannel] file JSON permission check failed: {web_body_log_summary(exc)}")
                return json.dumps({"status": "error", "path": path_value, "message": "file JSON permission check failed"}, ensure_ascii=False)

            if not os.path.isfile(resolved):
                return json.dumps({"status": "missing", "path": path_value, "message": "path not found"}, ensure_ascii=False)
            if os.path.getsize(resolved) > 256 * 1024:
                return json.dumps({"status": "error", "path": path_value, "message": "JSON file too large"}, ensure_ascii=False)
            if not resolved.lower().endswith(".json"):
                return json.dumps({"status": "error", "path": path_value, "message": "only JSON files can be read"}, ensure_ascii=False)
            with open(resolved, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            return json.dumps({"status": "success", "path": resolved, "data": data}, ensure_ascii=False)
        except json.JSONDecodeError as exc:
            return json.dumps({"status": "error", "path": "", "message": f"invalid JSON: {exc}"}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] file JSON read error: {web_body_log_summary(exc)}")
            return json.dumps(public_error_payload("File read failed.", exc, path=""), ensure_ascii=False)
