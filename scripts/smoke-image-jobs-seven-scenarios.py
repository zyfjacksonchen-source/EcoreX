#!/usr/bin/env python3
"""Seven-scenario smoke for v0.2.2 backend-led image jobs.

This smoke intentionally mixes one real external provider credential check with
local deterministic scenarios.  The external credential is read only from
environment variables and is never written to the JSON artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
FAKE_API_KEY = "sk-smoke-image-job-fallback"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    + (1).to_bytes(4, "big")
    + (1).to_bytes(4, "big")
    + b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00\xef\xbf\xa7\xdb"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _ensure_web_stub() -> None:
    if "web" in sys.modules:
        return
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


def _load_provider_fallback_module() -> Any:
    path = ROOT / "scripts" / "smoke-image-jobs-provider-fallback.py"
    spec = importlib.util.spec_from_file_location("image_jobs_provider_fallback_smoke", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load provider fallback smoke module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _conf_for(workspace: Path, timeout: int) -> dict[str, Any]:
    return {
        "agent_workspace": str(workspace),
        "image_request_timeout_seconds": timeout,
        "image_job_max_parallel": 4,
        "image_job_hard_max_parallel": 8,
    }


def _input_factory(params: types.SimpleNamespace | None):
    def _fake_input(**defaults: Any) -> types.SimpleNamespace:
        values = dict(defaults)
        if params:
            values.update(vars(params))
        return types.SimpleNamespace(**values)

    return _fake_input


@contextmanager
def _web_patches(web_channel: Any, workspace: Path, timeout: int, *, body: bytes = b"{}", params: types.SimpleNamespace | None = None):
    output_dir = workspace / "image-jobs"
    output_dir.mkdir(parents=True, exist_ok=True)
    with patch.object(web_channel, "_require_auth", return_value=None), \
        patch.object(web_channel, "conf", return_value=_conf_for(workspace, timeout)), \
        patch.object(web_channel.web, "header", return_value=None), \
        patch.object(web_channel.web, "data", return_value=body), \
        patch.object(web_channel.web, "input", side_effect=_input_factory(params)), \
        patch.object(web_channel, "_image_job_output_dir", return_value=str(output_dir)):
        yield


def _invoke_start(web_channel: Any, body: dict[str, Any], *, workspace: Path, timeout: int, runner_override: Callable | None = None) -> dict[str, Any]:
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    with _web_patches(web_channel, workspace, timeout, body=encoded):
        if runner_override is None:
            raw = web_channel.ImageJobsHandler().POST()
        else:
            with patch.object(web_channel, "_image_job_runner", return_value=runner_override):
                raw = web_channel.ImageJobsHandler().POST()
    return json.loads(raw or "{}")


def _invoke_get(web_channel: Any, *, workspace: Path, timeout: int, job_id: str, include_events: bool = True) -> dict[str, Any]:
    params = types.SimpleNamespace(
        job_id=job_id,
        request_id="",
        requestId="",
        wait="",
        timeout="",
        include_events="1" if include_events else "",
    )
    with _web_patches(web_channel, workspace, timeout, params=params):
        return json.loads(web_channel.ImageJobsHandler().GET() or "{}")


def _invoke_action(web_channel: Any, *, workspace: Path, timeout: int, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    with _web_patches(web_channel, workspace, timeout, body=encoded):
        return json.loads(web_channel.ImageJobActionHandler().POST(job_id) or "{}")


def _events_for(ledger: Any, request_id: str) -> list[dict[str, Any]]:
    return ledger.events_for_request(request_id, limit=0)


def _event_types(events: list[dict[str, Any]]) -> list[str]:
    return [str(event.get("event_type") or "") for event in events]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _assert_no_secret_leak(value: Any, secrets: list[str], *, label: str) -> None:
    serialized = _json_dumps(value)
    for secret in secrets:
        if secret and secret in serialized:
            raise AssertionError(f"{label} leaked a credential value")
    if "sk-smoke-image-job-fallback" in serialized:
        raise AssertionError(f"{label} leaked fake API key")


def _artifact_signature(path: str) -> dict[str, Any]:
    target = Path(path)
    _assert(target.exists(), f"artifact does not exist: {target}")
    data = target.read_bytes()
    kind = "unknown"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png"
    elif data.startswith(b"\xff\xd8"):
        kind = "jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        kind = "webp"
    _assert(kind != "unknown", f"artifact is not a supported image: {target}")
    return {
        "kind": kind,
        "size_bytes": len(data),
        "sha256_12": hashlib.sha256(data).hexdigest()[:12],
    }


def _public_artifacts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    artifacts = job.get("artifacts") if isinstance(job.get("artifacts"), list) else []
    if artifacts:
        return [item for item in artifacts if isinstance(item, dict)]
    projection = payload.get("projection") if isinstance(payload.get("projection"), dict) else {}
    projected_jobs = projection.get("image_jobs") if isinstance(projection.get("image_jobs"), list) else []
    if projected_jobs:
        projected_artifacts = projected_jobs[0].get("artifacts") if isinstance(projected_jobs[0], dict) else []
        return [item for item in projected_artifacts or [] if isinstance(item, dict)]
    return []


def _require_external_env(args: argparse.Namespace) -> tuple[str, str, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    api_base = os.environ.get("OPENAI_API_BASE", "")
    model = args.model or os.environ.get("SKILL_IMAGE_GENERATION_MODEL") or "gpt-image-2-pro"
    if not api_key or not api_base:
        raise RuntimeError("OPENAI_API_KEY and OPENAI_API_BASE are required for the external provider scenario")
    return api_key, api_base, model


def _host_hash(api_base: str) -> str:
    return hashlib.sha256(api_base.encode("utf-8", errors="replace")).hexdigest()[:12]


def scenario_external_generation(web_channel: Any, ledger: Any, workspace: Path, args: argparse.Namespace, secrets: list[str]) -> dict[str, Any]:
    _api_key, api_base, model = _require_external_env(args)
    request_id = "req-image-seven-external-generation"
    payload = _invoke_start(
        web_channel,
        {
            "action": "start",
            "synchronous": True,
            "include_events": True,
            "request_id": request_id,
            "session_id": "session-image-seven-scenarios",
            "job_id": "image-job-seven-external-generation",
            "prompt": "v0.2.2 external provider smoke: one simple blue square icon",
            "provider": "openai",
            "model": model,
            "output_format": "png",
            "size": "1024x1024",
        },
        workspace=workspace,
        timeout=args.timeout,
    )
    events = _events_for(ledger, request_id)
    _assert(payload.get("status") == "success", "external generation did not return success")
    _assert((payload.get("job") or {}).get("status") == "completed", "external generation job did not complete")
    artifacts = _public_artifacts(payload)
    _assert(artifacts, "external generation produced no artifact")
    signatures = [_artifact_signature(str(item.get("path") or "")) for item in artifacts]
    event_types = _event_types(events)
    for event_type in ("image_job.started", "image_job.progress", "image_job.artifact", "image_job.completed"):
        _assert(event_type in event_types, f"external generation missing {event_type}")
    projected_job = ((payload.get("projection") or {}).get("image_jobs") or [{}])[0]
    _assert(projected_job.get("last_provider") in {"OpenAI", "openai", ""}, "external provider projection has unexpected provider")
    _assert_no_secret_leak({"payload": payload, "events": events}, secrets, label="external_generation")
    return {
        "status": "pass",
        "external_base_hash": _host_hash(api_base),
        "model": projected_job.get("last_model") or model,
        "artifact_count": len(artifacts),
        "artifact_signatures": signatures,
        "event_types": sorted(set(event_types)),
    }


def scenario_fake_edit_fallback(web_channel: Any, ledger: Any, workspace: Path, args: argparse.Namespace, secrets: list[str]) -> dict[str, Any]:
    fallback_mod = _load_provider_fallback_module()
    edit_input = workspace / "edit-input.png"
    edit_input.write_bytes(PNG_BYTES)
    request_id = "req-image-seven-fake-edit-fallback"
    with fallback_mod.FakeImageApiServer() as api_base:
        with fallback_mod._provider_env(api_base):
            payload = _invoke_start(
                web_channel,
                {
                    "action": "start",
                    "synchronous": True,
                    "include_events": True,
                    "request_id": request_id,
                    "session_id": "session-image-seven-scenarios",
                    "job_id": "image-job-seven-fake-edit-fallback",
                    "prompt": "v0.2.2 fake provider edit fallback smoke",
                    "image_url": str(edit_input),
                    "provider": "openai",
                    "model": "gpt-image-2-pro",
                    "output_format": "png",
                },
                workspace=workspace,
                timeout=args.timeout,
            )
            calls = list(fallback_mod.FakeImageApiHandler.calls)
    events = _events_for(ledger, request_id)
    projected_job = ((payload.get("projection") or {}).get("image_jobs") or [{}])[0]
    edit_calls = [call for call in calls if call.get("route") == "edits"]
    _assert(payload.get("status") == "success", "fake edit fallback did not return success")
    _assert((payload.get("job") or {}).get("status") == "completed", "fake edit fallback job did not complete")
    _assert(projected_job.get("fallback_used") is True, "fake edit fallback did not project fallback_used")
    _assert(projected_job.get("fallback_from_model") == "gpt-image-2-pro", "fake edit fallback from_model mismatch")
    _assert(projected_job.get("fallback_to_model") == "gpt-image-2", "fake edit fallback to_model mismatch")
    _assert([call.get("model") for call in edit_calls] == ["gpt-image-2-pro", "gpt-image-2"], "fake edit fallback model order mismatch")
    _assert(all(call.get("has_image_file") for call in edit_calls), "fake edit fallback did not send image file")
    _assert_no_secret_leak({"payload": payload, "events": events}, secrets + [FAKE_API_KEY], label="fake_edit_fallback")
    return {
        "status": "pass",
        "route": "edits",
        "attempted_models": [call.get("model") for call in edit_calls],
        "fallback_used": projected_job.get("fallback_used"),
        "artifact_count": len(_public_artifacts(payload)),
        "event_types": sorted(set(_event_types(events))),
    }


def scenario_parallel_artifacts(web_channel: Any, ledger: Any, workspace: Path, args: argparse.Namespace, secrets: list[str]) -> dict[str, Any]:
    request_id = "req-image-seven-parallel-artifacts"
    payload = _invoke_start(
        web_channel,
        {
            "action": "start",
            "dry_run": True,
            "synchronous": True,
            "include_events": True,
            "request_id": request_id,
            "session_id": "session-image-seven-scenarios",
            "job_id": "image-job-seven-parallel-artifacts",
            "prompt": "v0.2.2 dry-run parallel artifact smoke",
            "count": 3,
            "max_parallel": 3,
        },
        workspace=workspace,
        timeout=args.timeout,
    )
    events = _events_for(ledger, request_id)
    projected_job = ((payload.get("projection") or {}).get("image_jobs") or [{}])[0]
    event_types = _event_types(events)
    _assert((payload.get("job") or {}).get("status") == "completed", "parallel dry-run job did not complete")
    _assert(len(_public_artifacts(payload)) == 3, "parallel dry-run did not emit three artifacts")
    _assert(event_types.count("image_job.artifact") == 3, "parallel dry-run durable artifact event count mismatch")
    _assert(projected_job.get("effective_max_parallel") == 3, "parallel dry-run effective_max_parallel mismatch")
    _assert_no_secret_leak({"payload": payload, "events": events}, secrets, label="parallel_artifacts")
    return {
        "status": "pass",
        "artifact_count": len(_public_artifacts(payload)),
        "effective_max_parallel": projected_job.get("effective_max_parallel"),
        "artifact_events": event_types.count("image_job.artifact"),
    }


def scenario_ocr_reuse(web_channel: Any, ledger: Any, workspace: Path, args: argparse.Namespace, secrets: list[str]) -> dict[str, Any]:
    image_ref = str(workspace / "ocr-input.png")
    Path(image_ref).write_bytes(PNG_BYTES)
    request_id = "req-image-seven-ocr-reuse"
    payload = _invoke_start(
        web_channel,
        {
            "action": "start",
            "dry_run": True,
            "synchronous": True,
            "include_events": True,
            "ocr_reuse": True,
            "request_id": request_id,
            "session_id": "session-image-seven-scenarios",
            "job_id": "image-job-seven-ocr-reuse",
            "max_parallel": 1,
            "tasks": [
                {"prompt": "ocr reuse task one", "image_url": image_ref},
                {"prompt": "ocr reuse task two", "image_url": image_ref},
            ],
        },
        workspace=workspace,
        timeout=args.timeout,
    )
    events = _events_for(ledger, request_id)
    projected_job = ((payload.get("projection") or {}).get("image_jobs") or [{}])[0]
    ocr_events = [
        event.get("payload") or {}
        for event in events
        if event.get("event_type") == "image_job.progress" and (event.get("payload") or {}).get("status") == "ocr"
    ]
    _assert((payload.get("job") or {}).get("status") == "completed", "ocr reuse job did not complete")
    _assert(len(ocr_events) == 2, "ocr reuse did not emit two OCR progress events")
    _assert(any(item.get("ocr_cache_hit") is False for item in ocr_events), "ocr reuse did not record cache miss")
    _assert(any(item.get("ocr_cache_hit") is True for item in ocr_events), "ocr reuse did not record cache hit")
    _assert(projected_job.get("ocr_cache_hit_count") == 1, "ocr reuse projection cache hit count mismatch")
    _assert(projected_job.get("ocr_cache_miss_count") == 1, "ocr reuse projection cache miss count mismatch")
    serialized = _json_dumps({"payload": payload, "events": events})
    _assert("dry-run-image-brief" not in serialized, "raw OCR brief leaked into public payload")
    _assert_no_secret_leak({"payload": payload, "events": events}, secrets, label="ocr_reuse")
    return {
        "status": "pass",
        "ocr_events": len(ocr_events),
        "ocr_cache_hit_count": projected_job.get("ocr_cache_hit_count"),
        "ocr_cache_miss_count": projected_job.get("ocr_cache_miss_count"),
        "ocr_total_ms": projected_job.get("ocr_total_ms"),
    }


def scenario_projection_recovery(web_channel: Any, ledger: Any, workspace: Path, args: argparse.Namespace, secrets: list[str], job_id: str) -> dict[str, Any]:
    from agent.protocol import reset_image_job_service_for_tests

    reset_image_job_service_for_tests(ledger)
    payload = _invoke_get(web_channel, workspace=workspace, timeout=args.timeout, job_id=job_id, include_events=True)
    _assert(payload.get("status") == "success", "projection recovery GET did not return success")
    _assert((payload.get("job") or {}).get("status") == "completed", "projection recovery did not restore completed status")
    _assert((payload.get("job") or {}).get("recovered_from_projection") is True, "projection recovery did not mark recovered_from_projection")
    _assert(((payload.get("projection") or {}).get("image_jobs") or [{}])[0].get("status") == "completed", "projection recovery missing projected completed job")
    _assert_no_secret_leak(payload, secrets, label="projection_recovery")
    return {
        "status": "pass",
        "job_id": (payload.get("job") or {}).get("job_id"),
        "recovered_from_projection": (payload.get("job") or {}).get("recovered_from_projection"),
        "projected_jobs": len((payload.get("projection") or {}).get("image_jobs") or []),
    }


def scenario_cancel_running(web_channel: Any, ledger: Any, workspace: Path, args: argparse.Namespace, secrets: list[str]) -> dict[str, Any]:
    from agent.protocol import ImageJobCancelled

    request_id = "req-image-seven-cancel-running"
    job_id = "image-job-seven-cancel-running"

    def slow_runner(task: dict[str, Any], emit_progress: Callable, cancel_event: Any) -> dict[str, Any]:
        emit_progress("provider_request", progress=0.2, detail={"source": "seven_scenario", "provider": "test"})
        deadline = time.time() + 10
        while time.time() < deadline:
            if cancel_event.is_set():
                raise ImageJobCancelled("cancel_requested")
            time.sleep(0.05)
        return {"kind": "image", "title": "slow.png", "path": str(workspace / "slow.png"), "fileType": "image"}

    start = _invoke_start(
        web_channel,
        {
            "action": "start",
            "synchronous": False,
            "include_events": True,
            "request_id": request_id,
            "session_id": "session-image-seven-scenarios",
            "job_id": job_id,
            "prompt": "v0.2.2 cancel running image job smoke",
        },
        workspace=workspace,
        timeout=args.timeout,
        runner_override=slow_runner,
    )
    _assert(start.get("status") == "success", "cancel scenario start did not return success")
    time.sleep(0.15)
    cancel = _invoke_action(
        web_channel,
        workspace=workspace,
        timeout=args.timeout,
        job_id=job_id,
        body={"action": "cancel", "include_events": True, "reason": "seven_scenario_cancel"},
    )
    collect = _invoke_action(
        web_channel,
        workspace=workspace,
        timeout=args.timeout,
        job_id=job_id,
        body={"action": "collect", "wait": True, "timeout": 2, "include_events": True},
    )
    events = _events_for(ledger, request_id)
    event_types = _event_types(events)
    _assert((cancel.get("job") or {}).get("status") == "cancelled", "cancel action did not mark job cancelled")
    _assert((collect.get("job") or {}).get("status") == "cancelled", "collect after cancel did not preserve cancelled")
    _assert("image_job.cancelled" in event_types, "cancel scenario missing durable cancelled event")
    _assert("image_job.completed" not in event_types, "cancel scenario emitted completed event")
    _assert_no_secret_leak({"start": start, "cancel": cancel, "collect": collect, "events": events}, secrets, label="cancel_running")
    return {
        "status": "pass",
        "cancel_status": (cancel.get("job") or {}).get("status"),
        "collect_status": (collect.get("job") or {}).get("status"),
        "event_types": sorted(set(event_types)),
    }


def scenario_validation_no_events(web_channel: Any, ledger: Any, workspace: Path, args: argparse.Namespace, secrets: list[str]) -> dict[str, Any]:
    before = len(ledger.list_events(limit=10000))
    missing_prompt = _invoke_start(
        web_channel,
        {"action": "start", "dry_run": True},
        workspace=workspace,
        timeout=args.timeout,
    )
    invalid_task = _invoke_start(
        web_channel,
        {"action": "start", "dry_run": True, "tasks": [{"provider": "privateprompt"}]},
        workspace=workspace,
        timeout=args.timeout,
    )
    after = len(ledger.list_events(limit=10000))
    _assert(missing_prompt.get("status") == "error", "missing prompt validation did not fail")
    _assert(invalid_task.get("status") == "error", "invalid task validation did not fail")
    _assert(after == before, "validation failures wrote runtime events")
    _assert_no_secret_leak({"missing_prompt": missing_prompt, "invalid_task": invalid_task}, secrets, label="validation_no_events")
    return {
        "status": "pass",
        "missing_prompt_message": missing_prompt.get("message"),
        "invalid_task_message": invalid_task.get("message"),
        "events_written": after - before,
    }


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    _ensure_web_stub()
    from agent.protocol import (
        get_run_event_ledger,
        reset_image_job_service_for_tests,
        reset_run_event_ledger_for_tests,
        reset_run_ledger_for_tests,
    )
    from channel.web import web_channel

    api_key, api_base, model = _require_external_env(args)
    secrets = [api_key, FAKE_API_KEY]
    started = time.time()
    with tempfile.TemporaryDirectory() as workspace_raw:
        workspace = Path(workspace_raw)
        db_path = workspace / "run-ledger.db"
        reset_run_ledger_for_tests(db_path)
        reset_run_event_ledger_for_tests(db_path)
        ledger = get_run_event_ledger()
        reset_image_job_service_for_tests(ledger)
        scenarios: dict[str, Any] = {}
        try:
            scenarios["external_generation"] = scenario_external_generation(web_channel, ledger, workspace, args, secrets)
            scenarios["fake_edit_fallback"] = scenario_fake_edit_fallback(web_channel, ledger, workspace, args, secrets)
            scenarios["parallel_artifacts"] = scenario_parallel_artifacts(web_channel, ledger, workspace, args, secrets)
            parallel_job_id = "image-job-seven-parallel-artifacts"
            scenarios["ocr_reuse"] = scenario_ocr_reuse(web_channel, ledger, workspace, args, secrets)
            scenarios["projection_recovery"] = scenario_projection_recovery(web_channel, ledger, workspace, args, secrets, parallel_job_id)
            scenarios["cancel_running"] = scenario_cancel_running(web_channel, ledger, workspace, args, secrets)
            scenarios["validation_no_events"] = scenario_validation_no_events(web_channel, ledger, workspace, args, secrets)
            total_events = len(ledger.list_events(limit=10000))
        finally:
            reset_run_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-run-ledger-test-reset.db")
            reset_run_event_ledger_for_tests(Path(tempfile.gettempdir()) / "ecorex-run-event-ledger-test-reset.db")
    result = {
        "status": "PASS",
        "version": "v0.2.2",
        "model": model,
        "external_provider": {
            "provider": "OpenAI-compatible",
            "base_url_hash": _host_hash(api_base),
            "credential_source": "env:OPENAI_API_KEY",
        },
        "duration_ms": round((time.time() - started) * 1000),
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
        "total_runtime_events": total_events,
    }
    _assert_no_secret_leak(result, secrets, label="final_artifact")
    return result


def _write_artifact(path: str, payload: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    if not target.is_absolute():
        target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", default="", help="Optional redacted JSON artifact path.")
    parser.add_argument("--model", default=os.environ.get("SKILL_IMAGE_GENERATION_MODEL") or "gpt-image-2-pro")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    try:
        result = run_smoke(args)
    except Exception as exc:  # pragma: no cover - script-level failure report
        result = {
            "status": "FAIL",
            "error": str(exc),
            "credential_source": "env:OPENAI_API_KEY",
            "external_base_hash": _host_hash(os.environ.get("OPENAI_API_BASE", "")),
        }
        _write_artifact(args.artifact, result)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 1
    _write_artifact(args.artifact, result)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
