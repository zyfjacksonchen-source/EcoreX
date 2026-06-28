#!/usr/bin/env python3
"""Smoke-test Office/PDF quality evidence projection through the Web API surface."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_web_stub_if_needed() -> None:
    try:
        __import__("web")
        return
    except Exception:
        pass
    web_stub = types.ModuleType("web")
    web_stub.HTTPError = type("HTTPError", (Exception,), {})
    web_stub.cookies = lambda: {}
    web_stub.header = lambda *args, **kwargs: None
    web_stub.data = lambda: b"{}"
    web_stub.input = lambda **kwargs: types.SimpleNamespace(**kwargs)
    web_stub.setcookie = lambda *args, **kwargs: None
    web_stub.seeother = lambda *args, **kwargs: Exception("seeother")
    web_stub.notfound = lambda *args, **kwargs: Exception("notfound")
    web_stub.badrequest = lambda *args, **kwargs: Exception("badrequest")
    web_stub.application = lambda *args, **kwargs: types.SimpleNamespace(wsgifunc=lambda: None)
    web_stub.httpserver = types.SimpleNamespace(
        LogMiddleware=type("LogMiddleware", (), {"log": lambda *args, **kwargs: None}),
        StaticMiddleware=lambda app: app,
        WSGIServer=lambda *args, **kwargs: types.SimpleNamespace(serve_forever=lambda: None),
    )
    sys.modules["web"] = web_stub


def _sample_quality_evidence() -> dict:
    return {
        "schemaVersion": "v0.2.4",
        "kind": "pdf",
        "sourceRef": "hmac:source-ref",
        "qualityGates": ["text-orientation", "page-render", "layout-inspection", "visual-diff"],
        "checks": [
            {"id": "text-orientation", "status": "pass", "detail": "rotated=0"},
            {"id": "page-render", "status": "fail", "detail": r"rendered=0 C:\Users\private\secret.pdf token=abc"},
        ],
        "missingQualityGates": ["layout-inspection"],
        "status": "fail",
        "renderedArtifacts": [
            {
                "page": 1,
                "artifactRef": "hmac:render-ref",
                "extension": ".png",
                "width": 1200,
                "height": 900,
                "renderProof": "hmac:private-proof",
                "path": r"C:\Users\private\render.png",
            }
        ],
        "pdfAnalysis": {
            "summary": {
                "pageCount": 1,
                "totalExtractedTextChars": 120,
                "blankPageRiskCount": 0,
                "rawText": "private prompt",
            },
            "pageEvidence": [
                {
                    "page": 1,
                    "textLengthBucket": "100",
                    "renderProof": "hmac:private-proof",
                    "rawText": "private prompt",
                }
            ],
        },
        "debug": "private prompt sk-private-1234567890",
        "redacted": True,
    }


def run_smoke() -> dict:
    _install_web_stub_if_needed()
    from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests
    from channel.web.web_channel import RuntimeProjectionHandler

    with tempfile.TemporaryDirectory() as workspace:
        ledger = reset_run_event_ledger_for_tests(Path(workspace) / "runtime-events.db")
        request_id = "req-v024-office-pdf-web-evidence"
        session_id = "session-v024-office-pdf-web-evidence"
        evidence = _sample_quality_evidence()
        ledger.append_event(
            request_id=request_id,
            session_id=session_id,
            event_type="tool.completed",
            payload={
                "tool_call_id": "tool-office-pdf",
                "tool": "office-pdf",
                "status": "done",
                "result": {"qualityEvidence": evidence, "content": "private prompt body"},
            },
            idempotency_key=f"{request_id}:tool",
        )
        ledger.append_event(
            request_id=request_id,
            session_id=session_id,
            event_type="artifact.created",
            payload={
                "artifact": {
                    "title": "report.pdf",
                    "kind": "file",
                    "path": "outputs/report.pdf",
                    "quality_evidence": evidence,
                }
            },
            idempotency_key=f"{request_id}:artifact",
        )
        service_projection = RuntimeProjectionService(ledger).request_projection(request_id)
        with patch("channel.web.web_channel.web.input", return_value=types.SimpleNamespace(
            request_id=request_id,
            session_id="",
            after_event_id="0",
            limit="1000",
            include_events="1",
        )):
            api_payload = json.loads(RuntimeProjectionHandler().GET())

    assistant = next(message for message in service_projection["messages"] if message["role"] == "assistant")
    tool_evidence = assistant["tool_calls"][0].get("qualityEvidence") or {}
    artifact_evidence = assistant["artifacts"][0].get("qualityEvidence") or {}
    serialized = json.dumps({"service": service_projection, "api": api_payload}, ensure_ascii=False)
    forbidden = [
        "renderProof",
        "private prompt",
        "sk-private",
        "secret.pdf",
        "C:\\Users",
        "rawText",
    ]
    leaks = [item for item in forbidden if item in serialized]
    checks = {
        "serviceToolEvidence": tool_evidence.get("status") == "fail" and tool_evidence.get("kind") == "pdf",
        "serviceArtifactEvidence": artifact_evidence.get("status") == "fail" and artifact_evidence.get("kind") == "pdf",
        "apiProjectionEvidence": "qualityEvidence" in serialized,
        "renderProofOmitted": "renderProof" not in serialized,
        "privacyLeaksAbsent": not leaks,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "toolStatus": tool_evidence.get("status"),
        "artifactStatus": artifact_evidence.get("status"),
        "apiEventCount": len(api_payload.get("projection", {}).get("events") or []),
        "leaks": leaks,
        "redacted": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    payload = run_smoke()
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
