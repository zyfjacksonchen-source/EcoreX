#!/usr/bin/env python3
"""Smoke R24-08 PDF page-level quality evidence."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.office_pdf_runtime import (  # noqa: E402
    OfficePdfRuntimeError,
    build_pdf_quality_evidence,
    render_pdf_pages,
)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _create_pdf(path: Path, *, blank: bool = False, landscape_page: bool = False) -> None:
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.pdfgen import canvas

    page_size = landscape(letter) if landscape_page else letter
    c = canvas.Canvas(str(path), pagesize=page_size)
    if not blank:
        c.setFont("Helvetica", 14)
        c.drawString(72, page_size[1] - 72, "Private PDF smoke report")
        c.setFont("Helvetica", 10)
        rows = [
            ("Private Product", "Units", "Price", "Revenue"),
            ("Secret Alpha", "10", "2.5", "25"),
            ("Secret Beta", "12", "3.0", "36"),
        ]
        start_x = 72
        start_y = page_size[1] - 120
        col_width = 95
        row_height = 22
        for row_index, row in enumerate(rows):
            y = start_y - row_index * row_height
            for col_index, value in enumerate(row):
                x = start_x + col_index * col_width
                c.drawString(x + 4, y - 15, value)
                c.rect(x, y - row_height, col_width, row_height)
    c.showPage()
    c.save()


def _try_render(path: Path, output_dir: Path) -> tuple[bool, Dict[str, Any], list[Dict[str, Any]]]:
    try:
        rendered = render_pdf_pages(path, output_dir, max_pages=1, dpi=96)
        return True, rendered, list(rendered.get("artifacts") or [])
    except OfficePdfRuntimeError as exc:
        return False, {"status": "unavailable", "errorType": type(exc).__name__}, [
            {"page": 1, "artifactRef": "hmac:synthetic-pdf-render", "width": 612, "height": 792}
        ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke R24-08 PDF quality evidence.")
    parser.add_argument("--output", default="docs/v0.2.4/artifacts/pdf-quality-smoke.json")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="ecorex-pdf-quality-") as tmp:
        root = Path(tmp)
        clean_path = root / "clean-private-report.pdf"
        blank_path = root / "blank-private-report.pdf"
        mismatch_path = root / "mismatch-private-report.pdf"
        _create_pdf(clean_path)
        _create_pdf(blank_path, blank=True)
        _create_pdf(mismatch_path, landscape_page=True)

        clean_render_available, clean_render, clean_render_items = _try_render(clean_path, root / "clean-render")
        mismatch_render_available, mismatch_render, mismatch_render_items = _try_render(
            mismatch_path,
            root / "mismatch-render",
        )

        clean = build_pdf_quality_evidence(clean_path, renders=clean_render_items, reference_path=clean_path)
        blank = build_pdf_quality_evidence(blank_path, renders=[])
        mismatch = build_pdf_quality_evidence(mismatch_path, renders=mismatch_render_items, reference_path=clean_path)

        expected_clean_no_render_failures = {"page-render"}
        required_blank_failures = {"text-orientation", "page-render", "layout-inspection"}
        required_mismatch_failures = {"visual-diff"}
        clean_failed_checks = {item["id"] for item in clean.get("checks", []) if item.get("status") == "fail"}
        blank_failed_checks = {item["id"] for item in blank.get("checks", []) if item.get("status") == "fail"}
        mismatch_failed_checks = {item["id"] for item in mismatch.get("checks", []) if item.get("status") == "fail"}
        synthetic_render_rejected = (
            not clean_render_available
            and "page-render" in clean_failed_checks
            and not clean.get("renderedArtifacts")
        )
        clean_render_contract_ok = (
            clean.get("status") == "pass" and not clean_failed_checks
            if clean_render_available
            else clean.get("status") == "fail"
            and synthetic_render_rejected
            and clean_failed_checks == expected_clean_no_render_failures
        )

        serialized = json.dumps({"clean": clean, "blank": blank, "mismatch": mismatch}, ensure_ascii=False)
        leaks = [
            item
            for item in (
                "Private PDF smoke report",
                "Private Product",
                "Secret Alpha",
                "Secret Beta",
                str(root),
                clean_path.name,
                blank_path.name,
                mismatch_path.name,
            )
            if item and item in serialized
        ]
        payload = {
            "status": (
                "PASS"
                if (
                    clean_render_contract_ok
                    and blank.get("status") == "fail"
                    and required_blank_failures <= blank_failed_checks
                    and required_mismatch_failures <= mismatch_failed_checks
                    and not leaks
                )
                else "FAIL"
            ),
            "cleanActualRender": clean_render,
            "cleanActualRenderAvailable": clean_render_available,
            "mismatchActualRender": mismatch_render,
            "mismatchActualRenderAvailable": mismatch_render_available,
            "syntheticRenderRejected": synthetic_render_rejected,
            "cleanStatus": clean.get("status"),
            "cleanFailedChecks": sorted(clean_failed_checks),
            "expectedCleanNoRenderFailures": sorted(expected_clean_no_render_failures),
            "blankStatus": blank.get("status"),
            "blankFailedChecks": sorted(blank_failed_checks),
            "requiredBlankFailures": sorted(required_blank_failures),
            "mismatchStatus": mismatch.get("status"),
            "mismatchFailedChecks": sorted(mismatch_failed_checks),
            "requiredMismatchFailures": sorted(required_mismatch_failures),
            "cleanSummary": clean.get("pdfAnalysis", {}).get("summary", {}),
            "blankSummary": blank.get("pdfAnalysis", {}).get("summary", {}),
            "mismatchDiffSummary": mismatch.get("pdfDiffAnalysis", {}).get("summary", {}),
            "leakCount": len(leaks),
            "redacted": True,
        }
        _write_json(Path(args.output), payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
