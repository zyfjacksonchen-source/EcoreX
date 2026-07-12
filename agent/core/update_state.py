from __future__ import annotations

from typing import Any, Dict


class UpdateNoticeAuthority:
    """Stable update notice identity and visibility decisions."""

    @staticmethod
    def stable_notice_key(state: Dict[str, Any] | None) -> str:
        if not isinstance(state, dict) or not state.get("stateAvailable"):
            return ""
        fingerprint = (
            state.get("artifactFingerprint")
            or state.get("artifact_fingerprint")
            or state.get("noticeRevision")
            or state.get("notice_revision")
            or state.get("reason")
            or "state"
        )
        parts = [
            state.get("source") or "runtime-update-state",
            state.get("version") or "",
            state.get("status") or "unknown",
            fingerprint,
        ]
        return ":".join(str(part) for part in parts if str(part or "").strip())

    @staticmethod
    def suppress_stale_admin_notice(state: Dict[str, Any] | None, current_version: str) -> bool:
        if not isinstance(state, dict) or not state.get("stateAvailable"):
            return False
        if str(state.get("source") or "") != "admin-release-notice":
            return False
        version = str(state.get("version") or "")
        if not version or not current_version:
            return False
        return _compare_versions(version, current_version) <= 0


def _compare_versions(left: str, right: str) -> int:
    def parse(value: str):
        parts = []
        for part in str(value or "").replace("-", ".").split("."):
            digits = "".join(ch for ch in part if ch.isdigit())
            if digits:
                parts.append(int(digits))
        return parts or [0]

    a = parse(left)
    b = parse(right)
    for index in range(max(len(a), len(b))):
        diff = (a[index] if index < len(a) else 0) - (b[index] if index < len(b) else 0)
        if diff:
            return 1 if diff > 0 else -1
    return 0
