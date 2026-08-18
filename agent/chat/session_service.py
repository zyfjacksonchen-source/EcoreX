"""
SessionService - Manages multi-session lifecycle for both web channel and cloud client.

Provides a unified interface for listing, deleting, renaming, clearing context,
and generating AI titles for conversation sessions. Backed by ConversationStore
(SQLite) and AgentBridge (in-memory agent instances).
"""

import re
import hashlib
from typing import Any, List, Optional

from common.log import logger


def _clean_title_candidate(value: str) -> str:
    text = str(value or "").strip().strip('"\'')
    if not text:
        return ""
    for _ in range(3):
        cleaned = re.sub(
            r"^\s*(?:[-*]\s*)?(?:User|Assistant|用户|助手|Recent conversation|Session summary|会话摘要|最近对话)\s*[:：]\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned == text:
            break
        text = cleaned
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*]\s*)", "", line).strip()
        line = re.sub(r"^(?:User|Assistant|用户|助手)\s*[:：]\s*", "", line, flags=re.IGNORECASE).strip()
        if line:
            return line
    return text


def _truncate_fallback_title(user_message: str, max_len: int = 30) -> str:
    """Pick the first non-empty line of the user message and truncate it."""
    if not user_message:
        return "New Chat"
    first_line = ""
    for line in user_message.splitlines():
        line = _clean_title_candidate(line)
        if line:
            first_line = line
            break
    if not first_line:
        return "New Chat"
    if len(first_line) > max_len:
        first_line = first_line[:max_len].rstrip() + "..."
    return first_line


def _extract_title_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


def _title_log_summary(value: str) -> str:
    text = str(value or "")
    if not text:
        return "hash= chars=0 bytes=0"
    encoded = text.encode("utf-8", errors="replace")
    return f"hash={hashlib.sha256(encoded).hexdigest()[:16]} chars={len(text)} bytes={len(encoded)}"


