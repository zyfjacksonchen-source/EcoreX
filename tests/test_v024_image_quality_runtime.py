import importlib.util
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.image_quality_runtime import (
    analyze_image_quality,
    attach_image_finalization_evidence,
    build_image_finalization_decision,
    build_image_quality_evidence,
    compare_image_reference_quality,
)


pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402


def _write_gradient(path: Path) -> None:
    image = Image.new("RGB", (96, 96))
    draw = ImageDraw.Draw(image)
    for x in range(96):
        color = (40 + x, 120, 210 - x)
        draw.line([(x, 0), (x, 95)], fill=color)
    image.save(path)


def _check_status(evidence: dict, check_id: str) -> str:
    for check in evidence.get("checks") or []:
        if check.get("id") == check_id:
            return str(check.get("status") or "")
    raise AssertionError(f"missing check {check_id}")


def test_image_quality_evidence_passes_clean_generated_image_without_leaking_path():
    with tempfile.TemporaryDirectory() as workspace:
        path = Path(workspace) / "clean.png"
        _write_gradient(path)

        evidence = build_image_quality_evidence(path)
        serialized = json.dumps(evidence, ensure_ascii=False)

    assert evidence["kind"] == "image"
    assert evidence["status"] == "pass"
    assert _check_status(evidence, "decode-valid") == "pass"
    assert _check_status(evidence, "non-blank") == "pass"
    assert str(evidence["sourceRef"]).startswith("hmac:")
    assert str(path) not in serialized
    assert "clean.png" not in serialized


def test_image_quality_evidence_flags_blank_corrupt_seam_and_overlay_risks():
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        blank = root / "blank.png"
        corrupt = root / "corrupt.png"
        seam = root / "seam.png"
        overlay = root / "overlay.png"

        Image.new("RGB", (80, 80), "white").save(blank)
        corrupt.write_bytes(b"\x89PNG\r\n\x1a\nbroken")

        seam_image = Image.new("RGB", (96, 96), "black")
        seam_draw = ImageDraw.Draw(seam_image)
        seam_draw.rectangle([48, 0, 95, 95], fill="white")
        seam_image.save(seam)

        overlay_image = Image.new("RGBA", (96, 96), (40, 90, 160, 255))
        overlay_draw = ImageDraw.Draw(overlay_image)
        overlay_draw.rectangle([20, 20, 76, 76], fill=(255, 255, 255, 80))
        overlay_image.save(overlay)

        blank_evidence = build_image_quality_evidence(blank)
        corrupt_evidence = build_image_quality_evidence(corrupt)
        seam_evidence = build_image_quality_evidence(seam)
        overlay_evidence = build_image_quality_evidence(overlay)
        overlay_analysis = analyze_image_quality(overlay)

    assert _check_status(blank_evidence, "non-blank") == "fail"
    assert _check_status(corrupt_evidence, "decode-valid") == "fail"
    assert _check_status(seam_evidence, "seam-check") == "fail"
    assert _check_status(overlay_evidence, "overlay-ghosting-check") == "warn"
    assert overlay_analysis["summary"]["overlayGhostingRisk"] is True


def test_image_quality_evidence_flags_vision_text_watermark_and_anomaly_risks():
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        dense_text = root / "dense-text.png"
        watermark = root / "watermark.png"
        noise = root / "noise.png"

        text_marker = "WMWM 1234 ABCD ####"
        text_image = Image.new("RGB", (240, 160), "white")
        text_draw = ImageDraw.Draw(text_image)
        for y in range(8, 145, 14):
            text_draw.text((8, y), text_marker, fill="black")
        text_image.save(dense_text)

        watermark_image = Image.new("RGBA", (160, 120), (80, 150, 210, 255))
        watermark_draw = ImageDraw.Draw(watermark_image)
        for y in range(0, 120, 18):
            watermark_draw.text((8, y), "MARK MARK", fill=(255, 255, 255, 80))
        watermark_image.save(watermark)

        noise_image = Image.new("RGB", (96, 96), "white")
        noise_pixels = noise_image.load()
        for y in range(96):
            for x in range(96):
                noise_pixels[x, y] = (0, 0, 0) if (x * y + x + y) % 3 == 0 else (255, 255, 255)
        noise_image.save(noise)

        text_evidence = build_image_quality_evidence(dense_text)
        watermark_evidence = build_image_quality_evidence(watermark)
        noise_evidence = build_image_quality_evidence(noise)
        serialized = json.dumps(
            {"text": text_evidence, "watermark": watermark_evidence, "noise": noise_evidence},
            ensure_ascii=False,
        )

    assert _check_status(text_evidence, "text-glyph-check") == "warn"
    assert _check_status(text_evidence, "anomaly-check") == "pass"
    assert _check_status(watermark_evidence, "watermark-check") == "warn"
    assert _check_status(noise_evidence, "anomaly-check") == "warn"
    assert "text-glyph-check" in text_evidence["qualityGates"]
    assert "watermark-check" in watermark_evidence["qualityGates"]
    assert "anomaly-check" in noise_evidence["qualityGates"]
    assert text_marker not in serialized
    assert "MARK MARK" not in serialized


