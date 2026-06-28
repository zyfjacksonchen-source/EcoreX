#!/usr/bin/env python3
"""Measure image visual-analysis speed without storing image contents."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.image_quality_runtime import build_image_quality_evidence  # noqa: E402

_MIN_IMPROVED_CASES = 2
_MIN_AVERAGE_IMPROVEMENT_PCT = 5.0
_MAX_SINGLE_REGRESSION_PCT = 25.0
_SIGNIFICANT_IMPROVEMENT_PCT = 5.0
_EXPECTED_CHECKS = {
    "large-png-gradient": {"decode-valid": "pass", "non-blank": "pass"},
    "large-jpeg-gradient": {"decode-valid": "pass", "non-blank": "pass"},
    "large-alpha-overlay": {"overlay-ghosting-check": "warn", "watermark-check": "warn"},
    "large-seam": {"seam-check": "fail"},
    "large-dense-text": {"text-glyph-check": "warn"},
    "large-anomaly": {"anomaly-check": "warn"},
}


def _require_pillow():
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:  # pragma: no cover - smoke host guard
        raise RuntimeError(f"Pillow is required for image performance smoke: {exc.__class__.__name__}") from exc
    return Image, ImageDraw


def _write_gradient(path: Path, *, image_mod: Any, draw_mod: Any, fmt: str) -> None:
    width, height = 2048, 2048
    image = image_mod.new("RGB", (width, height))
    draw = draw_mod.Draw(image)
    for x in range(width):
        color = (40 + (x % 180), 80 + (x % 100), 210 - (x % 160))
        draw.line([(x, 0), (x, height - 1)], fill=color)
    image.save(path, format=fmt)


def _write_alpha_overlay(path: Path, *, image_mod: Any, draw_mod: Any) -> None:
    width, height = 1800, 1400
    image = image_mod.new("RGBA", (width, height), (40, 90, 160, 255))
    draw = draw_mod.Draw(image)
    for y in range(80, height - 80, 120):
        draw.rectangle([120, y, width - 120, min(height - 1, y + 80)], fill=(255, 255, 255, 74))
    image.save(path)


def _write_noise(path: Path, *, image_mod: Any) -> None:
    width, height = 1024, 1024
    image = image_mod.new("RGB", (width, height), "white")
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (0, 0, 0) if (x * y + x + y) % 3 == 0 else (255, 255, 255)
    image.save(path)


def _nearest_resample(image_mod: Any) -> Any:
    resampling = getattr(image_mod, "Resampling", None)
    return getattr(resampling, "NEAREST", 0) if resampling is not None else 0


def _write_large_seam(path: Path, *, image_mod: Any, draw_mod: Any) -> None:
    width, height = 2048, 2048
    image = image_mod.new("RGB", (width, height), "black")
    draw = draw_mod.Draw(image)
    draw.rectangle([width // 2, 0, width - 1, height - 1], fill="white")
    image.save(path)


def _write_large_dense_text(path: Path, *, image_mod: Any, draw_mod: Any) -> None:
    image = image_mod.new("RGB", (240, 160), "white")
    draw = draw_mod.Draw(image)
    marker = "WMWM 1234 ABCD ####"
    for y in range(8, 145, 14):
        draw.text((8, y), marker, fill="black")
    image.resize((2048, 1365), _nearest_resample(image_mod)).save(path)


def _write_large_anomaly(path: Path, *, image_mod: Any) -> None:
    width, height = 96, 96
    image = image_mod.new("RGB", (width, height), "white")
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (0, 0, 0) if (x * y + x + y) % 3 == 0 else (255, 255, 255)
    image.resize((2048, 2048), _nearest_resample(image_mod)).save(path)


def _fixtures(root: Path) -> dict[str, Path]:
    image_mod, draw_mod = _require_pillow()
    paths = {
        "large-png-gradient": root / "large-png-gradient.png",
        "large-jpeg-gradient": root / "large-jpeg-gradient.jpg",
        "large-alpha-overlay": root / "large-alpha-overlay.png",
        "noise-anomaly": root / "noise-anomaly.png",
        "large-seam": root / "large-seam.png",
        "large-dense-text": root / "large-dense-text.png",
        "large-anomaly": root / "large-anomaly.png",
    }
    _write_gradient(paths["large-png-gradient"], image_mod=image_mod, draw_mod=draw_mod, fmt="PNG")
    _write_gradient(paths["large-jpeg-gradient"], image_mod=image_mod, draw_mod=draw_mod, fmt="JPEG")
    _write_alpha_overlay(paths["large-alpha-overlay"], image_mod=image_mod, draw_mod=draw_mod)
    _write_noise(paths["noise-anomaly"], image_mod=image_mod)
    _write_large_seam(paths["large-seam"], image_mod=image_mod, draw_mod=draw_mod)
    _write_large_dense_text(paths["large-dense-text"], image_mod=image_mod, draw_mod=draw_mod)
    _write_large_anomaly(paths["large-anomaly"], image_mod=image_mod)
    return paths


def _quality_shape(evidence: dict[str, Any]) -> dict[str, Any]:
    summary = ((evidence.get("imageAnalysis") or {}).get("summary") or {})
    return {
        "status": evidence.get("status"),
        "checks": {
            str(check.get("id")): str(check.get("status"))
            for check in evidence.get("checks") or []
            if isinstance(check, dict)
        },
        "summary": {
            key: summary.get(key)
            for key in (
                "status",
                "sampleWidth",
                "sampleHeight",
                "edgeDensityPct",
                "overlayGhostingRisk",
                "anomalyRisk",
            )
        },
    }


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * 0.95) - 1)]


def _benchmark_case(label: str, path: Path, iterations: int) -> dict[str, Any]:
    durations: list[float] = []
    last: dict[str, Any] = {}
    build_image_quality_evidence(path)
    for _index in range(iterations):
        started = time.perf_counter()
        last = build_image_quality_evidence(path)
        durations.append((time.perf_counter() - started) * 1000)
    return {
        "label": label,
        "iterations": iterations,
        "medianMs": round(statistics.median(durations), 3),
        "p95Ms": round(_p95(durations), 3),
        "minMs": round(min(durations), 3),
        "maxMs": round(max(durations), 3),
        "result": _quality_shape(last),
    }


def _quality_expectations(report: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    cases = {case.get("label"): case for case in report.get("cases") or [] if isinstance(case, dict)}
    for label, expected_checks in _EXPECTED_CHECKS.items():
        checks = (((cases.get(label) or {}).get("result") or {}).get("checks") or {})
        for check_id, expected_status in expected_checks.items():
            actual_status = checks.get(check_id)
            if actual_status != expected_status:
                findings.append({
                    "label": label,
                    "check": check_id,
                    "expected": expected_status,
                    "actual": actual_status,
                })
    return {"ok": not findings, "findings": findings}


def _compare_to_baseline(report: dict[str, Any], baseline_path: Path) -> dict[str, Any]:
    if not baseline_path.is_file():
        return {"available": False, "compatible": False, "performanceAcceptable": False, "deltas": []}
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_cases = {case.get("label"): case for case in baseline.get("cases") or [] if isinstance(case, dict)}
    deltas: list[dict[str, Any]] = []
    compatible = True
    skipped: list[str] = []
    for case in report.get("cases") or []:
        before = baseline_cases.get(case.get("label")) or {}
        if not before:
            skipped.append(str(case.get("label") or "unknown"))
            continue
        before_result = before.get("result") or {}
        same_status = before_result.get("status") == (case.get("result") or {}).get("status")
        same_checks = before_result.get("checks") == (case.get("result") or {}).get("checks")
        compatible = compatible and same_status and same_checks
        before_ms = float(before.get("medianMs") or 0)
        after_ms = float(case.get("medianMs") or 0)
        deltas.append({
            "label": case.get("label"),
            "baselineMedianMs": round(before_ms, 3),
            "optimizedMedianMs": round(after_ms, 3),
            "medianDeltaPct": round((after_ms - before_ms) * 100 / before_ms, 2) if before_ms else None,
            "sameStatus": same_status,
            "sameChecks": same_checks,
        })
    compared = [item for item in deltas if item.get("medianDeltaPct") is not None]
    improved_count = sum(
        1 for item in compared if float(item.get("medianDeltaPct") or 0.0) <= -_SIGNIFICANT_IMPROVEMENT_PCT
    )
    average_delta = (
        sum(float(item.get("medianDeltaPct") or 0.0) for item in compared) / len(compared)
        if compared
        else 0.0
    )
    max_regression = max([float(item.get("medianDeltaPct") or 0.0) for item in compared] + [0.0])
    performance_acceptable = (
        bool(compared)
        and improved_count >= _MIN_IMPROVED_CASES
        and average_delta <= -_MIN_AVERAGE_IMPROVEMENT_PCT
        and max_regression <= _MAX_SINGLE_REGRESSION_PCT
    )
    return {
        "available": True,
        "compatible": compatible,
        "performanceAcceptable": performance_acceptable,
        "averageMedianDeltaPct": round(average_delta, 2),
        "improvedCaseCount": improved_count,
        "comparedCaseCount": len(compared),
        "maxRegressionPct": round(max_regression, 2),
        "thresholds": {
            "minImprovedCases": _MIN_IMPROVED_CASES,
            "minAverageImprovementPct": _MIN_AVERAGE_IMPROVEMENT_PCT,
            "maxSingleRegressionPct": _MAX_SINGLE_REGRESSION_PCT,
        },
        "skippedNewCases": skipped,
        "deltas": deltas,
    }


def run_smoke(*, iterations: int, baseline: Path | None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as workspace:
        paths = _fixtures(Path(workspace))
        cases = [_benchmark_case(label, path, iterations) for label, path in paths.items()]
    report: dict[str, Any] = {
        "status": "PASS",
        "iterations": iterations,
        "cases": cases,
        "redacted": True,
    }
    expectations = _quality_expectations(report)
    report["qualityExpectations"] = expectations
    if not expectations.get("ok"):
        report["status"] = "FAIL"
    if baseline is not None:
        comparison = _compare_to_baseline(report, baseline)
        report["baselineComparison"] = comparison
        if comparison.get("available") and (
            not comparison.get("compatible") or not comparison.get("performanceAcceptable")
        ):
            report["status"] = "FAIL"
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=7)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    payload = run_smoke(iterations=max(3, args.iterations), baseline=args.baseline)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
