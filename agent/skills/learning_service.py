"""EcoreX-native skill learning draft service.

This module intentionally keeps self-learning skills inside the existing
EcoreX runtime boundaries: learning creates ledger-backed drafts first, and
formal registration still goes through ``SkillService``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent.protocol.run_event_ledger import RunEventLedger, get_run_event_ledger
from agent.skills.service import SkillService


_SAFE_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
_ALLOWED_TOP_LEVEL_FILES = {"SKILL.md"}
_ALLOWED_PREFIXES = ("scripts/", "references/", "assets/", "agents/")
_MAX_FILE_BYTES = 128 * 1024
_MAX_TOTAL_BYTES = 512 * 1024
_SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password|authorization)\s*[:=]"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}\b"),
)
_HIGH_RISK_PATTERNS = (
    re.compile(r"(?i)\brm\s+-rf\b"),
    re.compile(r"(?i)\b(?:curl|wget)\b.*\|\s*(?:sh|bash|powershell|pwsh)\b"),
    re.compile(r"(?i)\b(?:reverse shell|nc\s+-e|Invoke-Expression|iex)\b"),
)


def _normalize_skill_name(value: Any) -> str:
    name = str(value or "").strip().lower().replace("_", "-")
    name = re.sub(r"[^a-z0-9-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name[:64].strip("-")


def _canonical_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    parts = [part for part in path.split("/") if part and part not in {"."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("invalid skill file path")
    return "/".join(parts)


def _file_content(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _content_hash(files: Iterable[Dict[str, str]]) -> str:
    h = hashlib.sha256()
    for item in sorted(files, key=lambda row: row["path"]):
        h.update(item["path"].encode("utf-8", errors="replace"))
        h.update(b"\0")
        h.update(item["content"].encode("utf-8", errors="replace"))
        h.update(b"\0")
    return h.hexdigest()


def _redacted_source_summary(sources: Any) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    if not isinstance(sources, list):
        return result
    for item in sources[:20]:
        if isinstance(item, dict):
            result.append({
                "type": str(item.get("type") or "source")[:48],
                "title": str(item.get("title") or item.get("name") or "")[:160],
                "url": str(item.get("url") or "")[:300],
                "hash": str(item.get("hash") or "")[:64],
            })
        else:
            result.append({"type": "text", "title": str(item)[:160]})
    return result


class SkillLearningService:
    """Create, validate, review, and register learned skill drafts."""

    def __init__(self, *, ledger: Optional[RunEventLedger] = None, skill_service: Optional[SkillService] = None):
        self.ledger = ledger or get_run_event_ledger()
        self.skill_service = skill_service

    def learning_prompt(self, *, goal: str, request_id: str = "", session_id: str = "") -> Dict[str, Any]:
        goal_text = str(goal or "").strip()
        if not goal_text:
            raise ValueError("goal is required")
        request_id = request_id or self._request_id_for("skill-learning", goal_text)
        payload = {
            "goal": goal_text[:1000],
            "mode": "draft-only",
            "rules": [
                "Collect only the workflow facts proven in this run.",
                "Create a draft first; do not write directly to skills/.",
                "Declare required tools, permissions, and external connections.",
                "Do not include raw secrets, cookies, tokens, or personal credentials.",
            ],
            "nextAction": {
                "tool": "agent_capability",
                "action": "create_skill_draft",
            },
        }
        self._append(
            request_id=request_id,
            session_id=session_id,
            event_type="skill_learning.requested",
            payload=payload,
            key=f"skill-learning:requested:{hashlib.sha256(goal_text.encode('utf-8')).hexdigest()[:16]}",
        )
        return {
            "status": "success",
            "requestId": request_id,
            "prompt": self._authoring_prompt(goal_text),
            **payload,
        }

    def create_draft(
        self,
        *,
        name: str,
        description: str,
        files: List[Dict[str, Any]],
        goal: str = "",
        sources: Any = None,
        request_id: str = "",
        session_id: str = "",
        reviews: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        normalized_name = _normalize_skill_name(name)
        if not normalized_name or not _SAFE_SKILL_NAME.match(normalized_name):
            raise ValueError("invalid learned skill name")
        if not str(description or "").strip():
            raise ValueError("description is required")

        normalized_files = self._normalize_files(normalized_name, description, files)
        content_hash = _content_hash(normalized_files)
        draft_id = f"draft-{content_hash[:16]}"
        request_id = request_id or self._request_id_for("skill-draft", draft_id)
        validation = self._validate_files(normalized_files)
        security = self._scan_files(normalized_files)
        review_state = self._review_state(reviews or [])

        draft = {
            "draftId": draft_id,
            "name": normalized_name,
            "goal": str(goal or "")[:1000],
            "description": str(description or "").strip(),
            "sources": _redacted_source_summary(sources),
            "files": normalized_files,
            "manifest": {
                "contentHash": content_hash,
                "fileCount": len(normalized_files),
                "totalBytes": sum(len(item["content"].encode("utf-8", errors="replace")) for item in normalized_files),
            },
            "validation": validation,
            "security": security,
            "reviewState": review_state,
            "status": "blocked" if validation["status"] != "pass" or security["status"] != "pass" else "draft",
        }

        event_base = f"skill-draft:{draft_id}"
        self._append(
            request_id=request_id,
            session_id=session_id,
            event_type="skill_draft.created",
            payload=self._draft_event_payload(draft),
            key=f"{event_base}:created",
        )
        self._append(
            request_id=request_id,
            session_id=session_id,
            event_type="skill_draft.validation_completed",
            payload={"draftId": draft_id, "name": normalized_name, **validation},
            key=f"{event_base}:validation:{validation['status']}",
        )
        self._append(
            request_id=request_id,
            session_id=session_id,
            event_type="skill_draft.security_reviewed",
            payload={"draftId": draft_id, "name": normalized_name, **security},
            key=f"{event_base}:security:{security['status']}",
        )
        if review_state.get("reviews"):
            self._append(
                request_id=request_id,
                session_id=session_id,
                event_type="skill_draft.role_reviewed",
                payload={"draftId": draft_id, "name": normalized_name, **review_state},
                key=f"{event_base}:reviews:{review_state['status']}",
            )
        return {"status": "success", "requestId": request_id, "draft": draft}

    def approve_and_register(
        self,
        *,
        draft: Dict[str, Any],
        request_id: str = "",
        session_id: str = "",
    ) -> Dict[str, Any]:
        if not isinstance(draft, dict):
            raise ValueError("draft is required")
        name = _normalize_skill_name(draft.get("name"))
        files = draft.get("files") if isinstance(draft.get("files"), list) else []
        description = str(draft.get("description") or "").strip()
        created = self.create_draft(
            name=name,
            description=description,
            files=files,
            goal=str(draft.get("goal") or ""),
            sources=draft.get("sources") or [],
            request_id=request_id,
            session_id=session_id,
            reviews=draft.get("reviews") if isinstance(draft.get("reviews"), list) else [],
        )
        checked_draft = created["draft"]
        if checked_draft["validation"]["status"] != "pass":
            raise ValueError("draft validation did not pass")
        if checked_draft["security"]["status"] != "pass":
            raise ValueError("draft security review did not pass")
        if self.skill_service is None:
            raise ValueError("skill service is required for registration")

        request_id = created.get("requestId") or request_id or self._request_id_for("skill-register", name)
        add_payload = {
            "name": name,
            "type": "url",
            "enabled": True,
            "category": "learned",
            "_permission_checked": True,
            "files": [
                {"path": item["path"], "content": item["content"]}
                for item in checked_draft["files"]
            ],
        }
        self.skill_service.add(add_payload)
        self._append(
            request_id=request_id,
            session_id=session_id,
            event_type="skill_draft.approved",
            payload={"draftId": checked_draft["draftId"], "name": name, "contentHash": checked_draft["manifest"]["contentHash"]},
            key=f"skill-draft:{checked_draft['draftId']}:approved",
        )
        self._append(
            request_id=request_id,
            session_id=session_id,
            event_type="skill.registered",
            payload={"draftId": checked_draft["draftId"], "name": name, "contentHash": checked_draft["manifest"]["contentHash"], "source": "learned"},
            key=f"skill-draft:{checked_draft['draftId']}:registered",
        )
        self._append(
            request_id=request_id,
            session_id=session_id,
            event_type="skill.materialized",
            payload={"draftId": checked_draft["draftId"], "name": name, "contentHash": checked_draft["manifest"]["contentHash"], "path": f"skills/{name}"},
            key=f"skill-draft:{checked_draft['draftId']}:materialized",
        )
        return {"status": "success", "requestId": request_id, "skill": name, "draftId": checked_draft["draftId"]}

    @staticmethod
    def _authoring_prompt(goal: str) -> str:
        return (
            "Learn this EcoreX workflow as a draft skill. Gather the concrete steps, "
            "required tools, external connections, permissions, failure modes, and output contract. "
            "Then call agent_capability with action=create_skill_draft. Do not write files directly. "
            f"Goal: {goal}"
        )

    def _normalize_files(self, name: str, description: str, files: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []
        seen = set()
        for item in files or []:
            path = _canonical_path(item.get("path") if isinstance(item, dict) else "")
            content = _file_content(item.get("content") if isinstance(item, dict) else "")
            if path in seen:
                raise ValueError(f"duplicate skill file path: {path}")
            seen.add(path)
            normalized.append({"path": path, "content": content})
        if "SKILL.md" not in seen:
            skill_md = (
                "---\n"
                f"name: {name}\n"
                f"description: {description.strip()}\n"
                "category: learned\n"
                "---\n\n"
                f"# {name}\n\n"
                "Use this learned skill only for workflows matching the original reviewed goal.\n"
            )
            normalized.insert(0, {"path": "SKILL.md", "content": skill_md})
        return normalized

    @staticmethod
    def _validate_files(files: List[Dict[str, str]]) -> Dict[str, Any]:
        findings: List[Dict[str, Any]] = []
        total = 0
        has_skill_md = False
        for item in files:
            path = item["path"]
            content_bytes = item["content"].encode("utf-8", errors="replace")
            total += len(content_bytes)
            if path == "SKILL.md":
                has_skill_md = True
            if path not in _ALLOWED_TOP_LEVEL_FILES and not path.startswith(_ALLOWED_PREFIXES):
                findings.append({"severity": "P0", "code": "path_not_allowed", "path": path})
            if len(content_bytes) > _MAX_FILE_BYTES:
                findings.append({"severity": "P1", "code": "file_too_large", "path": path})
        if not has_skill_md:
            findings.append({"severity": "P0", "code": "missing_skill_md"})
        if total > _MAX_TOTAL_BYTES:
            findings.append({"severity": "P1", "code": "draft_too_large"})
        status = "pass" if not findings else "fail"
        return {"status": status, "findings": findings, "totalBytes": total}

    @staticmethod
    def _scan_files(files: List[Dict[str, str]]) -> Dict[str, Any]:
        findings: List[Dict[str, Any]] = []
        for item in files:
            path = item["path"]
            text = item["content"]
            for pattern in _SENSITIVE_TEXT_PATTERNS:
                if pattern.search(text):
                    findings.append({"severity": "P0", "code": "secret_like_content", "path": path})
                    break
            for pattern in _HIGH_RISK_PATTERNS:
                if pattern.search(text):
                    findings.append({"severity": "P0", "code": "high_risk_command", "path": path})
                    break
        return {"status": "pass" if not findings else "fail", "findings": findings}

    @staticmethod
    def _review_state(reviews: List[Dict[str, Any]]) -> Dict[str, Any]:
        compact = []
        for item in reviews[:10]:
            if not isinstance(item, dict):
                continue
            compact.append({
                "role": str(item.get("role") or "reviewer")[:80],
                "status": str(item.get("status") or "pending").lower()[:24],
                "summary": str(item.get("summary") or "")[:500],
            })
        statuses = {item["status"] for item in compact}
        return {
            "status": "pass" if compact and statuses <= {"pass", "passed"} else "pending" if not statuses else "reviewing",
            "reviews": compact,
        }

    @staticmethod
    def _draft_event_payload(draft: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "draftId": draft["draftId"],
            "name": draft["name"],
            "goal": draft.get("goal") or "",
            "description": draft.get("description") or "",
            "sources": draft.get("sources") or [],
            "manifest": draft.get("manifest") or {},
            "status": draft.get("status") or "draft",
        }

    def _append(self, *, request_id: str, session_id: str, event_type: str, payload: Dict[str, Any], key: str) -> None:
        self.ledger.append_event(
            request_id=request_id,
            session_id=session_id or "",
            event_type=event_type,
            payload=payload,
            idempotency_key=key,
            source="skill_learning",
        )

    @staticmethod
    def _request_id_for(prefix: str, value: str) -> str:
        h = hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:16]
        return f"{prefix}-{h}"
