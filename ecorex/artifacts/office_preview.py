"""Small, script-free in-app previews for trusted OOXML artifacts."""

from __future__ import annotations

from html import escape
from io import BytesIO
import re
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from .models import ArtifactFamily


_MAX_XML_BYTES = 8 * 1024 * 1024
# ponytail: this is a bounded semantic preview, not an Office layout engine;
# add paged visual renditions only when real fidelity requirements justify it.
_MAX_PREVIEW_CHARS = 512 * 1024
_NUMBER = re.compile(r"(\d+)")


class OfficePreviewError(ValueError):
    pass


def _natural(path: str) -> tuple[object, ...]:
    return tuple(int(part) if part.isdigit() else part for part in _NUMBER.split(path))


def _xml(archive: ZipFile, name: str) -> ElementTree.Element:
    matches = [info for info in archive.infolist() if info.filename == name]
    if len(matches) != 1:
        raise OfficePreviewError("required Office preview content is missing or ambiguous")
    info = matches[0]
    if info.flag_bits & 0x1 or not 0 < info.file_size <= _MAX_XML_BYTES:
        raise OfficePreviewError("Office preview content is invalid")
    data = archive.read(info)
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise OfficePreviewError("Office preview XML declarations are invalid")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise OfficePreviewError("Office preview XML is invalid") from error


def _texts(element: ElementTree.Element, suffix: str = "}t") -> str:
    return "".join(node.text or "" for node in element.iter() if node.tag.endswith(suffix)).strip()


def _document(archive: ZipFile) -> str:
    root = _xml(archive, "word/document.xml")
    body = next((node for node in root.iter() if node.tag.endswith("}body")), None)
    if body is None:
        raise OfficePreviewError("Office document body is missing")
    blocks: list[str] = []
    for node in body:
        if node.tag.endswith("}p"):
            text = _texts(node)
            if text:
                blocks.append(f"<p>{escape(text)}</p>")
        elif node.tag.endswith("}tbl"):
            rows = []
            for row in (child for child in node.iter() if child.tag.endswith("}tr")):
                cells = [
                    f"<td>{escape(_texts(cell))}</td>"
                    for cell in row
                    if cell.tag.endswith("}tc")
                ]
                if cells:
                    rows.append(f"<tr>{''.join(cells)}</tr>")
            if rows:
                blocks.append(f'<table border="1" cellspacing="0" cellpadding="6">{"".join(rows)}</table>')
    return "".join(blocks) or "<p>这份文档没有可提取的文字。</p>"


def _presentation(archive: ZipFile) -> str:
    slides = sorted(
        (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
        key=_natural,
    )[:200]
    if not slides:
        raise OfficePreviewError("Office presentation slides are missing")
    blocks = []
    for index, name in enumerate(slides, start=1):
        text = _texts(_xml(archive, name))
        paragraphs = "".join(f"<p>{escape(line)}</p>" for line in text.splitlines() if line.strip())
        blocks.append(f"<section><h2>第 {index} 页</h2>{paragraphs or '<p>无文字内容</p>'}</section>")
    return "".join(blocks)


def _spreadsheet(archive: ZipFile) -> str:
    shared: list[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        shared = [_texts(node) for node in _xml(archive, "xl/sharedStrings.xml") if node.tag.endswith("}si")]
    sheets = sorted(
        (name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
        key=_natural,
    )[:100]
    if not sheets:
        raise OfficePreviewError("Office spreadsheet sheets are missing")
    blocks = []
    for index, name in enumerate(sheets, start=1):
        rows = []
        root = _xml(archive, name)
        for row in (node for node in root.iter() if node.tag.endswith("}row")):
            cells = []
            for cell in (node for node in row if node.tag.endswith("}c")):
                formula = next((node.text or "" for node in cell if node.tag.endswith("}f")), "")
                value = next((node.text or "" for node in cell if node.tag.endswith("}v")), "")
                if cell.attrib.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                    value = shared[int(value)]
                elif cell.attrib.get("t") == "inlineStr":
                    value = _texts(cell)
                if formula:
                    value = f"={formula}" if not value else f"={formula} ({value})"
                cells.append(f"<td>{escape(value)}</td>")
            if cells:
                rows.append(f"<tr>{''.join(cells[:256])}</tr>")
            if len(rows) >= 10_000:
                break
        blocks.append(
            f'<section><h2>工作表 {index}</h2><table border="1" cellspacing="0" cellpadding="6">'
            f"{''.join(rows) or '<tr><td>无单元格内容</td></tr>'}</table></section>"
        )
    return "".join(blocks)


def render_office_preview(
    family: ArtifactFamily,
    content: bytes,
    *,
    display_name: str,
) -> bytes:
    try:
        with ZipFile(BytesIO(content)) as archive:
            if not 1 <= len(archive.infolist()) <= 4096:
                raise OfficePreviewError("Office archive file count is invalid")
            if family is ArtifactFamily.DOCUMENT:
                body = _document(archive)
            elif family is ArtifactFamily.SPREADSHEET:
                body = _spreadsheet(archive)
            elif family is ArtifactFamily.PRESENTATION:
                body = _presentation(archive)
            else:
                raise OfficePreviewError("artifact is not an OOXML Office file")
    except BadZipFile as error:
        raise OfficePreviewError("Office artifact is not a valid archive") from error
    page = (
        '<!doctype html><meta charset="utf-8">'
        f"<title>{escape(display_name)}</title><main><h1>{escape(display_name)}</h1>{body}</main>"
    )
    if len(page) > _MAX_PREVIEW_CHARS:
        page = page[:_MAX_PREVIEW_CHARS] + "<p>预览内容已截断。</p></main>"
    return page.encode("utf-8")


__all__ = ["OfficePreviewError", "render_office_preview"]