def test_image_quality_evidence_compares_reference_images_without_leaking_sources():
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        reference = root / "reference.png"
        candidate = root / "candidate.png"
        similar = root / "similar.png"
        different = root / "different.png"
        _write_gradient(reference)
        _write_gradient(candidate)
        _write_gradient(similar)
        with Image.open(similar) as image:
            similar_image = image.convert("RGB")
        similar_draw = ImageDraw.Draw(similar_image)
        similar_draw.rectangle([10, 10, 24, 24], fill=(58, 124, 194))
        similar_image.save(similar)

        different_image = Image.new("RGB", (96, 96), (20, 170, 70))
        different_draw = ImageDraw.Draw(different_image)
        for x in range(0, 96, 8):
            different_draw.line([(x, 0), (95 - x, 95)], fill=(230, 20, 120), width=3)
        for y in range(0, 96, 12):
            different_draw.rectangle([0, y, 95, min(95, y + 4)], fill=(20, 30, 220))
        different_image.save(different)

        match_evidence = build_image_quality_evidence(candidate, reference_images=[reference])
        similar_evidence = build_image_quality_evidence(similar, reference_images=[reference])
        mismatch_evidence = build_image_quality_evidence(different, reference_images=[reference])
        remote_evidence = build_image_quality_evidence(
            candidate,
            reference_images=["https://example.invalid/private/ref.png?token=secret"],
        )
        data_evidence = build_image_quality_evidence(
            candidate,
            reference_images=["data:image/png;base64,PRIVATEINLINEPAYLOAD"],
        )
        no_reference_evidence = build_image_quality_evidence(candidate)
        comparison = compare_image_reference_quality(candidate, [reference])
        similar_is_distinct = reference.read_bytes() != similar.read_bytes()
        serialized = json.dumps(
            {
                "match": match_evidence,
                "similar": similar_evidence,
                "mismatch": mismatch_evidence,
                "remote": remote_evidence,
                "data": data_evidence,
                "none": no_reference_evidence,
                "comparison": comparison,
            },
            ensure_ascii=False,
        )

    assert _check_status(match_evidence, "reference-fidelity") == "pass"
    assert _check_status(similar_evidence, "reference-fidelity") == "pass"
    assert _check_status(mismatch_evidence, "reference-fidelity") == "warn"
    assert _check_status(remote_evidence, "reference-fidelity") == "skipped"
    assert _check_status(data_evidence, "reference-fidelity") == "skipped"
    assert _check_status(no_reference_evidence, "reference-fidelity") == "skipped"
    assert match_evidence["imageAnalysis"]["summary"]["referenceComparedCount"] == 1
    assert similar_evidence["imageAnalysis"]["summary"]["referenceComparedCount"] == 1
    assert data_evidence["imageAnalysis"]["summary"]["remoteReferenceCount"] == 1
    assert no_reference_evidence["imageAnalysis"]["summary"]["referenceCount"] == 0
    assert comparison["referenceSimilarityPct"] >= 90
    assert similar_is_distinct
    assert "reference-fidelity" in match_evidence["qualityGates"]
    assert str(reference) not in serialized
    assert str(candidate) not in serialized
    assert "token=secret" not in serialized
    assert "example.invalid" not in serialized
    assert "PRIVATEINLINEPAYLOAD" not in serialized


def test_image_finalization_decision_recommends_retry_and_records_safe_details():
    with tempfile.TemporaryDirectory() as workspace:
        blank = Path(workspace) / "blank.png"
        Image.new("RGB", (80, 80), "white").save(blank)

        evidence = build_image_quality_evidence(blank)
        decision = build_image_finalization_decision(evidence, retry_count=0, max_retries=1)
        annotated = attach_image_finalization_evidence(evidence, decision)
        serialized = json.dumps({"decision": decision, "annotated": annotated}, ensure_ascii=False)

    assert decision["status"] == "retry"
    assert decision["retryRecommended"] is True
    assert decision["retryGate"] == "non-blank"
    assert annotated["imageAnalysis"]["summary"]["finalizationStatus"] == "retry"
    assert annotated["imageAnalysis"]["summary"]["retryRecommended"] is True
    assert _check_status(annotated, "visual-inspection") == "warn"
    assert "retry_count=0" in serialized
    assert "max_retries=1" in serialized
    assert str(blank) not in serialized


def test_image_job_service_attaches_structural_quality_evidence_to_artifacts(monkeypatch):
    from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests
    from agent.protocol import image_job_service as image_job_module

    monkeypatch.setattr(image_job_module, "_authorize_reference_read", lambda _path: True)

    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        image_path = root / "service.png"
        _write_gradient(image_path)
        ledger = reset_run_event_ledger_for_tests(root / "runtime-events.db")

        def runner(_task, _progress, _cancel_event):
            return {
                "kind": "image",
                "path": str(image_path),
                "title": "service.png",
                "qualityEvidence": {
                    "kind": "image",
                    "status": "fail",
                    "checks": [{"id": "private prompt", "status": "fail", "detail": str(image_path)}],
                },
            }

        status = ImageJobService(ledger).start(
            request_id="req-v024-image-quality-job",
            session_id="session-v024-image-quality-job",
            job_id="image-job-v024-quality",
            tasks=[{"task_id": "task-1", "image_url": str(image_path)}],
            runner=runner,
            synchronous=True,
        )
        projection = RuntimeProjectionService(ledger).request_projection("req-v024-image-quality-job")
        artifact = projection["image_jobs"][0]["artifacts"][0]
        serialized = json.dumps({"status": status, "projection": projection}, ensure_ascii=False)

    evidence = artifact["qualityEvidence"]
    assert evidence["kind"] == "image"
    assert evidence["status"] == "pass"
    assert "decode-valid" in evidence["qualityGates"]
    assert "reference-fidelity" in evidence["qualityGates"]
    assert _check_status(evidence, "decode-valid") == "pass"
    assert _check_status(evidence, "reference-fidelity") == "pass"
    assert "private prompt" not in serialized
    assert str(image_path) not in serialized


def test_image_job_service_projects_safe_timing_breakdown(monkeypatch):
    from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests
    from agent.protocol import image_job_service as image_job_module

    monkeypatch.setattr(image_job_module, "_authorize_reference_read", lambda _path: True)

    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        image_path = root / "timing.png"
        _write_gradient(image_path)
        ledger = reset_run_event_ledger_for_tests(root / "runtime-events.db")

        def runner(_task, _progress, _cancel_event):
            return {"kind": "image", "path": str(image_path), "title": "timing.png"}

        ImageJobService(ledger).start(
            request_id="req-v024-image-timing",
            session_id="session-v024-image-timing",
            job_id="image-job-v024-timing",
            tasks=[{"task_id": "task-1"}],
            runner=runner,
            synchronous=True,
        )
        events = ledger.events_for_request("req-v024-image-timing", limit=0)
        projection = RuntimeProjectionService(ledger).request_projection("req-v024-image-timing")
        job = projection["image_jobs"][0]
        task = job["tasks"][0]
        serialized = json.dumps({"events": events, "projection": projection}, ensure_ascii=False)

    progress_statuses = [
        event["payload"].get("status")
        for event in events
        if event.get("event_type") == "image_job.progress"
    ]
    projected_progress_statuses = [
        event["payload"].get("status")
        for event in projection.get("events") or []
        if event.get("event_type") == "image_job.progress"
    ]
    assert "provider_response" in progress_statuses
    assert "quality_check" in progress_statuses
    assert "provider_response" in projected_progress_statuses
    assert "quality_check" in projected_progress_statuses
    assert isinstance(task["provider_latency_ms"], int)
    assert isinstance(task["quality_latency_ms"], int)
    assert isinstance(task["finalization_latency_ms"], int)
    assert isinstance(task["postprocess_latency_ms"], int)
    assert job["provider_total_ms"] >= task["provider_latency_ms"]
    assert job["postprocess_total_ms"] >= task["postprocess_latency_ms"]
    assert str(image_path) not in serialized


