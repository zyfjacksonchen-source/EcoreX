"""Content-safe structural QA helpers for generated image artifacts."""

from __future__ import annotations

import hashlib
import hmac
import math
import os
from copy import deepcopy
from collections.abc import Iterable as IterableABC
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCHEMA_VERSION = 1
_EVIDENCE_HMAC_KEY = b"ecorex-image-quality-evidence-v1"
IMAGE_QUALITY_GATES = [
    "decode-valid",
    "artifact-integrity",
    "non-blank",
    "seam-check",
    "overlay-ghosting-check",
    "text-glyph-check",
    "watermark-check",
    "subject-structure-check",
    "anomaly-check",
    "reference-fidelity",
    "visual-inspection",
]
_SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_MAX_SAMPLE_EDGE = 256
_BLANK_STDDEV_THRESHOLD = 3.0
_BLANK_UNIQUE_BUCKET_THRESHOLD = 3
_SEAM_RATIO_WARN_THRESHOLD = 3.5
_SEAM_RATIO_FAIL_THRESHOLD = 7.0
_EDGE_DENSITY_HIGH_THRESHOLD = 0.42
_ALPHA_MIX_WARN_THRESHOLD = 0.08
_REFERENCE_SIMILARITY_WARN_THRESHOLD = 0.78
IMAGE_FINALIZATION_POLICY_VERSION = "v0.2.4-image-finalization-v1"
_IMAGE_RETRY_FAIL_GATES = {
    "decode-valid",
    "artifact-integrity",
    "non-blank",
    "seam-check",
}
_IMAGE_RETRY_WARN_GATES = {
    "seam-check",
    "overlay-ghosting-check",
    "text-glyph-check",
    "watermark-check",
    "subject-structure-check",
    "anomaly-check",
    "reference-fidelity",
}


class ImageQualityRuntimeError(RuntimeError):
    """Raised when an image artifact cannot be inspected."""


def build_image_quality_evidence(path_or_artifact: Any, reference_images: Any = None) -> Dict[str, Any]:
    """Return a redacted structural QA evidence payload for a generated image."""

    path = _artifact_path(path_or_artifact)
    analysis = analyze_image_quality(path)
    reference_summary = compare_image_reference_quality(
        path,
        _reference_sources(path_or_artifact, reference_images),
    )
    if isinstance(analysis.get("summary"), dict):
        summary = analysis["summary"]
        summary.update(reference_summary)
        if reference_summary.get("referenceMismatchRisk") and summary.get("status") == "pass":
            summary["status"] = "warn"
    checks = _quality_checks_from_analysis(analysis)
    status = _status_from_checks(checks)
    return {
        "schemaVersion": "v0.2.4",
        "kind": "image",
        "sourceRef": _hash_ref(path),
        "qualityGates": list(IMAGE_QUALITY_GATES),
        "checks": checks,
        "missingQualityGates": [],
        "status": status,
        "imageAnalysis": analysis,
        "redacted": True,
    }


def build_image_finalization_decision(
    evidence: Any,
    *,
    retry_count: Any = 0,
    max_retries: Any = 0,
) -> Dict[str, Any]:
    """Return a content-safe decision for whether generated image output is final."""

    safe_retry_count = _nonnegative_int(retry_count)
    safe_max_retries = _nonnegative_int(max_retries)
    gate, gate_status = _retry_gate_from_evidence(evidence)
    retry_recommended = bool(gate)
    can_retry = retry_recommended and safe_retry_count < safe_max_retries
    status = "retry" if can_retry else "needs_review" if retry_recommended else "final"
    return {
        "schemaVersion": "v0.2.4",
        "kind": "image",
        "policyVersion": IMAGE_FINALIZATION_POLICY_VERSION,
        "status": status,
        "finalized": status == "final",
        "retryRecommended": retry_recommended,
        "retryGate": gate or "none",
        "retryGateStatus": gate_status or "none",
        "retryCount": safe_retry_count,
        "maxRetries": safe_max_retries,
        "attemptCount": safe_retry_count + 1,
        "redacted": True,
    }


def attach_image_finalization_evidence(evidence: Any, decision: Any) -> Dict[str, Any]:
    """Attach finalization metadata to image evidence without exposing content."""

    if not isinstance(evidence, dict):
        return {}
    safe_decision = dict(decision or {})
    annotated = deepcopy(evidence)
    annotated["redacted"] = True
    analysis = annotated.get("imageAnalysis")
    if isinstance(analysis, dict):
        summary = analysis.get("summary")
        if isinstance(summary, dict):
            summary.update({
                "finalizationStatus": _safe_finalization_status(safe_decision.get("status")),
                "finalized": bool(safe_decision.get("finalized")),
                "retryRecommended": bool(safe_decision.get("retryRecommended")),
                "retryCount": _nonnegative_int(safe_decision.get("retryCount")),
                "maxRetries": _nonnegative_int(safe_decision.get("maxRetries")),
                "retryGate": _safe_quality_gate(safe_decision.get("retryGate")) or "none",
            })
    checks = annotated.get("checks")
    if isinstance(checks, list):
        _annotate_visual_inspection_check(checks, safe_decision)
    return annotated


