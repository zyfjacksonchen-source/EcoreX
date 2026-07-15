from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List


CONNECTOR_STATES = {
    "configured",
    "starting",
    "ready",
    "failed",
    "stale_config",
    "restarting",
    "disconnected",
}


@dataclass
class ConnectorRegistry:
    """Durable connector state projection for WebUI/runtime consumers."""

    workspace: Path
    file_name: str = "connector-registry.json"
    _cache: Dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return Path(self.workspace).expanduser() / "state" / self.file_name

    def load(self) -> Dict[str, Any]:
        if self._cache:
            return dict(self._cache)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self._cache = payload
                return dict(payload)
        except Exception:
            pass
        return {"schema": "ecorex.connector-registry.v1", "connectors": {}}

    def snapshot(self, configured: Iterable[Dict[str, Any]] | None = None) -> Dict[str, Any]:
        payload = self.load()
        connectors: Dict[str, Any] = dict(payload.get("connectors") or {})
        for item in configured or []:
            connector_id = str((item or {}).get("id") or "").strip()
            if not connector_id:
                continue
            existing = dict(connectors.get(connector_id) or {})
            state = str(existing.get("state") or "configured")
            if state not in CONNECTOR_STATES:
                state = "configured"
            connectors[connector_id] = {
                **existing,
                "id": connector_id,
                "state": state,
                "configured": True,
                "callable": bool(existing.get("callable")),
                "toolNames": sorted({str(name) for name in (existing.get("toolNames") or []) if str(name or "").strip()}),
                "lastCheckedAt": existing.get("lastCheckedAt") or "",
            }
        return {
            "schema": "ecorex.connector-registry.v1",
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "connectors": connectors,
        }

    @staticmethod
    def health_lists(snapshot: Dict[str, Any]) -> Dict[str, List[str]]:
        connectors = snapshot.get("connectors") if isinstance(snapshot, dict) else {}
        if not isinstance(connectors, dict):
            connectors = {}
        configured: List[str] = []
        connected: List[str] = []
        callable_ids: List[str] = []
        for connector_id, item in connectors.items():
            record = item if isinstance(item, dict) else {}
            cid = str(record.get("id") or connector_id or "").strip()
            if not cid:
                continue
            if record.get("configured"):
                configured.append(cid)
            if str(record.get("state") or "") in {"ready", "starting", "restarting"}:
                connected.append(cid)
            if record.get("callable"):
                callable_ids.append(cid)
        return {
            "configuredIds": sorted(set(configured)),
            "connectedIds": sorted(set(connected)),
            "callableIds": sorted(set(callable_ids)),
        }