def test_image_job_service_skips_unauthorized_local_reference_images(monkeypatch):
    from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests
    from agent.protocol import image_job_service as image_job_module

    monkeypatch.setattr(image_job_module, "_authorize_reference_read", lambda _path: False)

    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        output_path = root / "output.png"
        reference_path = root / "reference.png"
        _write_gradient(output_path)
        _write_gradient(reference_path)
        ledger = reset_run_event_ledger_for_tests(root / "runtime-events.db")

        def runner(_task, _progress, _cancel_event):
            return {
                "kind": "image",
                "path": str(output_path),
                "title": "output.png",
                "reference_image": str(reference_path),
            }

        ImageJobService(ledger).start(
            request_id="req-v024-image-quality-ref-denied",
            session_id="session-v024-image-quality-ref-denied",
            job_id="image-job-v024-quality-ref-denied",
            tasks=[{"task_id": "task-1", "image_url": str(reference_path)}],
            runner=runner,
            synchronous=True,
        )
        projection = RuntimeProjectionService(ledger).request_projection("req-v024-image-quality-ref-denied")
        artifact = projection["image_jobs"][0]["artifacts"][0]
        serialized = json.dumps({"projection": projection}, ensure_ascii=False)

    evidence = artifact["qualityEvidence"]
    assert _check_status(evidence, "reference-fidelity") == "skipped"
    assert evidence["imageAnalysis"]["summary"]["referenceComparedCount"] == 0
    assert str(reference_path) not in serialized


def test_image_job_service_retries_failed_quality_before_final_artifact(monkeypatch):
    from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests
    from agent.protocol import image_job_service as image_job_module

    monkeypatch.setattr(image_job_module, "_authorize_reference_read", lambda _path: True)

    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        blank_path = root / "blank.png"
        clean_path = root / "clean.png"
        Image.new("RGB", (80, 80), "white").save(blank_path)
        _write_gradient(clean_path)
        ledger = reset_run_event_ledger_for_tests(root / "runtime-events.db")
        calls = []

        def runner(task, _progress, _cancel_event):
            calls.append(dict(task))
            path = blank_path if len(calls) == 1 else clean_path
            return {"kind": "image", "path": str(path), "title": f"attempt-{len(calls)}.png"}

        status = ImageJobService(ledger).start(
            request_id="req-v024-image-quality-retry",
            session_id="session-v024-image-quality-retry",
            job_id="image-job-v024-quality-retry",
            tasks=[{"task_id": "task-1", "quality_retry_max": 1}],
            runner=runner,
            synchronous=True,
        )
        events = ledger.events_for_request("req-v024-image-quality-retry", limit=0)
        projection = RuntimeProjectionService(ledger).request_projection("req-v024-image-quality-retry")
        artifact = projection["image_jobs"][0]["artifacts"][0]
        serialized = json.dumps({"status": status, "events": events, "projection": projection}, ensure_ascii=False)

    assert len(calls) == 2
    assert calls[1]["_quality_retry_attempt"] == 1
    assert status["status"] == "completed"
    assert status["artifacts"][0]["title"] == "attempt-2.png"
    assert projection["image_jobs"][0]["artifact_count"] == 1
    assert artifact["qualityEvidence"]["imageAnalysis"]["summary"]["finalizationStatus"] == "final"
    assert artifact["qualityEvidence"]["imageAnalysis"]["summary"]["retryCount"] == 1
    assert any(
        event["event_type"] == "image_job.progress" and event["payload"].get("status") == "retry"
        for event in events
    )
    assert str(blank_path) not in serialized
    assert str(clean_path) not in serialized


def test_image_job_service_cancel_after_quality_retry_progress_stops_next_attempt(monkeypatch):
    from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests
    from agent.protocol import image_job_service as image_job_module

    monkeypatch.setattr(image_job_module, "_authorize_reference_read", lambda _path: True)

    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        blank_path = root / "blank.png"
        Image.new("RGB", (80, 80), "white").save(blank_path)
        ledger = reset_run_event_ledger_for_tests(root / "runtime-events.db")
        service = ImageJobService(ledger)
        calls = []
        original_emit_progress = service._emit_progress

        def emit_progress_and_cancel(state, task_id, status, *, progress=None, index=0, detail=None):
            event = original_emit_progress(
                state,
                task_id,
                status,
                progress=progress,
                index=index,
                detail=detail,
            )
            if status == "retry":
                state.cancel_event.set()
            return event

        monkeypatch.setattr(service, "_emit_progress", emit_progress_and_cancel)

        def runner(task, _progress, _cancel_event):
            calls.append(dict(task))
            return {"kind": "image", "path": str(blank_path), "title": "cancel-before-retry.png"}

        status = service.start(
            request_id="req-v024-image-quality-retry-cancel",
            session_id="session-v024-image-quality-retry-cancel",
            job_id="image-job-v024-quality-retry-cancel",
            tasks=[{"task_id": "task-1", "quality_retry_max": 1}],
            runner=runner,
            synchronous=True,
        )
        events = ledger.events_for_request("req-v024-image-quality-retry-cancel", limit=0)
        projection = RuntimeProjectionService(ledger).request_projection("req-v024-image-quality-retry-cancel")
        serialized = json.dumps({"status": status, "events": events, "projection": projection}, ensure_ascii=False)

    assert len(calls) == 1
    assert status["status"] == "cancelled"
    assert status["artifacts"] == []
    event_types = [event["event_type"] for event in events]
    assert "image_job.cancelled" in event_types
    assert "image_job.artifact" not in event_types
    assert projection["image_jobs"][0]["status"] == "cancelled"
    assert str(blank_path) not in serialized


def test_imagegen_tool_attaches_reference_fidelity_for_local_outputs():
    from agent.tools.imagegen.imagegen import _with_image_quality_evidence

    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        output = root / "output.png"
        reference = root / "reference.png"
        _write_gradient(output)
        _write_gradient(reference)
        images, aggregate = _with_image_quality_evidence(
            [{"path": str(output), "kind": "image"}],
            reference_images=[str(reference)],
        )
        serialized = json.dumps({"images": images, "aggregate": aggregate}, ensure_ascii=False)

    assert images[0]["qualityEvidence"]["kind"] == "image"
    assert _check_status(images[0]["qualityEvidence"], "reference-fidelity") == "pass"
    assert aggregate and "reference-fidelity" in aggregate["qualityGates"]
    assert str(reference) not in serialized


def test_imagegen_tool_drops_caller_quality_evidence_from_remote_images():
    from agent.tools.imagegen.imagegen import _with_image_quality_evidence

    malicious_path = "C:/Users/Alice/secret.png"
    images, aggregate = _with_image_quality_evidence(
        [
            {
                "url": "https://example.invalid/generated.png",
                "qualityEvidence": {
                    "kind": "image",
                    "status": "fail",
                    "checks": [
                        {
                            "id": "private prompt",
                            "status": "fail",
                            "detail": f"sk-private-token {malicious_path}",
                        }
                    ],
                },
                "quality_evidence": {"sourceRef": malicious_path},
            }
        ]
    )
    serialized = json.dumps({"images": images, "aggregate": aggregate}, ensure_ascii=False)

    assert images == [{"url": "https://example.invalid/generated.png"}]
    assert aggregate is None
    assert "qualityEvidence" not in serialized
    assert "quality_evidence" not in serialized
    assert "private prompt" not in serialized
    assert "sk-private-token" not in serialized
    assert malicious_path not in serialized


