"""Signed Pack-Python worker for dependency service invocations."""

from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import sys
import textwrap
import time
from typing import Any, Mapping


def _inside(module: Any, root: Path) -> None:
    origin = getattr(module, "__file__", None)
    try:
        Path(origin).resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, TypeError, ValueError):
        raise RuntimeError("dependency origin escaped verified pack") from None


def _ocr(payload: Mapping[str, Any], runtime: Path) -> Mapping[str, Any]:
    import numpy as np
    from PIL import Image, ImageOps
    import PIL
    import rapidocr_onnxruntime
    from rapidocr_onnxruntime import RapidOCR

    for module in (np, PIL, rapidocr_onnxruntime):
        _inside(module, runtime)
    content = base64.b64decode(str(payload.get("content_base64") or ""), validate=True)
    if not content or len(content) > 8 * 1024 * 1024:
        raise ValueError("OCR content is invalid")
    started = time.monotonic()
    with Image.open(BytesIO(content)) as source:
        pixels = np.asarray(ImageOps.exif_transpose(source).convert("RGB"))
    raw = RapidOCR()(pixels)
    values = raw[0] if isinstance(raw, tuple) and raw else raw
    blocks: list[dict[str, Any]] = []
    if isinstance(values, list):
        for item in values:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            text = str(item[1] or "").strip()
            if not text:
                continue
            confidence = item[2] if len(item) > 2 else None
            blocks.append(
                {
                    "text": text,
                    "confidence": (
                        max(0.0, min(1.0, float(confidence)))
                        if isinstance(confidence, (int, float))
                        and not isinstance(confidence, bool)
                        else None
                    ),
                }
            )
    text = "\n".join(item["text"] for item in blocks)
    return {
        "status": "success" if text else "empty",
        "provider": "rapidocr_onnxruntime",
        "text": text,
        "blocks": blocks,
        "latencyMs": int((time.monotonic() - started) * 1000),
        "cacheHit": False,
    }


def _office_probe(runtime: Path) -> Mapping[str, Any]:
    import docx
    import openpyxl
    import pptx
    import pypdf
    import reportlab

    for module in (docx, openpyxl, pptx, pypdf, reportlab):
        _inside(module, runtime)
    return {
        "provider": "python-office-formats-v1",
        "modules": ["docx", "openpyxl", "pptx", "pypdf", "reportlab"],
    }


def _text(value: Any, *, maximum: int = 4096) -> str:
    text = str(value or "").strip()
    if not text or len(text.encode("utf-8")) > maximum:
        raise ValueError("office text is invalid")
    return text


