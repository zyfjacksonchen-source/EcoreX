"""Conversation session Web handlers."""

import json
import re

import web

from channel.web.handler_support import public_error_payload, require_auth, web_body_log_summary
from common.log import logger


def _legacy_web_channel():
    from channel.web import web_channel

    return web_channel


class SessionsHandler:
    def GET(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            params = web.input(
                page="1",
                page_size="50",
                include_ids="",
                include_session_ids="",
                include_pinned="",
                pinned_ids="",
            )
            from agent.memory import get_conversation_store

            store = get_conversation_store()
            include_ids_text = str(params.include_ids or params.include_session_ids or "")
            include_pinned = str(params.include_pinned or "").strip().lower() in {"1", "true", "yes", "on"}
            pinned_ids_text = str(params.pinned_ids or "") if include_pinned else ""
            include_session_ids = [
                item.strip()
                for item in re.split(r"[\s,]+", f"{include_ids_text},{pinned_ids_text}")
                if item.strip()
            ][:200]
            result = store.list_sessions(
                channel_type="web",
                page=int(params.page),
                page_size=int(params.page_size),
                include_session_ids=include_session_ids,
            )
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] Sessions API error: {web_body_log_summary(exc)}")
            return json.dumps(public_error_payload("Request failed.", exc))


class SessionDetailHandler:
    def DELETE(self, session_id: str):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        logger.info(f"[WebChannel] DELETE session request: {session_id}")
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            from agent.memory import get_conversation_store

            store = get_conversation_store()
            store.clear_session(session_id)
            try:
                from models.openai.responses_state_store import clear_responses_state_for_session

                removed = clear_responses_state_for_session(session_id)
                if removed:
                    logger.info(f"[WebChannel] Cleared Responses state for session {session_id}: removed={removed}")
            except Exception as exc:
                logger.warning(f"[WebChannel] Failed clearing Responses state for {session_id}: {web_body_log_summary(exc)}")

            try:
                from bridge.bridge import Bridge

                ab = Bridge().get_agent_bridge()
                if session_id in ab.agents:
                    del ab.agents[session_id]
                    logger.info(f"[WebChannel] Removed agent instance for session {session_id}")
            except Exception:
                pass

            _legacy_web_channel().WebChannel().session_queues.pop(session_id, None)

            logger.info(f"[WebChannel] Session deleted: {session_id}")
            return json.dumps({"status": "success"})
        except Exception as exc:
            logger.error(f"[WebChannel] Session delete error: {web_body_log_summary(exc)}")
            return json.dumps(public_error_payload("Request failed.", exc))

    def PUT(self, session_id: str):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})
            body = json.loads(web.data())
            title = body.get("title", "").strip()
            if not title:
                return json.dumps({"status": "error", "message": "title required"})

            from agent.memory import get_conversation_store

            store = get_conversation_store()
            found = store.rename_session(session_id, title, lock_title=True)
            if not found:
                return json.dumps({"status": "error", "message": "session not found"})
            return json.dumps({"status": "success"})
        except Exception as exc:
            logger.error(f"[WebChannel] Session rename error: {web_body_log_summary(exc)}")
            return json.dumps(public_error_payload("Request failed.", exc))


class SessionTitleHandler:
    def POST(self, session_id: str):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            raw_body = web.data()
            parsed_body = json.loads(raw_body) if raw_body else {}
            body = parsed_body if isinstance(parsed_body, dict) else {}
            user_message = body.get("user_message", "")
            assistant_reply = body.get("assistant_reply", "")
            session_summary = body.get("session_summary", "")

            from agent.memory import get_conversation_store

            store = get_conversation_store()
            title_state = store.get_session_title_state(session_id)
            if title_state and title_state.get("title_locked"):
                current_title = str(title_state.get("title") or "")
                logger.info(
                    f"[WebChannel] Session title locked: sid={session_id}, "
                    f"title_summary={web_body_log_summary(current_title)}"
                )
                return json.dumps({
                    "status": "success",
                    "title": current_title,
                    "updated": False,
                    "title_locked": True,
                    "titleLocked": True,
                }, ensure_ascii=False)
            from agent.chat.session_service import generate_session_title_for_store

            title = generate_session_title_for_store(
                store,
                session_id,
                user_message,
                assistant_reply,
                session_summary=session_summary,
            )
            updated = store.rename_session(session_id, title, respect_title_lock=True)
            logger.info(
                f"[WebChannel] Session title set: sid={session_id}, "
                f"title_summary={web_body_log_summary(title)}, db_updated={updated}"
            )

            return json.dumps({"status": "success", "title": title, "updated": updated}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] Title generation error: {web_body_log_summary(exc)}")
            return json.dumps(public_error_payload("Request failed.", exc))