def aggregate_image_finalization_decisions(decisions: Iterable[Any]) -> Dict[str, Any]:
    """Aggregate per-image finalization decisions into one safe result summary."""

    items = [dict(item) for item in decisions if isinstance(item, dict)]
    if not items:
        return {
            "schemaVersion": "v0.2.4",
            "kind": "image",
            "policyVersion": IMAGE_FINALIZATION_POLICY_VERSION,
            "status": "unknown",
            "finalized": False,
            "retryRecommended": False,
            "retryGate": "none",
            "retryGateStatus": "none",
            "retryCount": 0,
            "maxRetries": 0,
            "attemptCount": 0,
            "redacted": True,
        }
    status_order = {"retry": 3, "needs_review": 2, "unknown": 1, "final": 0}
    selected = max(items, key=lambda item: status_order.get(str(item.get("status") or "unknown"), 1))
    return {
        "schemaVersion": "v0.2.4",
        "kind": "image",
        "policyVersion": IMAGE_FINALIZATION_POLICY_VERSION,
        "status": _safe_finalization_status(selected.get("status")),
        "finalized": all(bool(item.get("finalized")) for item in items),
        "retryRecommended": any(bool(item.get("retryRecommended")) for item in items),
        "retryGate": _safe_quality_gate(selected.get("retryGate")) or "none",
        "retryGateStatus": _safe_check_status(selected.get("retryGateStatus")) or "none",
        "retryCount": max(_nonnegative_int(item.get("retryCount")) for item in items),
        "maxRetries": max(_nonnegative_int(item.get("maxRetries")) for item in items),
        "attemptCount": max(_nonnegative_int(item.get("attemptCount")) for item in items),
        "redacted": True,
    }


def compare_image_reference_quality(path_or_artifact: Any, reference_images: Any = None) -> Dict[str, Any]:
    """Compare a generated image with local reference images without exposing content."""

    candidate_path = _artifact_path(path_or_artifact)
    sources = _coerce_reference_sources(reference_images)
    summary: Dict[str, Any] = {
        "referenceCount": len(sources),
        "localReferenceCount": 0,
        "remoteReferenceCount": 0,
        "referenceComparedCount": 0,
        "referenceDecodeFailureCount": 0,
        "referenceSimilarityPct": 0,
        "referenceMismatchRisk": False,
        "referenceAspectMismatchRisk": False,
    }
    if not sources:
        return summary

    local_paths: List[str] = []
    for source in sources[:8]:
        if _is_remote_or_inline(source):
            summary["remoteReferenceCount"] += 1
            continue
        reference_path = _local_existing_path(source)
        if reference_path:
            local_paths.append(reference_path)
        else:
            summary["referenceDecodeFailureCount"] += 1
    summary["localReferenceCount"] = len(local_paths)
    if not local_paths:
        return summary

    candidate_signature = _image_similarity_signature(candidate_path)
    if not candidate_signature:
        summary["referenceDecodeFailureCount"] += len(local_paths)
        return summary

    best_score = 0.0
    best_aspect_score = 0.0
    best_ref = ""
    compared_count = 0
    decode_failures = int(summary["referenceDecodeFailureCount"])
    for reference_path in local_paths:
        reference_signature = _image_similarity_signature(reference_path)
        if not reference_signature:
            decode_failures += 1
            continue
        compared_count += 1
        score, aspect_score = _signature_similarity(candidate_signature, reference_signature)
        if score >= best_score:
            best_score = score
            best_aspect_score = aspect_score
            best_ref = reference_path

    summary["referenceComparedCount"] = compared_count
    summary["referenceDecodeFailureCount"] = decode_failures
    if compared_count <= 0:
        return summary
    summary["referenceSimilarityPct"] = int(round(max(0.0, min(best_score, 1.0)) * 100))
    summary["referenceMismatchRisk"] = best_score < _REFERENCE_SIMILARITY_WARN_THRESHOLD
    summary["referenceAspectMismatchRisk"] = best_aspect_score < 0.55
    if best_ref:
        summary["referenceRef"] = _hash_ref(best_ref)
    return summary


