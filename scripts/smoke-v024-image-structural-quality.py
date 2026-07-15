#!/usr/bin/env python3
"""Smoke R24-10/R24-12 QA evidence for generated image artifacts."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.image_quality_runtime import build_image_quality_evidence  # noqa: E402


def _require_pillow():
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:  # pragma: no cover - smoke host guard
        raise RuntimeError(f"Pillow is required for image structural QA smoke: {exc.__class__.__name__}") from exc
    return Image, ImageDraw


def _status(evidence: dict[str, Any], check_id: str) -> str:
    for check in evidence.get("checks") or []:
        if isinstance(check, dict) and check.get("id") == check_id:
            return str(check.get("status") or "")
    return ""


def _write_gradient(path: Path, Image: Any, ImageDraw: Any) -> None:
    image = Image.new("RGB", (96, 96))
    draw = ImageDraw.Draw(image)
    for x in range(96):
        draw.line([(x, 0), (x, 95)], fill=(40 + x, 120, 210 - x))
    image.save(path)


def _fixtures(root: Path) -> dict[str, Path]:
    Image, ImageDraw = _require_pillow()
    paths = {
        "clean": root / "clean.png",
        "blank": root / "blank.png",
        "corrupt": root / "corrupt.png",
        "seam": root / "seam.png",
        "overlay": root / "overlay.png",
        "watermark": root / "watermark.png",
        "denseText": root / "dense-text.png",
        "noise": root / "noise.png",
        "reference": root / "reference.png",
        "referenceSimilar": root / "reference-similar.png",
        "referenceMismatch": root / "reference-mismatch.png",
    }
    _write_gradient(paths["clean"], Image, ImageDraw)
    _write_gradient(paths["reference"], Image, ImageDraw)
    _write_gradient(paths["referenceSimilar"], Image, ImageDraw)
    with Image.open(paths["referenceSimilar"]) as image:
        similar = image.convert("RGB")
    similar_draw = ImageDraw.Draw(similar)
    similar_draw.rectangle([10, 10, 24, 24], fill=(58, 124, 194))
    similar.save(paths["referenceSimilar"])
    Image.new("RGB", (80, 80), "white").save(paths["blank"])
    paths["corrupt"].write_bytes(b"\x89PNG\r\n\x1a\nbroken")
    seam_image = Image.new("RGB", (96, 96), "black")
    seam_draw = ImageDraw.Draw(seam_image)
    seam_draw.rectangle([48, 0, 95, 95], fill="white")
    seam_image.save(paths["seam"])
    overlay_image = Image.new("RGBA", (96, 96), (40, 90, 160, 255))
    overlay_draw = ImageDraw.Draw(overlay_image)
    overlay_draw.rectangle([20, 20, 76, 76], fill=(255, 255, 255, 80))
    overlay_image.save(paths["overlay"])
    watermark_image = Image.new("RGBA", (160, 120), (80, 150, 210, 255))
    watermark_draw = ImageDraw.Draw(watermark_image)
    for y in range(0, 120, 18):
        watermark_draw.text((8, y), "MARK MARK", fill=(255, 255, 255, 80))
    watermark_image.save(paths["watermark"])
    dense_text = Image.new("RGB", (240, 160), "white")
    dense_text_draw = ImageDraw.Draw(dense_text)
    for y in range(8, 145, 14):
        dense_text_draw.text((8, y), "WMWM 1234 ABCD ####", fill="black")
    dense_text.save(paths["denseText"])
    noise = Image.new("RGB", (96, 96), "white")
    noise_pixels = noise.load()
    for y in range(96):
        for x in range(96):
            noise_pixels[x, y] = (0, 0, 0) if (x * y + x + y) % 3 == 0 else (255, 255, 255)
    noise.save(paths["noise"])
    mismatch = Image.new("RGB", (96, 96), (25, 170, 75))
    mismatch_draw = ImageDraw.Draw(mismatch)
    for x in range(0, 96, 8):
        mismatch_draw.line([(x, 0), (95 - x, 95)], fill=(230, 20, 120), width=3)
    for y in range(0, 96, 12):
        mismatch_draw.rectangle([0, y, 95, min(95, y + 4)], fill=(20, 30, 220))
    mismatch.save(paths["referenceMismatch"])
    return paths


def _image_job_projection_smoke(image_path: Path, workspace: Path) -> dict[str, Any]:
    from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

    ledger = reset_run_event_ledger_for_tests(workspace / "runtime-events.db")

    def runner(_task, _progress, _cancel_event):
        return {
            "kind": "image",
            "path": str(image_path),
            "title": "image-structural-quality.png",
            "qualityEvidence": {
                "kind": "image",
                "status": "fail",
                "checks": [{"id": "private prompt", "status": "fail", "detail": str(image_path)}],
            },
        }

    status = ImageJobService(ledger).start(
        request_id="req-v024-image-structural-quality",
        session_id="session-v024-image-structural-quality",
        job_id="image-job-v024-structural-quality",
        tasks=[{"task_id": "task-1", "image_url": str(image_path)}],
        runner=runner,
        synchronous=True,
    )
    projection = RuntimeProjectionService(ledger).request_projection("req-v024-image-structural-quality")
    artifact = (((projection.get("image_jobs") or [{}])[0]).get("artifacts") or [{}])[0]
    evidence = artifact.get("qualityEvidence") if isinstance(artifact, dict) else {}
    return {
        "status": status.get("status"),
        "artifactEvidenceStatus": evidence.get("status") if isinstance(evidence, dict) else "",
        "artifactEvidenceKind": evidence.get("kind") if isinstance(evidence, dict) else "",
        "hasDecodeGate": isinstance(evidence, dict) and "decode-valid" in (evidence.get("qualityGates") or []),
        "hasReferenceGate": isinstance(evidence, dict) and "reference-fidelity" in (evidence.get("qualityGates") or []),
        "projectionEventCount": len(projection.get("events") or []),
    }


def _image_job_retry_smoke(blank_path: Path, clean_path: Path, workspace: Path) -> dict[str, Any]:
    from agent.protocol import ImageJobService, RuntimeProjectionService, reset_run_event_ledger_for_tests

    ledger = reset_run_event_ledger_for_tests(workspace / "runtime-events-retry.db")
    calls: list[dict[str, Any]] = []

    def runner(task, _progress, _cancel_event):
        calls.append(dict(task))
        path = blank_path if len(calls) == 1 else clean_path
        return {
            "kind": "image",
            "path": str(path),
            "title": f"retry-attempt-{len(calls)}.png",
        }

    status = ImageJobService(ledger).start(
        request_id="req-v024-image-finalization-retry",
        session_id="session-v024-image-finalization-retry",
        job_id="image-job-v024-finalization-retry",
        tasks=[{"task_id": "task-1", "quality_retry_max": 1}],
        runner=runner,
        synchronous=True,
    )
    projection = RuntimeProjectionService(ledger).request_projection("req-v024-image-finalization-retry")
    artifact = (((projection.get("image_jobs") or [{}])[0]).get("artifacts") or [{}])[0]
    evidence = artifact.get("qualityEvidence") if isinstance(artifact, dict) else {}
    summary = (((evidence or {}).get("imageAnalysis") or {}).get("summary") or {}) if isinstance(evidence, dict) else {}
    events = projection.get("events") or []
    return {
        "status": status.get("status"),
        "runnerCallCount": len(calls),
        "retryAttemptTagged": len(calls) >= 2 and calls[1].get("_quality_retry_attempt") == 1,
        "retryProgressEvent": any(
            isinstance(event, dict)
            and event.get("event_type") == "image_job.progress"
            and ((event.get("payload") or {}).get("status") == "retry")
            for event in events
        ),
        "artifactCount": ((projection.get("image_jobs") or [{}])[0]).get("artifact_count"),
        "finalArtifactTitle": artifact.get("title") if isinstance(artifact, dict) else "",
        "finalizationStatus": summary.get("finalizationStatus"),
        "retryCount": summary.get("retryCount"),
    }


def run_smoke() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        paths = _fixtures(root)
        evidence = {name: build_image_quality_evidence(path) for name, path in paths.items()}
        reference_match = build_image_quality_evidence(paths["clean"], reference_images=[paths["reference"]])
        reference_similar = build_image_quality_evidence(paths["referenceSimilar"], reference_images=[paths["reference"]])
        reference_mismatch = build_image_quality_evidence(
            paths["referenceMismatch"],
            reference_images=[paths["reference"]],
        )
        reference_remote = build_image_quality_evidence(
            paths["clean"],
            reference_images=["https://example.invalid/private/ref.png?token=secret"],
        )
        reference_data = build_image_quality_evidence(
            paths["clean"],
            reference_images=["data:image/png;base64,PRIVATEINLINEPAYLOAD"],
        )
        reference_none = build_image_quality_evidence(paths["clean"])
        job_probe = _image_job_projection_smoke(paths["clean"], root)
        retry_probe = _image_job_retry_smoke(paths["blank"], paths["clean"], root)
        serialized = json.dumps(
            {
                "evidence": evidence,
                "reference": {
                    "match": reference_match,
                    "similar": reference_similar,
                    "mismatch": reference_mismatch,
                    "remote": reference_remote,
                    "data": reference_data,
                    "none": reference_none,
                },
                "job": job_probe,
                "retry": retry_probe,
            },
            ensure_ascii=False,
        )
        leaks = [
            item
            for item in [
                str(root),
                "private prompt",
                "sk-private",
                "provider_raw_response",
                "token=secret",
                "example.invalid",
                "PRIVATEINLINEPAYLOAD",
            ]
            if item and item in serialized
        ]
        checks = {
            "cleanPass": evidence["clean"].get("status") == "pass",
            "blankDetected": _status(evidence["blank"], "non-blank") == "fail",
            "corruptDetected": _status(evidence["corrupt"], "decode-valid") == "fail",
            "seamDetected": _status(evidence["seam"], "seam-check") == "fail",
            "overlayDetected": _status(evidence["overlay"], "overlay-ghosting-check") == "warn",
            "watermarkDetected": _status(evidence["watermark"], "watermark-check") == "warn",
            "textGlyphDetected": _status(evidence["denseText"], "text-glyph-check") == "warn",
            "anomalyDetected": _status(evidence["noise"], "anomaly-check") == "warn",
            "referenceMatchDetected": _status(reference_match, "reference-fidelity") == "pass",
            "referenceSimilarDetected": _status(reference_similar, "reference-fidelity") == "pass",
            "referenceMismatchDetected": _status(reference_mismatch, "reference-fidelity") == "warn",
            "referenceRemoteSkipped": _status(reference_remote, "reference-fidelity") == "skipped",
            "referenceDataSkipped": _status(reference_data, "reference-fidelity") == "skipped",
            "referenceNoReferenceSkipped": _status(reference_none, "reference-fidelity") == "skipped",
            "jobProjectionEvidence": job_probe["artifactEvidenceStatus"] == "pass"
            and job_probe["artifactEvidenceKind"] == "image"
            and job_probe["hasDecodeGate"]
            and job_probe["hasReferenceGate"],
            "finalizationRetryDetected": retry_probe["status"] == "completed"
            and retry_probe["runnerCallCount"] == 2
            and retry_probe["retryAttemptTagged"]
            and retry_probe["retryProgressEvent"]
            and retry_probe["artifactCount"] == 1
            and retry_probe["finalArtifactTitle"] == "retry-attempt-2.png"
            and retry_probe["finalizationStatus"] == "final"
            and retry_probe["retryCount"] == 1,
            "privacyLeaksAbsent": not leaks,
        }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "cleanStatus": evidence["clean"].get("status"),
        "blankStatus": evidence["blank"].get("status"),
        "corruptStatus": evidence["corrupt"].get("status"),
        "seamStatus": evidence["seam"].get("status"),
        "overlayStatus": evidence["overlay"].get("status"),
        "watermarkStatus": evidence["watermark"].get("status"),
        "denseTextStatus": evidence["denseText"].get("status"),
        "noiseStatus": evidence["noise"].get("status"),
        "referenceMatchStatus": reference_match.get("status"),
        "referenceSimilarStatus": reference_similar.get("status"),
        "referenceMismatchStatus": reference_mismatch.get("status"),
        "referenceRemoteStatus": reference_remote.get("status"),
        "referenceDataStatus": reference_data.get("status"),
        "referenceNoReferenceStatus": reference_none.get("status"),
        "jobProbe": job_probe,
        "retryProbe": retry_probe,
        "leakCount": len(leaks),
        "leaks": leaks,
        "redacted": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke R24-10 image structural quality evidence.")
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