class SessionClearContextHandler:
    def POST(self, session_id: str):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        try:
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            from agent.memory import get_conversation_store

            store = get_conversation_store()
            new_seq = store.clear_context(session_id)
            try:
                from models.openai.responses_state_store import clear_responses_state_for_session

                removed = clear_responses_state_for_session(session_id)
                if removed:
                    logger.info(f"[WebChannel] Cleared Responses state for session {session_id}: removed={removed}")
            except Exception as exc:
                logger.warning(f"[WebChannel] Failed clearing Responses state for {session_id}: {web_body_log_summary(exc)}")

            try:
                from bridge.bridge import Bridge

                bridge = Bridge()
                ab = bridge.get_agent_bridge()
                if session_id in ab.agents:
                    del ab.agents[session_id]
                    logger.info(f"[WebChannel] Cleared agent instance for session {session_id}")
            except Exception:
                pass

            return json.dumps({"status": "success", "context_start_seq": new_seq})
        except Exception as exc:
            logger.error(f"[WebChannel] Clear context error: {web_body_log_summary(exc)}")
            return json.dumps(public_error_payload("Request failed.", exc))


class HistoryHandler:
    def GET(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        web.header("Access-Control-Allow-Origin", "*")
        try:
            params = web.input(session_id="", page="1", page_size="20")
            session_id = params.session_id.strip()
            if not session_id:
                return json.dumps({"status": "error", "message": "session_id required"})

            from agent.memory import get_conversation_store

            store = get_conversation_store()
            result = store.load_history_page(
                session_id=session_id,
                page=int(params.page),
                page_size=int(params.page_size),
            )
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] History API error: {web_body_log_summary(exc)}")
            return json.dumps(public_error_payload("Request failed.", exc))


class MessageDeleteHandler:
    def POST(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        web.header("Access-Control-Allow-Origin", "*")
        try:
            data = json.loads(web.data())
            session_id = data.get("session_id", "").strip()
            user_seq = data.get("user_seq")
            delete_user = data.get("delete_user", True)
            cascade = data.get("cascade", False)

            if not session_id or user_seq is None:
                return json.dumps({"status": "error", "message": "session_id and user_seq required"})

            from agent.memory import get_conversation_store

            store = get_conversation_store()
            deleted = store.delete_message_pair(session_id, int(user_seq), delete_user=delete_user, cascade=cascade)

            try:
                from bridge import Bridge

                Bridge().get_agent_bridge().sync_session_messages_from_store(session_id)
            except Exception as sync_err:
                logger.warning(f"[WebChannel] Failed to sync agent memory: {web_body_log_summary(sync_err)}")

            return json.dumps({"status": "success", "deleted": deleted}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] Message delete error: {web_body_log_summary(exc)}")
            return json.dumps(public_error_payload("Request failed.", exc))


class UiStateHandler:
    MAX_UI_STATE_PAYLOAD_BYTES = 8 * 1024 * 1024

    def GET(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        wc = _legacy_web_channel()
        try:
            from common.ecorex_workspace import load_ui_state

            state = load_ui_state(wc._get_workspace_root())
            return json.dumps({"status": "success", "state": state}, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] UI state GET error: {web_body_log_summary(exc)}")
            return json.dumps(public_error_payload("Request failed.", exc))

    def _save(self):
        require_auth()
        web.header("Content-Type", "application/json; charset=utf-8")
        wc = _legacy_web_channel()
        try:
            raw = web.data() or b"{}"
            if len(raw) > self.MAX_UI_STATE_PAYLOAD_BYTES:
                return json.dumps({"status": "error", "message": "ui state payload too large"})
            body = json.loads(raw)
            incoming = body.get("state", body)
            if not isinstance(incoming, dict):
                return json.dumps({"status": "error", "message": "state must be an object"})
            from common.ecorex_workspace import save_ui_state

            state = save_ui_state(wc._get_workspace_root(), incoming)
            import_result = wc.WebChannel()._hydrate_conversation_store_from_ui_state(incoming)
            return json.dumps({
                "status": "success",
                "updatedAt": state.get("updatedAt"),
                "historyImport": import_result,
            }, ensure_ascii=False)
        except Exception as exc:
            logger.error(f"[WebChannel] UI state PUT error: {web_body_log_summary(exc)}")
            return json.dumps(public_error_payload("Request failed.", exc))

    def POST(self):
        return self._save()

    def PUT(self):
        return self._save()