def _format_messages_for_title(messages: List[dict], max_chars: int = 2600) -> str:
    lines: List[str] = []
    for item in messages[-24:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        text = _extract_title_text(item.get("content", ""))
        if not text:
            continue
        label = "User" if role == "user" else "Assistant"
        lines.append(f"{label}: {text[:500]}")
    context = "\n".join(lines).strip()
    if len(context) > max_chars:
        context = context[-max_chars:]
    return context


def _fallback_title_from_session_context(
    user_message: str = "",
    assistant_reply: str = "",
    *,
    session_summary: str = "",
    conversation_context: str = "",
) -> str:
    summary_candidates: List[str] = []
    for source in (session_summary, assistant_reply, conversation_context):
        for line in str(source or "").splitlines():
            clean = _clean_title_candidate(line)
            if not clean:
                continue
            if re.search(r"^(?:总结|摘要|本会话|当前会话|聚焦|围绕)\s*[:：]?", clean):
                clean = re.sub(r"^(?:总结|摘要)\s*[:：]\s*", "", clean).strip()
                summary_candidates.append(clean)
            elif "总结" in clean or "聚焦" in clean:
                summary_candidates.append(clean)
    for source in (*summary_candidates, session_summary, user_message, assistant_reply, conversation_context):
        title = _truncate_fallback_title(str(source or ""), max_len=30)
        if title != "New Chat":
            return title
    return "New Chat"


def generate_session_title(
    user_message: str = "",
    assistant_reply: str = "",
    *,
    conversation_messages: Optional[List[dict]] = None,
    session_summary: str = "",
) -> str:
    """Return the deterministic title fallback without selecting a provider."""
    conversation_context = _format_messages_for_title(conversation_messages or [])
    return _fallback_title_from_session_context(
        user_message,
        assistant_reply,
        session_summary=session_summary,
        conversation_context=conversation_context,
    )


async def generate_managed_session_title(
    user_message: str,
    assistant_reply: str,
    *,
    gateway: Any,
    thread_id: str,
    turn_id: str,
    model_id: str,
    model_policy: Any,
    identity_instruction: str,
    config_snapshot_id: str = "title_config_v1",
    capability_snapshot_id: str = "title_capabilities_v1",
    permission_snapshot_id: str = "title_permissions_v1",
    model_catalog_snapshot_id: str | None = None,
) -> str:
    """Generate one no-tool title through the Runtime's authenticated Gateway."""
    fallback = generate_session_title(user_message, assistant_reply)
    prompt = (
        "Generate a very short title (max 15 characters for Chinese, max 6 words for English) "
        "from the overall completed exchange, not from only one message. "
        "Return ONLY the title text, nothing else.\n\n"
        f"User: {user_message[:1200]}\nAssistant: {assistant_reply[:1200]}"
    )
    try:
        from ecorex.gateway import (
            GatewayEventType,
            GatewayUserMessageInput,
            ModelGatewayRequest,
        )

        scope = hashlib.sha256(f"{thread_id}\0{turn_id}".encode()).hexdigest()[:24]
        request = ModelGatewayRequest(
            request_id=f"title_{scope}",
            thread_id=thread_id,
            turn_id=turn_id,
            trace_id=f"title_trace_{scope}",
            model_id=model_id,
            model_policy=model_policy,
            instructions=identity_instruction,
            model_catalog_snapshot_id=model_catalog_snapshot_id,
            input_items=[
                GatewayUserMessageInput(
                    message_id=f"title_message_{scope}",
                    content=prompt,
                )
            ],
            config_snapshot_id=config_snapshot_id,
            capability_snapshot_id=capability_snapshot_id,
            permission_snapshot_id=permission_snapshot_id,
            direct_tools=[],
            deferred_tool_ids=[],
            disclosed_tool_ids=[],
            suppressed_tool_ids=[],
        )
        parts: List[str] = []
        completed = False
        async for event in gateway.stream(request):
            if event.event_type is GatewayEventType.OUTPUT_TEXT_DELTA:
                parts.append(event.delta or "")
            elif event.event_type is GatewayEventType.RESPONSE_COMPLETED:
                completed = True
            elif event.event_type in {
                GatewayEventType.TOOL_CALL_REQUESTED,
                GatewayEventType.RESPONSE_FAILED,
            }:
                logger.warning("[SessionService] Managed title request did not complete as text")
                return fallback
        if not completed:
            return fallback
        raw = "".join(parts).strip()
        title = _clean_title_candidate(
            re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        )
        logger.info(f"[SessionService] Title generation result: {_title_log_summary(title)}")
        if title and len(title) <= 50:
            return title
    except Exception as e:
        logger.warning(f"[SessionService] Managed title generation failed: {e}")
    return fallback


def _session_messages_for_title(store: Any, session_id: str) -> List[dict]:
    try:
        page = store.load_history_page(session_id, page=1, page_size=40)
    except Exception as exc:
        logger.warning(f"[SessionService] Failed loading session history for title: {exc}")
        return []
    messages = page.get("messages") if isinstance(page, dict) else []
    return messages if isinstance(messages, list) else []


def generate_session_title_for_store(
    store: Any,
    session_id: str,
    user_message: str = "",
    assistant_reply: str = "",
    *,
    session_summary: str = "",
) -> str:
    return generate_session_title(
        user_message,
        assistant_reply,
        conversation_messages=_session_messages_for_title(store, session_id),
        session_summary=session_summary,
    )


class SessionService:
    """
    High-level service for session lifecycle management.

    Usage:
        svc = SessionService()
        result = svc.dispatch("list", {"channel_type": "web", "page": 1})
    """

    def _get_store(self):
        from agent.memory import get_conversation_store
        return get_conversation_store()

    def _remove_agent(self, session_id: str):
        """Remove the in-memory Agent instance for a session if it exists."""
        try:
            from bridge.bridge import Bridge
            ab = Bridge().get_agent_bridge()
            if session_id in ab.agents:
                del ab.agents[session_id]
                logger.info(f"[SessionService] Removed agent instance: {session_id}")
        except Exception:
            pass

    def _clear_responses_state(self, session_id: str):
        try:
            from models.openai.responses_state_store import clear_responses_state_for_session

            removed = clear_responses_state_for_session(session_id)
            if removed:
                logger.info(f"[SessionService] Cleared Responses state: sid={session_id}, removed={removed}")
        except Exception as e:
            logger.warning(f"[SessionService] Failed to clear Responses state for {session_id}: {e}")

    @staticmethod
    def _normalize_sid(session_id: str) -> str:
        if session_id and not session_id.startswith("session_"):
            return f"session_{session_id}"
        return session_id

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def list_sessions(self, channel_type: Optional[str] = None,
                      page: int = 1, page_size: int = 50) -> dict:
        store = self._get_store()
        return store.list_sessions(
            channel_type=channel_type,
            page=page,
            page_size=page_size,
        )

    def delete_session(self, session_id: str) -> None:
        if not session_id:
            raise ValueError("session_id required")
        session_id = self._normalize_sid(session_id)

        store = self._get_store()
        store.clear_session(session_id)
        self._remove_agent(session_id)
        self._clear_responses_state(session_id)
        logger.info(f"[SessionService] Session deleted: {session_id}")

    def rename_session(self, session_id: str, title: str) -> None:
        if not session_id:
            raise ValueError("session_id required")
        if not title:
            raise ValueError("title required")
        session_id = self._normalize_sid(session_id)

        store = self._get_store()
        found = store.rename_session(session_id, title, lock_title=True)
        if not found:
            raise ValueError("session not found")

    def clear_context(self, session_id: str) -> int:
        """
        Set context boundary. Returns the new context_start_seq value.
        """
        if not session_id:
            raise ValueError("session_id required")
        session_id = self._normalize_sid(session_id)

        store = self._get_store()
        new_seq = store.clear_context(session_id)
        self._remove_agent(session_id)
        self._clear_responses_state(session_id)
        return new_seq

    def gen_title(self, session_id: str, user_message: str = "",
                  assistant_reply: str = "", session_summary: str = "") -> str:
        """
        Generate an AI title and persist it. Returns the generated title.
        """
        if not session_id:
            raise ValueError("session_id required")
        session_id = self._normalize_sid(session_id)

        store = self._get_store()
        title_state = store.get_session_title_state(session_id)
        if title_state and title_state.get("title_locked"):
            current_title = str(title_state.get("title") or "")
            logger.info(
                f"[SessionService] Title locked: sid={session_id}, "
                f"title_summary={_title_log_summary(current_title)}"
            )
            return current_title
        title = generate_session_title_for_store(
            store,
            session_id,
            user_message,
            assistant_reply,
            session_summary=session_summary,
        )
        updated = store.rename_session(session_id, title, respect_title_lock=True)
        logger.info(f"[SessionService] Title set: sid={session_id}, "
                     f"title_summary={_title_log_summary(title)}, db_updated={updated}")
        return title

    # ------------------------------------------------------------------
    # dispatch — single entry point for protocol messages
    # ------------------------------------------------------------------
    def dispatch(self, action: str, payload: Optional[dict] = None) -> dict:
        """
        Dispatch a session management action and return a protocol-compatible
        response dict.

        Action names use a ``*_session`` / session-prefixed convention so they
        can coexist with history actions (e.g. ``query``) on the same HISTORY
        message channel without ambiguity.

        Supported actions:
          - list_sessions: list sessions with pagination
          - delete_session: delete a session
          - rename_session: rename a session title
          - clear_context: set context boundary
          - generate_title: AI-generate a session title

        :param action: one of the above action names
        :param payload: action-specific payload
        :return: dict with action, code, message, payload
        """
        payload = payload or {}
        try:
            if action == "list_sessions":
                result = self.list_sessions(
                    channel_type=payload.get("channel_type"),
                    page=int(payload.get("page", 1)),
                    page_size=int(payload.get("page_size", 50)),
                )
                return {"action": action, "code": 200, "message": "success", "payload": result}

            elif action == "delete_session":
                self.delete_session(payload.get("session_id", ""))
                return {"action": action, "code": 200, "message": "success", "payload": None}

            elif action == "rename_session":
                self.rename_session(
                    payload.get("session_id", ""),
                    payload.get("title", "").strip(),
                )
                return {"action": action, "code": 200, "message": "success", "payload": None}

            elif action == "clear_context":
                new_seq = self.clear_context(payload.get("session_id", ""))
                return {"action": action, "code": 200, "message": "success",
                        "payload": {"context_start_seq": new_seq}}

            elif action == "generate_title":
                title = self.gen_title(
                    payload.get("session_id", ""),
                    payload.get("user_message", ""),
                    payload.get("assistant_reply", ""),
                    payload.get("session_summary", ""),
                )
                return {"action": action, "code": 200, "message": "success",
                        "payload": {"title": title}}

            else:
                return {"action": action, "code": 400,
                        "message": f"unknown action: {action}", "payload": None}

        except ValueError as e:
            return {"action": action, "code": 400, "message": str(e), "payload": None}
        except Exception as e:
            logger.error(f"[SessionService] dispatch error: action={action}, error={e}")
            return {"action": action, "code": 500, "message": str(e), "payload": None}