def test_imagegen_tool_failure_payload_is_content_safe(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from agent.tools.imagegen import imagegen as imagegen_module

    malicious_path = "C:/Users/Alice/secret.png"
    malicious_url = "https://example.test/render.png"

    def fake_provider_run(*_args, **_kwargs):
        return {
            "returncode": 1,
            "payload": {
                "provider": "OpenAI",
                "model": "gpt-image-test",
                "attempted_provider_count": 1,
                "error": f"private prompt sk-private-token {malicious_path} {malicious_url}",
                "provider_raw_response": {"body": "private prompt"},
                "images": [
                    {
                        "url": malicious_url,
                        "path": malicious_path,
                        "qualityEvidence": {
                            "checks": [
                                {
                                    "id": "private prompt",
                                    "status": "fail",
                                    "detail": f"sk-private-token {malicious_path}",
                                }
                            ]
                        },
                    }
                ],
            },
            "stderr": f"private prompt sk-private-token {malicious_path} {malicious_url}",
        }

    monkeypatch.setattr(imagegen_module, "_authorize_file_access", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(imagegen_module, "run_image_generation_payload", fake_provider_run)

    result = imagegen_module.ImageGenTool().execute(
        {"prompt": "private prompt", "output_dir": str(tmp_path / "images")}
    )
    serialized = json.dumps(result.result, ensure_ascii=False)

    assert result.status == "error"
    assert result.result["error"] == "image generation failed"
    assert result.result["payload"]["redacted"] is True
    assert result.result["payload"]["provider"] == "OpenAI"
    assert result.result["payload"]["imageCount"] == 1
    assert result.result["route"]["executionMode"] == "in_process_provider_runner"
    assert result.result["route"]["pythonSubprocess"] is False
    assert result.result["pythonFallbackUsed"] is False
    assert "qualityEvidence" not in serialized
    assert "quality_evidence" not in serialized
    assert "provider_raw_response" not in serialized
    assert "private prompt" not in serialized
    assert "sk-private-token" not in serialized
    assert malicious_path not in serialized
    assert malicious_url not in serialized


def test_imagegen_tool_success_images_are_allowlisted_and_stderr_is_summarized(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from agent.tools.imagegen import imagegen as imagegen_module

    malicious_path = "C:/Users/Alice/secret.png"
    malicious_url = "https://example.test/secret-render.png"
    calls = []

    def fake_provider_run(payload, **_kwargs):
        calls.append(dict(payload))
        return {
            "returncode": 0,
            "payload": {
                "provider": "OpenAI",
                "model": "gpt-image-test",
                "model_fallback": "none",
                "attempted_provider_count": 1,
                "provider_raw_response": "private prompt",
                "images": [
                    {
                        "url": "https://safe.example/generated.png",
                        "provider": "OpenAI",
                        "model": "gpt-image-test",
                        "width": 512,
                        "height": 512,
                        "prompt": "private prompt",
                        "rawText": f"sk-private-token {malicious_path}",
                        "provider_raw_response": {"url": malicious_url},
                        "qualityEvidence": {
                            "checks": [
                                {
                                    "id": "private prompt",
                                    "status": "fail",
                                    "detail": f"sk-private-token {malicious_path}",
                                }
                            ]
                        },
                    }
                ],
            },
            "stderr": f"private prompt sk-private-token {malicious_path} {malicious_url}",
        }

    monkeypatch.setattr(imagegen_module, "_authorize_file_access", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(imagegen_module, "run_image_generation_payload", fake_provider_run)

    result = imagegen_module.ImageGenTool().execute(
        {"prompt": "private prompt", "output_dir": str(tmp_path / "images")}
    )
    serialized = json.dumps(result.result, ensure_ascii=False)

    assert result.status == "success"
    assert len(calls) == 1
    assert result.result["images"] == [
        {
            "url": "https://safe.example/generated.png",
            "provider": "OpenAI",
            "model": "gpt-image-test",
            "width": 512,
            "height": 512,
        }
    ]
    assert "stderrTail" not in result.result
    assert result.result["stderr"] == {
        "present": True,
        "charCount": len(f"private prompt sk-private-token {malicious_path} {malicious_url}"),
        "redacted": True,
    }
    assert result.result["timing"]["attemptCount"] == 1
    assert result.result["timing"]["retryCount"] == 0
    assert result.result["route"]["executionMode"] == "in_process_provider_runner"
    assert result.result["route"]["shellInvocation"] is False
    assert result.result["pythonFallbackUsed"] is False
    assert isinstance(result.result["timing"]["providerTotalLatencyMs"], int)
    assert isinstance(result.result["timing"]["postprocessTotalLatencyMs"], int)
    assert result.result["finalization"]["status"] == "unknown"
    assert "qualityEvidence" not in result.result
    assert "qualityEvidence" not in serialized
    assert "quality_evidence" not in serialized
    assert "provider_raw_response" not in serialized
    assert "rawText" not in serialized
    assert "private prompt" not in serialized
    assert "sk-private-token" not in serialized
    assert malicious_path not in serialized
    assert malicious_url not in serialized


def test_imagegen_tool_routes_image_urls_as_reference_edit_payload(monkeypatch, tmp_path):
    from agent.tools.imagegen import imagegen as imagegen_module

    calls = []

    def fake_provider_run(payload, **_kwargs):
        calls.append(dict(payload))
        return {
            "returncode": 0,
            "payload": {
                "provider": "OpenAI",
                "model": "gpt-image-2-pro",
                "attempted_provider_count": 1,
                "images": [{"url": "https://safe.example/generated.png"}],
            },
            "stderr": "",
        }

    refs = [tmp_path / "ref-a.png", tmp_path / "ref-b.png"]
    monkeypatch.setattr(imagegen_module, "_authorize_file_access", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(imagegen_module, "run_image_generation_payload", fake_provider_run)

    result = imagegen_module.ImageGenTool().execute(
        {
            "prompt": "combine the references",
            "image_urls": [str(refs[0]), str(refs[1])],
            "output_dir": str(tmp_path / "images"),
        }
    )

    assert result.status == "success"
    assert calls[0]["image_url"] == [str(ref.resolve()) for ref in refs]
    assert result.result["model"] == "gpt-image-2-pro"
    assert result.result["route"]["inputRoute"] == "image_edit_reference"
    assert result.result["route"]["pythonSubprocess"] is False


def test_imagegen_tool_batches_tasks_without_shell_or_python_fallback(monkeypatch, tmp_path):
    from agent.tools.imagegen import imagegen as imagegen_module

    calls = []

    def fake_provider_run(payload, **_kwargs):
        calls.append(dict(payload))
        return {
            "returncode": 0,
            "payload": {
                "provider": "OpenAI",
                "model": "gpt-image-2-pro",
                "attempted_provider_count": 1,
                "images": [{"url": f"https://safe.example/generated-{len(calls)}.png"}],
            },
            "stderr": "",
        }

    monkeypatch.setattr(imagegen_module, "_authorize_file_access", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(imagegen_module, "run_image_generation_payload", fake_provider_run)

    result = imagegen_module.ImageGenTool().execute(
        {
            "tasks": [
                {"prompt": "batch image one", "aspect_ratio": "1:1"},
                {"prompt": "batch image two", "aspect_ratio": "16:9"},
            ],
            "output_dir": str(tmp_path / "images"),
        }
    )

    assert result.status == "success"
    assert len(calls) == 2
    assert calls[0]["prompt"] == "batch image one"
    assert calls[1]["prompt"] == "batch image two"
    assert result.result["batchMode"] == "native_imagegen_tool_loop"
    assert result.result["route"]["providerApiRoute"] == "native.batch.imagegen"
    assert result.result["pythonFallbackUsed"] is False
    assert result.result["shellFallbackUsed"] is False
    assert result.result["webFallbackUsed"] is False
    assert [item["taskIndex"] for item in result.result["images"]] == [0, 1]
    assert all(item["model"] == "gpt-image-2-pro" for item in result.result["taskResults"])


def test_imagegen_tool_retries_local_quality_failure_before_success(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from agent.tools.imagegen import imagegen as imagegen_module

    output_dir = tmp_path / "images"
    output_dir.mkdir()
    blank_path = output_dir / "blank.png"
    clean_path = output_dir / "clean.png"
    Image.new("RGB", (80, 80), "white").save(blank_path)
    _write_gradient(clean_path)
    calls = []

    def fake_provider_run(payload, **_kwargs):
        calls.append(dict(payload))
        path = blank_path if len(calls) == 1 else clean_path
        return {
            "returncode": 0,
            "payload": {
                "provider": "OpenAI",
                "model": "gpt-image-test",
                "attempted_provider_count": 1,
                "images": [{"url": str(path), "provider": "OpenAI", "model": "gpt-image-test"}],
            },
            "stderr": "",
        }

    monkeypatch.setattr(imagegen_module, "_authorize_file_access", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(imagegen_module, "run_image_generation_payload", fake_provider_run)

    result = imagegen_module.ImageGenTool().execute(
        {
            "prompt": "private prompt",
            "output_dir": str(output_dir),
            "quality_retry_max": 1,
        }
    )
    serialized = json.dumps(result.result, ensure_ascii=False)

    assert result.status == "success"
    assert len(calls) == 2
    assert calls[0]["prompt"] == "private prompt"
    assert calls[1]["prompt"].startswith("private prompt")
    assert result.result["finalization"]["status"] == "final"
    assert result.result["finalization"]["retryCount"] == 1
    assert result.result["timing"]["attemptCount"] == 2
    assert result.result["timing"]["retryCount"] == 1
    assert isinstance(result.result["timing"]["qualityTotalLatencyMs"], int)
    assert result.result["timing"]["totalLatencyMs"] == result.result["durationMs"]
    assert result.result["images"][0]["url"] == str(clean_path)
    assert result.result["qualityEvidence"]["status"] == "pass"
    assert str(blank_path) not in serialized


def test_imagegen_provider_runner_env_overlay_is_thread_safe():
    from agent.tools.imagegen import provider_runner

    keys = set(provider_runner.CONFIG_TO_ENV.values()) | {
        "SKILL_IMAGE_GENERATION_MODEL",
        "SKILL_IMAGE_GENERATION_PROVIDER",
    }
    saved_env = {key: os.environ.get(key) for key in keys}
    first_inside = threading.Event()
    second_about_to_call = threading.Event()
    seen: list[tuple[str, str | None, str | None]] = []
    errors: list[BaseException] = []

    class FakeModule:
        def _build_providers(self, model, provider_id=""):
            seen.append((str(provider_id), os.environ.get("OPENAI_API_KEY"), os.environ.get("OPENAI_API_BASE")))
            if provider_id == "first":
                first_inside.set()
                if not second_about_to_call.wait(timeout=5):
                    raise AssertionError("second provider call did not start")
                time.sleep(0.15)
            return {"provider": object()}

    def worker(provider_id: str, key: str, base: str) -> None:
        try:
            if provider_id == "second":
                if not first_inside.wait(timeout=5):
                    raise AssertionError("first provider call did not enter")
                second_about_to_call.set()
            provider_runner._build_providers_with_env(
                FakeModule(),
                model="model-test",
                provider_id=provider_id,
                env={
                    "OPENAI_API_KEY": key,
                    "OPENAI_API_BASE": base,
                },
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ["OPENAI_API_KEY"] = "outer-key"
        os.environ["OPENAI_API_BASE"] = "outer-base"
        first = threading.Thread(target=worker, args=("first", "first-key", "first-base"))
        second = threading.Thread(target=worker, args=("second", "second-key", "second-base"))
        first.start()
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)

        assert not first.is_alive()
        assert not second.is_alive()
        assert errors == []
        assert ("first", "first-key", "first-base") in seen
        assert ("second", "second-key", "second-base") in seen
        assert os.environ.get("OPENAI_API_KEY") == "outer-key"
        assert os.environ.get("OPENAI_API_BASE") == "outer-base"
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_imagegen_provider_runner_accepts_image_urls_alias(tmp_path):
    from agent.tools.imagegen import provider_runner

    script = tmp_path / "generate.py"
    script.write_text(
        """
from pathlib import Path

CALLS = []
LAST_MODEL = ""
LAST_PROVIDER_ID = ""


class OpenAIProvider:
    DEFAULT_MODEL = "gpt-image-2-pro"


class FakeProvider:
    model = "gpt-image-2-pro"
    model_fallback = None

    def generate(self, prompt, **kwargs):
        CALLS.append({"prompt": prompt, **kwargs})
        return [str(Path(kwargs["output_dir"]) / "out.png")]


def _build_providers(model, provider_id=""):
    global LAST_MODEL, LAST_PROVIDER_ID
    LAST_MODEL = model
    LAST_PROVIDER_ID = provider_id
    provider = FakeProvider()
    provider.model = model
    return [("OpenAI", provider)]


def _provider_error_from_exception(label, exc):
    raise exc
""",
        encoding="utf-8",
    )

    result = provider_runner.run_image_generation_payload(
        {
            "prompt": "reference-guided edit",
            "image_urls": ["ref-a.png", "ref-b.png"],
        },
        script_path=script,
        output_dir=tmp_path / "images",
        env={"OPENAI_API_KEY": "sk-test"},
    )
    module = provider_runner.load_image_generation_module(script)

    assert result["returncode"] == 0
    assert result["payload"]["model"] == "gpt-image-2-pro"
    assert module.LAST_MODEL == "gpt-image-2-pro"
    assert module.CALLS[0]["image_url"] == ["ref-a.png", "ref-b.png"]


def test_imagegen_provider_runner_ignores_legacy_skill_model_by_default(tmp_path):
    from agent.tools.imagegen import provider_runner

    script = tmp_path / "generate.py"
    script.write_text(
        """
from pathlib import Path

LAST_MODEL = ""
LAST_PROVIDER_ID = ""


class OpenAIProvider:
    DEFAULT_MODEL = "gpt-image-2-pro"


class FakeProvider:
    model = "gpt-image-2-pro"
    model_fallback = None

    def generate(self, prompt, **kwargs):
        return [str(Path(kwargs["output_dir"]) / "out.png")]


def _build_providers(model, provider_id=""):
    global LAST_MODEL, LAST_PROVIDER_ID
    LAST_MODEL = model
    LAST_PROVIDER_ID = provider_id
    provider = FakeProvider()
    provider.model = model
    return [("OpenAI", provider)]


def _provider_error_from_exception(label, exc):
    raise exc
""",
        encoding="utf-8",
    )

    result = provider_runner.run_image_generation_payload(
        {"prompt": "default image route"},
        script_path=script,
        output_dir=tmp_path / "images",
        env={
            "OPENAI_API_KEY": "sk-test",
            "SKILL_IMAGE_GENERATION_MODEL": "nano-banana-2",
            "SKILL_IMAGE_GENERATION_PROVIDER": "gemini",
        },
    )
    module = provider_runner.load_image_generation_module(script)

    assert result["returncode"] == 0
    assert result["payload"]["model"] == "gpt-image-2-pro"
    assert module.LAST_MODEL == "gpt-image-2-pro"
    assert module.LAST_PROVIDER_ID == ""


def _load_efficiency_benchmark_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "smoke-v024-imagegen-efficiency-benchmark.py"
    spec = importlib.util.spec_from_file_location("v024_imagegen_efficiency_benchmark", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _benchmark_args(mode: str, **overrides):
    values = {
        "mode": mode,
        "provider": "openai",
        "provider_delay_ms": 250,
        "codex_result": None,
        "output": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _no_provider_readiness() -> dict:
    return {
        "openai": False,
        "linkai": False,
        "gemini": False,
        "seedream": False,
        "qwen": False,
        "minimax": False,
    }


def test_imagegen_efficiency_preflight_blocks_without_provider_credentials(monkeypatch):
    benchmark = _load_efficiency_benchmark_module()
    monkeypatch.setattr(benchmark, "_real_provider_readiness", _no_provider_readiness)

    payload = benchmark.run(_benchmark_args("preflight"))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["status"] == "BLOCKED"
    assert payload["mode"] == "real-provider-preflight"
    assert payload["ready"] is False
    assert payload["realProviderReady"] == _no_provider_readiness()
    assert "cases" not in payload
    assert "sk-" not in serialized


def test_imagegen_efficiency_real_mode_blocks_instead_of_falling_back_to_fake(monkeypatch):
    benchmark = _load_efficiency_benchmark_module()
    monkeypatch.setattr(benchmark, "_real_provider_readiness", _no_provider_readiness)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("real mode without credentials must not run cases")

    monkeypatch.setattr(benchmark, "_run_cases", fail_if_called)
    payload = benchmark.run(_benchmark_args("real"))

    assert payload["status"] == "BLOCKED"
    assert payload["mode"] == "real-provider-benchmark"
    assert payload["cases"] == []
    assert payload["failedCases"] == []
    assert payload["codexComparison"]["status"] == "pending-codex-result"


def test_imagegen_efficiency_codex_template_is_redacted_and_not_ready(tmp_path):
    benchmark = _load_efficiency_benchmark_module()

    template = benchmark.run(_benchmark_args("codex-template"))
    serialized = json.dumps(template, ensure_ascii=False)
    template_path = tmp_path / "codex-template.json"
    template_path.write_text(json.dumps(template), encoding="utf-8")
    loaded = benchmark._load_codex_result(template_path)

    assert template["status"] == "TEMPLATE"
    assert template["mode"] == "codex-imagegen-timing-template"
    assert template["schemaVersion"] == "r24-14b-codex-timing-v1"
    assert len(template["cases"]) == 2
    assert template["cases"][0]["promptHash"]
    assert template["cases"][0]["finalUsableMs"] == 0
    assert "Create a clean square app icon" not in serialized
    assert "Use the reference colors" not in serialized
    assert loaded["available"] is False
    assert loaded["status"] == "incomplete-codex-result"


def test_imagegen_efficiency_valid_codex_result_enables_case_comparison(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    for index, item in enumerate(template["cases"]):
        item["status"] = "pass"
        item["finalUsableMs"] = 1000 + index * 100
    result_path = tmp_path / "codex-result.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")
    loaded = benchmark._load_codex_result(result_path)

    comparison = benchmark._comparison(
        template["cases"][0]["caseId"],
        {"finalUsableMs": 1100},
        loaded,
    )

    assert loaded["available"] is True
    assert loaded["status"] == "ready"
    assert loaded["schemaVersion"] == "r24-14b-codex-timing-v1"
    assert loaded["caseCount"] == 2
    assert len(loaded["sourceSha256"]) == 64
    assert comparison["available"] is True
    assert comparison["codexFinalUsableMs"] == 1000
    assert comparison["deltaPct"] == 10.0


def test_imagegen_efficiency_codex_result_mode_canonicalizes_valid_result(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    for index, item in enumerate(template["cases"]):
        item["status"] = "pass"
        item["finalUsableMs"] = 1000 + index * 100
    result_path = tmp_path / "codex-result-candidate.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    payload = benchmark.run(_benchmark_args("codex-result", codex_result=result_path))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["status"] == "PASS"
    assert payload["redacted"] is True
    assert payload["mode"] == "codex-imagegen-timing-result"
    assert payload["schemaVersion"] == "r24-14b-codex-timing-v1"
    assert len(payload["cases"]) == 2
    assert payload["cases"][0]["finalUsableMs"] == 1000
    assert "timingSemantics" not in payload
    assert "resultModeRequired" not in payload
    assert str(result_path) not in serialized
    assert "Create a clean square app icon" not in serialized


def test_imagegen_efficiency_codex_result_mode_reports_invalid_without_raw_content(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    for index, item in enumerate(template["cases"]):
        item["status"] = "pass"
        item["finalUsableMs"] = 1000 + index * 100
    template["cases"][0]["rawPrompt"] = "Create a clean square app icon"
    result_path = tmp_path / "codex-result-candidate-invalid.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    payload = benchmark.run(_benchmark_args("codex-result", codex_result=result_path))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["status"] == "FAIL"
    assert payload["redacted"] is True
    assert payload["mode"] == "codex-imagegen-timing-result"
    assert payload["reason"] == "invalid-codex-result"
    assert payload["unknownCaseKeyCaseCount"] == 1
    assert payload["unknownCaseKeyCount"] == 1
    assert "Create a clean square app icon" not in serialized
    assert str(result_path) not in serialized


def test_imagegen_efficiency_codex_result_mode_redacts_unknown_key_names(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    for index, item in enumerate(template["cases"]):
        item["status"] = "pass"
        item["finalUsableMs"] = 1000 + index * 100
    template["C:/Users/Alice/sk-private-token"] = True
    result_path = tmp_path / "codex-result-candidate-secret-key.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    payload = benchmark.run(_benchmark_args("codex-result", codex_result=result_path))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["status"] == "FAIL"
    assert payload["unknownTopLevelKeyCount"] == 1
    assert "C:/Users/Alice" not in serialized
    assert "sk-private-token" not in serialized

    case_key_template = benchmark.run(_benchmark_args("codex-template"))
    case_key_template["status"] = "PASS"
    case_key_template["mode"] = "codex-imagegen-timing-result"
    for index, item in enumerate(case_key_template["cases"]):
        item["status"] = "pass"
        item["finalUsableMs"] = 1000 + index * 100
    case_key_template["cases"][0]["https://secret.example/sk-private-token"] = True
    case_key_path = tmp_path / "codex-result-candidate-secret-case-key.json"
    case_key_path.write_text(json.dumps(case_key_template), encoding="utf-8")

    case_key_payload = benchmark.run(_benchmark_args("codex-result", codex_result=case_key_path))
    case_key_serialized = json.dumps(case_key_payload, ensure_ascii=False)

    assert case_key_payload["status"] == "FAIL"
    assert case_key_payload["unknownCaseKeyCaseCount"] == 1
    assert case_key_payload["unknownCaseKeyCount"] == 1
    assert "secret.example" not in case_key_serialized
    assert "sk-private-token" not in case_key_serialized


def test_imagegen_efficiency_rejects_codex_result_missing_schema_version(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    template.pop("schemaVersion", None)
    for index, item in enumerate(template["cases"]):
        item["status"] = "pass"
        item["finalUsableMs"] = 1000 + index * 100
    result_path = tmp_path / "codex-result-missing-schema.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    loaded = benchmark._load_codex_result(result_path)

    assert loaded["available"] is False
    assert loaded["status"] == "invalid-codex-result"
    assert loaded["expectedSchemaVersion"] == "r24-14b-codex-timing-v1"


def test_imagegen_efficiency_rejects_pass_codex_template_mode(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    for index, item in enumerate(template["cases"]):
        item["status"] = "pass"
        item["finalUsableMs"] = 1000 + index * 100
    result_path = tmp_path / "codex-result-template-mode.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    loaded = benchmark._load_codex_result(result_path)

    assert loaded["available"] is False
    assert loaded["status"] == "invalid-codex-result"
    assert loaded["expectedMode"] == "codex-imagegen-timing-result"


def test_imagegen_efficiency_rejects_nonredacted_codex_result(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    template["redacted"] = False
    for index, item in enumerate(template["cases"]):
        item["status"] = "pass"
        item["finalUsableMs"] = 1000 + index * 100
    result_path = tmp_path / "codex-result-nonredacted.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    loaded = benchmark._load_codex_result(result_path)

    assert loaded["available"] is False
    assert loaded["status"] == "invalid-codex-result"


def test_imagegen_efficiency_rejects_codex_result_extra_raw_fields(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    for index, item in enumerate(template["cases"]):
        item["status"] = "pass"
        item["finalUsableMs"] = 1000 + index * 100
    template["cases"][0]["rawPrompt"] = "Create a clean square app icon"
    result_path = tmp_path / "codex-result-extra-raw-fields.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    loaded = benchmark._load_codex_result(result_path)

    assert loaded["available"] is False
    assert loaded["status"] == "invalid-codex-result"
    assert loaded["unknownCaseKeys"] == {template["cases"][0]["caseId"]: ["rawPrompt"]}


def test_imagegen_efficiency_rejects_codex_result_requirement_mismatch(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    for index, item in enumerate(template["cases"]):
        item["status"] = "pass"
        item["finalUsableMs"] = 1000 + index * 100
    template["cases"][0]["size"] = "1536x1024"
    result_path = tmp_path / "codex-result-requirement-mismatch.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    loaded = benchmark._load_codex_result(result_path)

    assert loaded["available"] is False
    assert loaded["status"] == "incomplete-codex-result"
    assert loaded["missingCaseIds"] == [template["cases"][0]["caseId"]]


def test_imagegen_efficiency_rejects_codex_result_prompt_length_mismatch(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    for index, item in enumerate(template["cases"]):
        item["status"] = "pass"
        item["finalUsableMs"] = 1000 + index * 100
    template["cases"][0]["promptLength"] = template["cases"][0]["promptLength"] + 1
    result_path = tmp_path / "codex-result-prompt-length-mismatch.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    loaded = benchmark._load_codex_result(result_path)

    assert loaded["available"] is False
    assert loaded["status"] == "incomplete-codex-result"
    assert loaded["missingCaseIds"] == [template["cases"][0]["caseId"]]


def test_imagegen_efficiency_rejects_codex_result_without_final_usable_time(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    for index, item in enumerate(template["cases"]):
        item["status"] = "pass"
        item["finalUsableMs"] = 1000 + index * 100
        item["wallMs"] = 1000 + index * 100
    template["cases"][0]["finalUsableMs"] = 0
    template["cases"][0]["wallMs"] = 1000
    result_path = tmp_path / "codex-result-wall-time-only.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    loaded = benchmark._load_codex_result(result_path)

    assert loaded["available"] is False
    assert loaded["status"] == "incomplete-codex-result"
    assert loaded["missingCaseIds"] == [template["cases"][0]["caseId"]]


def test_imagegen_efficiency_rejects_failed_codex_case_status(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    for index, item in enumerate(template["cases"]):
        item["status"] = "pass"
        item["finalUsableMs"] = 1000 + index * 100
    template["cases"][0]["status"] = "fail"
    result_path = tmp_path / "codex-result-failed-case-status.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    loaded = benchmark._load_codex_result(result_path)

    assert loaded["available"] is False
    assert loaded["status"] == "incomplete-codex-result"
    assert loaded["missingCaseIds"] == [template["cases"][0]["caseId"]]


def test_imagegen_efficiency_rejects_codex_result_prompt_hash_mismatch(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    template["cases"][0]["finalUsableMs"] = 1000
    template["cases"][0]["promptHash"] = "badpromptbadhash"
    result_path = tmp_path / "codex-result-bad-hash.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    loaded = benchmark._load_codex_result(result_path)

    assert loaded["available"] is False
    assert loaded["status"] == "invalid-codex-result"


def test_imagegen_efficiency_rejects_codex_result_missing_prompt_hash(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    template["cases"] = [template["cases"][0]]
    template["cases"][0]["finalUsableMs"] = 1000
    template["cases"][0].pop("promptHash", None)
    result_path = tmp_path / "codex-result-missing-hash.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    loaded = benchmark._load_codex_result(result_path)

    assert loaded["available"] is False
    assert loaded["status"] == "invalid-codex-result"


def test_imagegen_efficiency_rejects_partial_codex_result(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    template["cases"][0]["status"] = "pass"
    template["cases"][0]["finalUsableMs"] = 1000
    result_path = tmp_path / "codex-result-partial.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    loaded = benchmark._load_codex_result(result_path)

    assert loaded["available"] is False
    assert loaded["status"] == "incomplete-codex-result"
    assert loaded["missingCaseIds"] == [template["cases"][1]["caseId"]]


def test_imagegen_efficiency_rejects_duplicate_codex_case_ids(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    for item in template["cases"]:
        item["status"] = "pass"
        item["finalUsableMs"] = 1000
    template["cases"].append(dict(template["cases"][0]))
    result_path = tmp_path / "codex-result-duplicate-case.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    loaded = benchmark._load_codex_result(result_path)

    assert loaded["available"] is False
    assert loaded["status"] == "invalid-codex-result"
    assert loaded["duplicateCaseIds"] == [template["cases"][0]["caseId"]]


def test_imagegen_efficiency_rejects_unknown_codex_case_ids(tmp_path):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    for item in template["cases"]:
        item["status"] = "pass"
        item["finalUsableMs"] = 1000
    template["cases"][1]["caseId"] = "unexpected-case"
    result_path = tmp_path / "codex-result-unknown-case.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")

    loaded = benchmark._load_codex_result(result_path)

    assert loaded["available"] is False
    assert loaded["status"] == "invalid-codex-result"
    assert loaded["unknownCaseIds"] == ["unexpected-case"]


def test_imagegen_efficiency_fake_mode_redacts_prompts_and_computes_overhead(monkeypatch):
    benchmark = _load_efficiency_benchmark_module()
    monkeypatch.setattr(benchmark, "_real_provider_readiness", _no_provider_readiness)
    monkeypatch.setattr(
        benchmark,
        "_run_ecorex_direct",
        lambda *_args, **_kwargs: {
            "status": "pass",
            "finalUsableMs": 400,
            "providerTotalLatencyMs": 300,
            "qualityTotalLatencyMs": 40,
            "finalizationTotalLatencyMs": 5,
            "postprocessTotalLatencyMs": 45,
            "attemptCount": 1,
            "retryCount": 0,
        },
    )
    monkeypatch.setattr(
        benchmark,
        "_run_ecorex_job",
        lambda *_args, **_kwargs: {
            "status": "pass",
            "finalUsableMs": 500,
            "providerTotalMs": 320,
            "qualityTotalMs": 50,
            "finalizationTotalMs": 10,
            "postprocessTotalMs": 60,
            "retryCount": 0,
        },
    )

    payload = benchmark.run(_benchmark_args("fake", provider_delay_ms=250))
    serialized = json.dumps(payload, ensure_ascii=False)
    first_case = payload["cases"][0]

    assert payload["status"] == "PASS"
    assert payload["mode"] == "fake-provider-overhead"
    assert "in-process provider runner latency" in payload["timingSemantics"]["providerLatencyMs"]
    assert "subprocess" not in payload["timingSemantics"]["providerLatencyMs"].lower()
    assert first_case["promptHash"]
    assert first_case["promptLength"] > 0
    assert "Create a clean square app icon" not in serialized
    assert "Use the reference colors" not in serialized
    assert "sk-v024-imagegen-benchmark" not in serialized
    assert first_case["ecorexDirect"]["providerRunnerOverheadMs"] == 50
    assert first_case["ecorexDirect"]["qaAndFinalizationMs"] == 45
    assert first_case["ecorexDirect"]["ecorexControllableOverheadMs"] == 150


def test_imagegen_efficiency_codex_summary_is_redacted_in_benchmark_payload(tmp_path, monkeypatch):
    benchmark = _load_efficiency_benchmark_module()
    template = benchmark.run(_benchmark_args("codex-template"))
    template["status"] = "PASS"
    template["mode"] = "codex-imagegen-timing-result"
    for index, item in enumerate(template["cases"]):
        item["status"] = "pass"
        item["finalUsableMs"] = 1000 + index * 100
    result_path = tmp_path / "codex-result.json"
    result_path.write_text(json.dumps(template), encoding="utf-8")
    monkeypatch.setattr(benchmark, "_real_provider_readiness", _no_provider_readiness)
    monkeypatch.setattr(
        benchmark,
        "_run_ecorex_direct",
        lambda *_args, **_kwargs: {
            "status": "pass",
            "finalUsableMs": 1100,
            "providerTotalLatencyMs": 300,
            "qualityTotalLatencyMs": 40,
            "finalizationTotalLatencyMs": 5,
            "postprocessTotalLatencyMs": 45,
            "attemptCount": 1,
            "retryCount": 0,
        },
    )
    monkeypatch.setattr(
        benchmark,
        "_run_ecorex_job",
        lambda *_args, **_kwargs: {
            "status": "pass",
            "finalUsableMs": 1200,
            "providerTotalMs": 320,
            "qualityTotalMs": 50,
            "finalizationTotalMs": 10,
            "postprocessTotalMs": 60,
            "retryCount": 0,
        },
    )

    payload = benchmark.run(_benchmark_args("fake", codex_result=result_path))
    serialized = json.dumps(payload, ensure_ascii=False)
    summary = payload["codexComparison"]

    assert summary["available"] is True
    assert summary["status"] == "ready"
    assert summary["schemaVersion"] == "r24-14b-codex-timing-v1"
    assert summary["caseCount"] == 2
    assert len(summary["sourceSha256"]) == 64
    assert summary["validatedBy"] == "ecorex-v024-imagegen-efficiency-loader"
    assert str(result_path) not in serialized
    assert "Create a clean square app icon" not in serialized