def analyze_image_quality(path_or_artifact: Any) -> Dict[str, Any]:
    """Inspect structural image risks without emitting raw content, paths, or text."""

    path = _artifact_path(path_or_artifact)
    source_ref = _hash_ref(path)
    summary: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "image",
        "sourceRef": source_ref,
        "status": "unknown",
    }
    if not path:
        summary.update({
            "decodeValid": False,
            "corruptRisk": True,
            "decodeError": "missing",
            "status": "fail",
        })
        return {"schemaVersion": SCHEMA_VERSION, "kind": "image", "sourceRef": source_ref, "summary": summary}

    target = Path(path)
    suffix = target.suffix.lower()
    try:
        size_bytes = target.stat().st_size
    except OSError:
        size_bytes = 0
    summary["sizeBytes"] = max(0, int(size_bytes))
    summary["extension"] = suffix if suffix in _SUPPORTED_EXTENSIONS else "unknown"
    if size_bytes <= 0:
        summary.update({
            "decodeValid": False,
            "corruptRisk": True,
            "decodeError": "empty",
            "status": "fail",
        })
        return {"schemaVersion": SCHEMA_VERSION, "kind": "image", "sourceRef": source_ref, "summary": summary}

    try:
        from PIL import Image, ImageStat
    except Exception:
        summary.update({
            "decodeValid": False,
            "corruptRisk": True,
            "decodeError": "pillow-missing",
            "status": "fail",
        })
        return {"schemaVersion": SCHEMA_VERSION, "kind": "image", "sourceRef": source_ref, "summary": summary}

    try:
        with Image.open(target) as opened:
            image_format = str(opened.format or "").lower()
            width, height = opened.size
            frame_count = int(getattr(opened, "n_frames", 1) or 1)
            sample = _analysis_sample(opened)
    except Exception as exc:
        summary.update({
            "decodeValid": False,
            "corruptRisk": True,
            "decodeError": _safe_decode_error(exc),
            "status": "fail",
        })
        return {"schemaVersion": SCHEMA_VERSION, "kind": "image", "sourceRef": source_ref, "summary": summary}

    width = max(0, int(width or 0))
    height = max(0, int(height or 0))
    summary.update({
        "decodeValid": width > 0 and height > 0,
        "format": image_format if image_format in {"png", "jpeg", "jpg", "webp"} else "unknown",
        "width": width,
        "height": height,
        "pixelCount": width * height,
        "frameCount": max(1, frame_count),
    })
    if width <= 0 or height <= 0:
        summary.update({"corruptRisk": True, "status": "fail"})
        return {"schemaVersion": SCHEMA_VERSION, "kind": "image", "sourceRef": source_ref, "summary": summary}

    gray = sample.convert("L")
    stat = ImageStat.Stat(gray)
    width_s, height_s = gray.size
    pixels = gray.tobytes()
    luminance_stddev = float(stat.stddev[0]) if stat.stddev else 0.0
    unique_bucket_count = len({int(pixel) // 16 for pixel in pixels})
    alpha_stddev, transparent_ratio, translucent_ratio = _alpha_sample_metrics(sample, ImageStat)
    edge_density, mean_gradient = _edge_density(gray)
    seam_ratio, seam_axis = _center_seam_ratio(gray, mean_gradient)
    border_mismatch_ratio = _border_mismatch_ratio(gray, mean_gradient)
    duplicate_tile_score = _duplicate_tile_score(gray)
    vision_summary = _vision_risk_summary(
        gray,
        edge_density=edge_density,
        mean_gradient=mean_gradient,
        translucent_ratio=translucent_ratio,
        duplicate_tile_score=duplicate_tile_score,
        border_mismatch_ratio=border_mismatch_ratio,
    )

    blank_risk = luminance_stddev <= _BLANK_STDDEV_THRESHOLD and unique_bucket_count <= _BLANK_UNIQUE_BUCKET_THRESHOLD
    corrupt_risk = not summary["decodeValid"] or suffix not in _SUPPORTED_EXTENSIONS
    seam_risk = seam_ratio >= _SEAM_RATIO_FAIL_THRESHOLD
    seam_warning = seam_ratio >= _SEAM_RATIO_WARN_THRESHOLD
    overlay_risk = (
        translucent_ratio >= _ALPHA_MIX_WARN_THRESHOLD
        or (edge_density >= _EDGE_DENSITY_HIGH_THRESHOLD and duplicate_tile_score >= 0.5)
        or (alpha_stddev > 20.0 and translucent_ratio > 0.02)
    )
    glyph_fragment_risk = bool(vision_summary.get("glyphFragmentRisk"))
    watermark_risk = bool(vision_summary.get("watermarkRisk"))
    subject_structure_risk = bool(vision_summary.get("subjectStructureRisk"))
    anomaly_risk = bool(vision_summary.get("anomalyRisk"))

    summary.update({
        "sampleWidth": width_s,
        "sampleHeight": height_s,
        "luminanceStdDev": round(luminance_stddev, 3),
        "uniqueColorBucketCount": unique_bucket_count,
        "blankRisk": bool(blank_risk),
        "corruptRisk": bool(corrupt_risk),
        "transparentRatioPct": int(round(transparent_ratio * 100)),
        "translucentRatioPct": int(round(translucent_ratio * 100)),
        "edgeDensityPct": int(round(edge_density * 100)),
        "meanGradient": round(mean_gradient, 3),
        "seamRisk": bool(seam_risk),
        "seamWarning": bool(seam_warning),
        "seamRatio": round(seam_ratio, 3),
        "seamAxis": seam_axis,
        "borderMismatchRatio": round(border_mismatch_ratio, 3),
        "overlayGhostingRisk": bool(overlay_risk),
        "duplicateTileScorePct": int(round(duplicate_tile_score * 100)),
        **vision_summary,
        "status": (
            "fail"
            if blank_risk or corrupt_risk or seam_risk
            else "warn"
            if seam_warning or overlay_risk or glyph_fragment_risk or watermark_risk or subject_structure_risk or anomaly_risk
            else "pass"
        ),
    })
    return {"schemaVersion": SCHEMA_VERSION, "kind": "image", "sourceRef": source_ref, "summary": summary}


def _quality_checks_from_analysis(analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    summary = analysis.get("summary") if isinstance(analysis.get("summary"), dict) else {}
    decode_valid = summary.get("decodeValid") is True
    corrupt_risk = summary.get("corruptRisk") is True
    blank_risk = summary.get("blankRisk") is True
    seam_risk = summary.get("seamRisk") is True
    seam_warning = summary.get("seamWarning") is True
    overlay_risk = summary.get("overlayGhostingRisk") is True
    glyph_fragment_risk = summary.get("glyphFragmentRisk") is True
    watermark_risk = summary.get("watermarkRisk") is True
    subject_structure_risk = summary.get("subjectStructureRisk") is True
    anomaly_risk = summary.get("anomalyRisk") is True
    reference_count = int(summary.get("referenceCount") or 0)
    reference_compared = int(summary.get("referenceComparedCount") or 0)
    reference_mismatch_risk = summary.get("referenceMismatchRisk") is True
    checks = [
        {
            "id": "decode-valid",
            "status": "pass" if decode_valid else "fail",
            "detail": _detail(decode_valid=int(bool(decode_valid)), decode_error=summary.get("decodeError") or "none"),
        },
        {
            "id": "artifact-integrity",
            "status": "fail" if corrupt_risk else "pass",
            "detail": _detail(corrupt_risk=int(bool(corrupt_risk)), size_bytes=summary.get("sizeBytes", 0)),
        },
        {
            "id": "non-blank",
            "status": "fail" if blank_risk else "pass",
            "detail": _detail(blank_risk=int(bool(blank_risk)), unique_color_buckets=summary.get("uniqueColorBucketCount", 0)),
        },
        {
            "id": "seam-check",
            "status": "fail" if seam_risk else "warn" if seam_warning else "pass",
            "detail": _detail(seam_risk=int(bool(seam_risk or seam_warning)), seam_axis=summary.get("seamAxis") or "none"),
        },
        {
            "id": "overlay-ghosting-check",
            "status": "warn" if overlay_risk else "pass",
            "detail": _detail(overlay_risk=int(bool(overlay_risk)), translucent_pct=summary.get("translucentRatioPct", 0)),
        },
        {
            "id": "text-glyph-check",
            "status": "warn" if glyph_fragment_risk else "pass",
            "detail": _detail(
                glyph_issues=int(bool(glyph_fragment_risk)),
                text_like_regions=summary.get("textLikeRegionCount", 0),
                glyph_fragments=summary.get("smallEdgeComponentCount", 0),
            ),
        },
        {
            "id": "watermark-check",
            "status": "warn" if watermark_risk else "pass",
            "detail": _detail(watermark_risk=int(bool(watermark_risk)), text_density=summary.get("textDensityPct", 0)),
        },
        {
            "id": "subject-structure-check",
            "status": "warn" if subject_structure_risk else "skipped",
            "detail": _detail(
                subject_risk=int(bool(subject_structure_risk)),
                saliency_pct=summary.get("saliencyCoveragePct", 0),
                subject_review="pending" if subject_structure_risk else "skipped",
            ),
        },
        {
            "id": "anomaly-check",
            "status": "warn" if anomaly_risk else "pass",
            "detail": _detail(anomaly_risk=int(bool(anomaly_risk)), edge_density=summary.get("edgeDensityPct", 0)),
        },
        {
            "id": "reference-fidelity",
            "status": "warn" if reference_mismatch_risk else "pass" if reference_compared > 0 else "skipped",
            "detail": _detail(
                reference_status="pending" if reference_mismatch_risk else "pass" if reference_compared > 0 else "skipped",
                reference_count=reference_count,
                references_compared=reference_compared,
                reference_similarity=summary.get("referenceSimilarityPct", 0),
                reference_mismatch=int(bool(reference_mismatch_risk)),
                remote_references=summary.get("remoteReferenceCount", 0),
            ),
        },
        {
            "id": "visual-inspection",
            "status": "pass" if summary.get("status") == "pass" else "warn",
            "detail": _detail(manual_visual_review="pending" if summary.get("status") != "pass" else "pass"),
        },
    ]
    return checks


def _status_from_checks(checks: Iterable[Dict[str, Any]]) -> str:
    statuses = {str(check.get("status") or "").lower() for check in checks}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    if "pending" in statuses:
        return "pending"
    return "pass"


def _retry_gate_from_evidence(evidence: Any) -> Tuple[str, str]:
    if not isinstance(evidence, dict):
        return "", ""
    checks = evidence.get("checks") if isinstance(evidence.get("checks"), list) else []
    normalized: List[Tuple[str, str]] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        gate = _safe_quality_gate(check.get("id") or check.get("gate"))
        status = _safe_check_status(check.get("status"))
        if gate and status:
            normalized.append((gate, status))
    for gate, status in normalized:
        if gate in _IMAGE_RETRY_FAIL_GATES and status == "fail":
            return gate, status
    for gate, status in normalized:
        if gate in _IMAGE_RETRY_WARN_GATES and status == "warn":
            return gate, status
    return "", ""


def _annotate_visual_inspection_check(checks: List[Any], decision: Dict[str, Any]) -> None:
    status = _safe_finalization_status(decision.get("status"))
    detail = _detail(
        manual_visual_review="pass" if status == "final" else "pending",
        retry_count=_nonnegative_int(decision.get("retryCount")),
        max_retries=_nonnegative_int(decision.get("maxRetries")),
        retry_recommended=int(bool(decision.get("retryRecommended"))),
        finalized=int(status == "final"),
        retry_gate=_safe_quality_gate(decision.get("retryGate")) or "none",
    )
    for check in checks:
        if isinstance(check, dict) and str(check.get("id") or "").strip().lower() == "visual-inspection":
            check["detail"] = detail
            if status == "final":
                check["status"] = "pass"
            elif str(check.get("status") or "").strip().lower() == "pass":
                check["status"] = "warn"
            return
    checks.append({"id": "visual-inspection", "status": "pass" if status == "final" else "warn", "detail": detail})


def _safe_quality_gate(value: Any) -> str:
    gate = str(value or "").strip().lower()
    return gate if gate in IMAGE_QUALITY_GATES else ""


def _safe_check_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in {"pass", "fail", "warn", "pending", "skipped"} else ""


def _safe_finalization_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in {"final", "retry", "needs_review", "unknown"} else "unknown"


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _artifact_path(value: Any) -> str:
    if isinstance(value, (str, os.PathLike)):
        return str(value)
    if isinstance(value, dict):
        for key in ("path", "url", "relativePath", "relative_path"):
            item = value.get(key)
            if isinstance(item, str) and item.strip() and not _is_remote_or_inline(item):
                return item.strip()
    return ""


def _reference_sources(path_or_artifact: Any, reference_images: Any = None) -> List[str]:
    sources = _coerce_reference_sources(reference_images)
    if sources:
        return sources
    if isinstance(path_or_artifact, dict):
        for key in (
            "reference_image",
            "reference_images",
            "referenceImage",
            "referenceImages",
            "image_url",
            "image_urls",
            "image_path",
            "image_paths",
            "input_image",
            "input_images",
        ):
            sources.extend(_coerce_reference_sources(path_or_artifact.get(key)))
    return sources


def _coerce_reference_sources(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (str, os.PathLike)):
        text = str(value).strip()
        return [text] if text else []
    if isinstance(value, dict):
        path = _artifact_path(value)
        return [path] if path else []
    if isinstance(value, IterableABC):
        sources: List[str] = []
        for item in list(value)[:16]:
            sources.extend(_coerce_reference_sources(item))
        return sources
    return []


def _local_existing_path(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or _is_remote_or_inline(raw):
        return ""
    if raw.lower().startswith("file://"):
        raw = raw[7:]
    try:
        candidate = Path(os.path.expanduser(raw)).resolve()
    except (OSError, RuntimeError, ValueError):
        return ""
    try:
        if candidate.exists() and candidate.is_file() and candidate.suffix.lower() in _SUPPORTED_EXTENSIONS:
            return str(candidate)
    except OSError:
        return ""
    return ""


def _is_remote_or_inline(value: str) -> bool:
    lowered = str(value or "").strip().lower()
    return lowered.startswith(("http://", "https://", "data:"))


def _hash_ref(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = "missing"
    digest = hmac.new(_EVIDENCE_HMAC_KEY, raw.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()
    return f"hmac:{digest[:16]}"


def _analysis_sample(opened: Any) -> Any:
    _apply_decoder_draft(opened, (_MAX_SAMPLE_EDGE, _MAX_SAMPLE_EDGE))
    source = opened
    if _image_has_alpha(opened) and str(getattr(opened, "mode", "")).upper() not in {"RGBA", "LA"}:
        source = opened.convert("RGBA")
    sample = _resize_for_sampling(source)
    if sample is source:
        sample = sample.copy()
    return sample


def _apply_decoder_draft(image: Any, size: Tuple[int, int]) -> None:
    draft = getattr(image, "draft", None)
    if not callable(draft):
        return
    try:
        draft("RGB", size)
    except Exception:
        return


def _image_has_alpha(image: Any) -> bool:
    mode = str(getattr(image, "mode", "") or "").upper()
    if mode in {"RGBA", "LA", "PA"}:
        return True
    try:
        return "transparency" in (getattr(image, "info", {}) or {})
    except Exception:
        return False


def _alpha_sample_metrics(sample: Any, image_stat: Any) -> Tuple[float, float, float]:
    if not _image_has_alpha(sample):
        return 0.0, 0.0, 0.0
    try:
        bands = tuple(getattr(sample, "getbands", lambda: ())())
        alpha = sample.getchannel("A") if "A" in bands else sample.convert("RGBA").getchannel("A")
        alpha_stat = image_stat.Stat(alpha)
        alpha_values = alpha.tobytes()
    except Exception:
        return 0.0, 0.0, 0.0
    sample_pixels = max(1, len(alpha_values))
    transparent_count = sum(1 for value in alpha_values if value <= 8)
    translucent_count = sum(1 for value in alpha_values if 8 < value < 248)
    alpha_stddev = float(alpha_stat.stddev[0]) if alpha_stat.stddev else 0.0
    return alpha_stddev, transparent_count / sample_pixels, translucent_count / sample_pixels


def _resize_for_sampling(image: Any) -> Any:
    width, height = image.size
    max_edge = max(width, height)
    if max_edge <= _MAX_SAMPLE_EDGE:
        return image
    scale = _MAX_SAMPLE_EDGE / max_edge
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return image.resize(new_size)


def _image_similarity_signature(path_or_artifact: Any) -> Optional[Dict[str, Any]]:
    path = _artifact_path(path_or_artifact)
    if not path:
        return None
    try:
        from PIL import Image, ImageStat
    except Exception:
        return None
    try:
        with Image.open(Path(path)) as opened:
            width, height = opened.size
            _apply_decoder_draft(opened, (64, 64))
            sample = opened.resize((32, 32))
            rgb_sample = sample.convert("RGB")
            gray = rgb_sample.convert("L")
    except Exception:
        return None

    pixels = list(gray.tobytes())
    histogram = [0] * 16
    for pixel in pixels:
        histogram[min(15, int(pixel) // 16)] += 1
    stat = ImageStat.Stat(rgb_sample)
    mean_rgb = [float(item) for item in (stat.mean or [0.0, 0.0, 0.0])[:3]]
    edge_density, _mean_gradient = _edge_density(gray)
    return {
        "width": max(1, int(width or 1)),
        "height": max(1, int(height or 1)),
        "pixels": pixels,
        "histogram": histogram,
        "meanRgb": mean_rgb,
        "edgeDensity": edge_density,
    }


def _signature_similarity(candidate: Dict[str, Any], reference: Dict[str, Any]) -> Tuple[float, float]:
    candidate_pixels = candidate.get("pixels") or []
    reference_pixels = reference.get("pixels") or []
    if not candidate_pixels or len(candidate_pixels) != len(reference_pixels):
        thumb_score = 0.0
    else:
        diff = sum(abs(int(a) - int(b)) for a, b in zip(candidate_pixels, reference_pixels))
        thumb_score = 1.0 - diff / max(1, len(candidate_pixels) * 255)

    candidate_hist = candidate.get("histogram") or []
    reference_hist = reference.get("histogram") or []
    hist_total = max(1, min(sum(candidate_hist), sum(reference_hist)))
    hist_score = sum(min(int(a), int(b)) for a, b in zip(candidate_hist, reference_hist)) / hist_total

    candidate_rgb = candidate.get("meanRgb") or []
    reference_rgb = reference.get("meanRgb") or []
    if len(candidate_rgb) == 3 and len(reference_rgb) == 3:
        color_diff = sum(abs(float(a) - float(b)) for a, b in zip(candidate_rgb, reference_rgb)) / (3 * 255)
        color_score = 1.0 - color_diff
    else:
        color_score = 0.0

    candidate_aspect = float(candidate.get("width") or 1) / max(1.0, float(candidate.get("height") or 1))
    reference_aspect = float(reference.get("width") or 1) / max(1.0, float(reference.get("height") or 1))
    aspect_delta = abs(math.log(max(candidate_aspect, 0.001) / max(reference_aspect, 0.001)))
    aspect_score = max(0.0, 1.0 - min(aspect_delta / 1.25, 1.0))

    edge_delta = abs(float(candidate.get("edgeDensity") or 0.0) - float(reference.get("edgeDensity") or 0.0))
    edge_score = max(0.0, 1.0 - min(edge_delta / 0.75, 1.0))
    score = (
        max(0.0, min(thumb_score, 1.0)) * 0.42
        + max(0.0, min(hist_score, 1.0)) * 0.24
        + max(0.0, min(color_score, 1.0)) * 0.12
        + max(0.0, min(aspect_score, 1.0)) * 0.12
        + max(0.0, min(edge_score, 1.0)) * 0.10
    )
    return max(0.0, min(score, 1.0)), max(0.0, min(aspect_score, 1.0))


def _edge_density(gray: Any) -> Tuple[float, float]:
    width, height = gray.size
    if width < 2 or height < 2:
        return 0.0, 0.0
    pix = gray.load()
    gradients: List[int] = []
    edge_count = 0
    for y in range(height - 1):
        for x in range(width - 1):
            gradient = abs(int(pix[x, y]) - int(pix[x + 1, y])) + abs(int(pix[x, y]) - int(pix[x, y + 1]))
            gradients.append(gradient)
            if gradient >= 48:
                edge_count += 1
    if not gradients:
        return 0.0, 0.0
    return edge_count / len(gradients), sum(gradients) / len(gradients)


def _center_seam_ratio(gray: Any, mean_gradient: float) -> Tuple[float, str]:
    width, height = gray.size
    if width < 4 or height < 4:
        return 0.0, "none"
    pix = gray.load()
    vertical = _mean_abs_line_delta(pix, width // 2 - 1, width // 2, range(height), axis="x")
    horizontal = _mean_abs_line_delta(pix, height // 2 - 1, height // 2, range(width), axis="y")
    baseline = max(1.0, mean_gradient)
    vertical_ratio = vertical / baseline
    horizontal_ratio = horizontal / baseline
    if vertical_ratio >= horizontal_ratio:
        return vertical_ratio, "vertical" if vertical_ratio >= _SEAM_RATIO_WARN_THRESHOLD else "none"
    return horizontal_ratio, "horizontal" if horizontal_ratio >= _SEAM_RATIO_WARN_THRESHOLD else "none"


def _mean_abs_line_delta(pix: Any, a: int, b: int, positions: Iterable[int], *, axis: str) -> float:
    deltas: List[int] = []
    for pos in positions:
        if axis == "x":
            deltas.append(abs(int(pix[a, pos]) - int(pix[b, pos])))
        else:
            deltas.append(abs(int(pix[pos, a]) - int(pix[pos, b])))
    return sum(deltas) / max(1, len(deltas))


def _border_mismatch_ratio(gray: Any, mean_gradient: float) -> float:
    width, height = gray.size
    if width < 2 or height < 2:
        return 0.0
    pix = gray.load()
    left_right = _mean_abs_line_delta(pix, 0, width - 1, range(height), axis="x")
    top_bottom = _mean_abs_line_delta(pix, 0, height - 1, range(width), axis="y")
    return max(left_right, top_bottom) / max(1.0, mean_gradient)


def _duplicate_tile_score(gray: Any) -> float:
    width, height = gray.size
    if width < 16 or height < 16:
        return 0.0
    crops = [
        gray.crop((0, 0, width // 2, height // 2)),
        gray.crop((width // 2, 0, width, height // 2)),
        gray.crop((0, height // 2, width // 2, height)),
        gray.crop((width // 2, height // 2, width, height)),
    ]
    histograms = [crop.resize((16, 16)).histogram() for crop in crops]
    similarities: List[float] = []
    for index, first in enumerate(histograms):
        for second in histograms[index + 1:]:
            diff = sum(abs(a - b) for a, b in zip(first, second))
            total = max(1, sum(first) + sum(second))
            similarities.append(max(0.0, 1.0 - diff / total))
    return max(similarities) if similarities else 0.0


def _vision_risk_summary(
    gray: Any,
    *,
    edge_density: float,
    mean_gradient: float,
    translucent_ratio: float,
    duplicate_tile_score: float,
    border_mismatch_ratio: float,
) -> Dict[str, Any]:
    component_summary = _edge_component_summary(gray)
    sample_pixels = max(1, int(gray.size[0]) * int(gray.size[1]))
    text_edge_pixels = int(component_summary.get("textEdgePixelCount", 0))
    text_density_pct = int(round(text_edge_pixels * 100 / sample_pixels))
    saliency_pct = int(round(edge_density * 100))
    small_components = int(component_summary.get("smallEdgeComponentCount", 0))
    line_like_components = int(component_summary.get("lineLikeComponentCount", 0))
    text_like_regions = int(component_summary.get("textLikeRegionCount", 0))
    glyph_fragment_risk = (
        text_like_regions >= 18
        and small_components >= 24
        and 1 <= text_density_pct <= 45
    )
    watermark_risk = (
        translucent_ratio >= _ALPHA_MIX_WARN_THRESHOLD
        or (line_like_components >= 10 and 1 <= text_density_pct <= 25 and edge_density >= 0.08)
    )
    subject_structure_risk = (
        edge_density >= 0.68
        or (border_mismatch_ratio >= 10.0 and mean_gradient >= 12.0)
    )
    anomaly_risk = (
        edge_density >= 0.74
        or (duplicate_tile_score >= 0.88 and edge_density >= 0.45)
        or (small_components >= 80 and text_density_pct >= 20)
    )
    return {
        "textLikeRegionCount": text_like_regions,
        "smallEdgeComponentCount": small_components,
        "lineLikeComponentCount": line_like_components,
        "textDensityPct": text_density_pct,
        "saliencyCoveragePct": saliency_pct,
        "glyphFragmentRisk": bool(glyph_fragment_risk),
        "watermarkRisk": bool(watermark_risk),
        "subjectStructureRisk": bool(subject_structure_risk),
        "anomalyRisk": bool(anomaly_risk),
    }


def _edge_component_summary(gray: Any) -> Dict[str, int]:
    width, height = gray.size
    if width < 4 or height < 4:
        return {
            "textLikeRegionCount": 0,
            "smallEdgeComponentCount": 0,
            "lineLikeComponentCount": 0,
            "textEdgePixelCount": 0,
        }
    pix = gray.load()
    edge_mask = bytearray(width * height)
    for y in range(height - 1):
        for x in range(width - 1):
            gradient = abs(int(pix[x, y]) - int(pix[x + 1, y])) + abs(int(pix[x, y]) - int(pix[x, y + 1]))
            if gradient >= 52:
                edge_mask[y * width + x] = 1
    visited = bytearray(width * height)
    text_like_regions = 0
    small_components = 0
    line_like_components = 0
    text_edge_pixels = 0
    for index, value in enumerate(edge_mask):
        if not value or visited[index]:
            continue
        stack = [index]
        visited[index] = 1
        count = 0
        min_x = width
        min_y = height
        max_x = 0
        max_y = 0
        while stack:
            current = stack.pop()
            count += 1
            x = current % width
            y = current // width
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)
            for neighbor in (current - 1, current + 1, current - width, current + width):
                if neighbor < 0 or neighbor >= len(edge_mask) or visited[neighbor] or not edge_mask[neighbor]:
                    continue
                nx = neighbor % width
                if abs(nx - x) > 1:
                    continue
                visited[neighbor] = 1
                stack.append(neighbor)
        box_width = max(1, max_x - min_x + 1)
        box_height = max(1, max_y - min_y + 1)
        if count < 3:
            continue
        if count <= 1800 and box_width <= max(12, width * 0.55) and box_height <= max(10, height * 0.18):
            small_components += 1
            text_edge_pixels += count
            aspect = box_width / max(1, box_height)
            if aspect >= 2.5 or (box_width >= 8 and box_height <= 10):
                line_like_components += 1
            if box_width >= 3 and box_height >= 3:
                text_like_regions += 1
    return {
        "textLikeRegionCount": text_like_regions,
        "smallEdgeComponentCount": small_components,
        "lineLikeComponentCount": line_like_components,
        "textEdgePixelCount": text_edge_pixels,
    }


def _safe_decode_error(exc: BaseException) -> str:
    raw = exc.__class__.__name__.strip().lower().replace("_", "-")
    allowed = {
        "decompressionbombwarning",
        "decompressionbomberror",
        "filenotfounderror",
        "oserror",
        "syntaxerror",
        "unidentifiedimageerror",
        "valueerror",
    }
    return raw if raw in allowed else "decode-error"


def _detail(**values: Any) -> str:
    parts: List[str] = []
    for key, value in values.items():
        normalized = key.replace("-", "_")
        if isinstance(value, bool):
            parts.append(f"{normalized}={int(value)}")
        elif isinstance(value, int):
            parts.append(f"{normalized}={max(0, value)}")
        elif isinstance(value, float) and math.isfinite(value):
            parts.append(f"{normalized}={max(0, int(round(value)))}")
        else:
            text = str(value or "").strip().lower().replace("_", "-")
            if text in {
                "empty",
                "fail",
                "horizontal",
                "missing",
                "none",
                "pass",
                "pending",
                "pillow-missing",
                "skipped",
                "unknown",
                "vertical",
            }:
                parts.append(f"{normalized}={text}")
    return "; ".join(parts)
