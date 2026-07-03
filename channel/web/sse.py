"""SSE stream Web handlers."""

import json
import urllib.parse

import web

from channel.web.handler_support import require_auth


def _legacy_web_channel():
    from channel.web import web_channel

    return web_channel


class StreamHandler:
    def GET(self):
        require_auth()
        wc = _legacy_web_channel()
        env = getattr(getattr(web, "ctx", None), "env", {}) or {}
        origin = str(env.get("HTTP_ORIGIN") or "")
        desktop_runtime = wc._desktop_runtime_token_matches()
        if origin and not desktop_runtime:
            try:
                origin_host = urllib.parse.urlparse(origin).netloc
            except Exception:
                origin_host = ""
            if origin_host and origin_host != str(env.get("HTTP_HOST") or ""):
                raise web.HTTPError(
                    "401 Unauthorized",
                    {"Content-Type": "application/json; charset=utf-8"},
                    json.dumps({"status": "error", "message": "Unauthorized"}),
                )
        params = web.input(request_id="", session_id="", sessionId="")
        request_id = params.request_id
        session_id = str(getattr(params, "session_id", "") or getattr(params, "sessionId", "") or "")
        if not request_id:
            raise web.badrequest()

        web.header("Content-Type", "text/event-stream; charset=utf-8")
        web.header("Cache-Control", "no-cache")
        web.header("X-Accel-Buffering", "no")
        if desktop_runtime:
            web.header("Access-Control-Allow-Origin", origin or "*")

        return wc.WebChannel().stream_response(request_id, session_id=session_id)