def _office_create(payload: Mapping[str, Any], runtime: Path) -> Mapping[str, Any]:
    family = str(payload.get("family") or "")
    title = _text(payload.get("title") or "e-Mate 办公产物", maximum=512)
    body = BytesIO()
    validation: dict[str, Any]

    if family == "document":
        import docx

        _inside(docx, runtime)
        document = docx.Document()
        document.add_heading(title, level=0)
        sections = payload.get("sections") or []
        for section in sections:
            if not isinstance(section, Mapping):
                raise ValueError("document section is invalid")
            heading = str(section.get("heading") or "").strip()
            if heading:
                document.add_heading(_text(heading, maximum=512), level=1)
            paragraphs = section.get("paragraphs") or []
            if not isinstance(paragraphs, list):
                raise ValueError("document paragraphs are invalid")
            for paragraph in paragraphs:
                document.add_paragraph(_text(paragraph))
        document.save(body)
        reopened = docx.Document(BytesIO(body.getvalue()))
        validation = {"paragraph_count": len(reopened.paragraphs)}
        mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        extension = ".docx"
    elif family == "spreadsheet":
        import openpyxl

        _inside(openpyxl, runtime)
        workbook = openpyxl.Workbook()
        workbook.remove(workbook.active)
        sheets = payload.get("sheets") or []
        if not isinstance(sheets, list) or not sheets:
            raise ValueError("spreadsheet sheets are invalid")
        for sheet_payload in sheets:
            if not isinstance(sheet_payload, Mapping):
                raise ValueError("spreadsheet sheet is invalid")
            sheet = workbook.create_sheet(_text(sheet_payload.get("name"), maximum=64))
            rows = sheet_payload.get("rows") or []
            if not isinstance(rows, list):
                raise ValueError("spreadsheet rows are invalid")
            for row in rows:
                if not isinstance(row, list):
                    raise ValueError("spreadsheet row is invalid")
                sheet.append(row)
        workbook.save(body)
        reopened = openpyxl.load_workbook(BytesIO(body.getvalue()), read_only=True)
        validation = {"sheet_count": len(reopened.sheetnames)}
        reopened.close()
        mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        extension = ".xlsx"
    elif family == "presentation":
        import pptx

        _inside(pptx, runtime)
        presentation = pptx.Presentation()
        slides = payload.get("slides") or []
        if not isinstance(slides, list) or not slides:
            raise ValueError("presentation slides are invalid")
        for index, slide_payload in enumerate(slides):
            if not isinstance(slide_payload, Mapping):
                raise ValueError("presentation slide is invalid")
            slide = presentation.slides.add_slide(
                presentation.slide_layouts[0 if index == 0 else 1]
            )
            slide.shapes.title.text = _text(slide_payload.get("title"), maximum=512)
            bullets = slide_payload.get("bullets") or []
            if not isinstance(bullets, list):
                raise ValueError("presentation bullets are invalid")
            if index == 0:
                if len(slide.placeholders) > 1:
                    slide.placeholders[1].text = "\n".join(_text(item) for item in bullets)
            else:
                frame = slide.placeholders[1].text_frame
                frame.clear()
                for bullet_index, bullet in enumerate(bullets):
                    paragraph = frame.paragraphs[0] if bullet_index == 0 else frame.add_paragraph()
                    paragraph.text = _text(bullet)
        presentation.save(body)
        reopened = pptx.Presentation(BytesIO(body.getvalue()))
        validation = {"slide_count": len(reopened.slides)}
        mime_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        extension = ".pptx"
    elif family == "pdf":
        import pypdf
        import reportlab
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen import canvas

        for module in (pypdf, reportlab):
            _inside(module, runtime)
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        writer = canvas.Canvas(body, pagesize=A4)
        width, height = A4
        y = height - 64
        writer.setFont("STSong-Light", 20)
        writer.drawString(56, y, title)
        y -= 36
        writer.setFont("STSong-Light", 11)
        for section in payload.get("sections") or []:
            if not isinstance(section, Mapping):
                raise ValueError("PDF section is invalid")
            heading = str(section.get("heading") or "").strip()
            lines = ([heading] if heading else []) + list(section.get("paragraphs") or [])
            for value in lines:
                for line in textwrap.wrap(_text(value), width=55, replace_whitespace=False):
                    if y < 56:
                        writer.showPage()
                        writer.setFont("STSong-Light", 11)
                        y = height - 56
                    writer.drawString(56, y, line)
                    y -= 18
                y -= 6
        writer.save()
        reopened = pypdf.PdfReader(BytesIO(body.getvalue()))
        validation = {"page_count": len(reopened.pages)}
        mime_type = "application/pdf"
        extension = ".pdf"
    else:
        raise ValueError("office family is unsupported")

    content = body.getvalue()
    # Base64 and the response envelope must remain under the process adapter's
    # 8 MiB stdout boundary.
    if not 1 <= len(content) <= 5 * 1024 * 1024:
        raise ValueError("office output size is invalid")
    return {
        "provider": "python-office-formats-v1",
        "family": family,
        "mime_type": mime_type,
        "extension": extension,
        "size_bytes": len(content),
        "content_base64": base64.b64encode(content).decode("ascii"),
        "validation": validation,
    }


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    root = Path(sys.argv[1]).resolve(strict=True)
    runtime = (root / "runtime" / "python").resolve(strict=True)
    runtime.relative_to(root)
    sys.path.insert(0, str(runtime))
    try:
        request = json.loads(
            sys.stdin.buffer.read(12 * 1024 * 1024 + 1).decode("utf-8")
        )
        pack_id = request.get("pack_id")
        operation = request.get("operation")
        payload = request.get("payload")
        if request.get("schema_version") != 1 or not isinstance(payload, Mapping):
            return 3
        if pack_id == "ocr" and operation == "extract":
            result = _ocr(payload, runtime)
        elif pack_id == "office" and operation == "probe":
            result = _office_probe(runtime)
        elif pack_id == "office" and operation == "create":
            result = _office_create(payload, runtime)
        else:
            return 4
        sys.stdout.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "pack_id": pack_id,
                    "status": "success",
                    "result": result,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except Exception:
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
