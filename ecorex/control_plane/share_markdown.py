"""Bounded, script-free Markdown for public share transcripts.

The public share page is a separate trust boundary from the local WebUI.  It
therefore does not accept pre-rendered HTML and does not use an extensible
Markdown engine.  Source text is first projected into this small AST and the
renderer can emit only the elements declared below.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import re
from urllib.parse import urlsplit


_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$")
_UNORDERED_ITEM = re.compile(r"^ {0,3}[-+*][ \t]+(.*)$")
_ORDERED_ITEM = re.compile(r"^ {0,3}(\d{1,9})[.)][ \t]+(.*)$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[^\S\r\n]*(.*)$")
_TABLE_DELIMITER = re.compile(r"^:?-{3,}:?$")
_LANGUAGE = re.compile(r"^[A-Za-z0-9_+-]{1,32}$")
_MAX_SOURCE_CHARS = 1_000_000
_MAX_STRUCTURAL_NODES = 50_000
_MAX_BLOCKS = 10_000
_MAX_INLINE_DEPTH = 12
_MAX_TABLE_COLUMNS = 64
_MAX_TABLE_ROWS = 4_096
_MAX_LINK_CHARS = 2_048
_MAX_CODE_FENCE_RUN = 32
_SPECULATIVE_SCAN_FACTOR = 2
_SPECULATIVE_SCAN_FLOOR = 1_024


class InlineNode:
    """Closed base type for safe inline nodes."""


@dataclass(frozen=True, slots=True)
class Text(InlineNode):
    value: str


@dataclass(frozen=True, slots=True)
class LineBreak(InlineNode):
    pass


@dataclass(frozen=True, slots=True)
class Strong(InlineNode):
    children: tuple[InlineNode, ...]


@dataclass(frozen=True, slots=True)
class CodeSpan(InlineNode):
    value: str


@dataclass(frozen=True, slots=True)
class Link(InlineNode):
    href: str
    children: tuple[InlineNode, ...]


class BlockNode:
    """Closed base type for safe block nodes."""


@dataclass(frozen=True, slots=True)
class Paragraph(BlockNode):
    children: tuple[InlineNode, ...]


@dataclass(frozen=True, slots=True)
class Heading(BlockNode):
    level: int
    children: tuple[InlineNode, ...]


@dataclass(frozen=True, slots=True)
class CodeBlock(BlockNode):
    value: str
    language: str | None


@dataclass(frozen=True, slots=True)
class ListItem:
    children: tuple[InlineNode, ...]


@dataclass(frozen=True, slots=True)
class ListBlock(BlockNode):
    ordered: bool
    start: int
    items: tuple[ListItem, ...]


@dataclass(frozen=True, slots=True)
class TableCell:
    children: tuple[InlineNode, ...]
    alignment: str | None


@dataclass(frozen=True, slots=True)
class Table(BlockNode):
    header: tuple[TableCell, ...]
    rows: tuple[tuple[TableCell, ...], ...]


@dataclass(frozen=True, slots=True)
class ShareMarkdownDocument:
    blocks: tuple[BlockNode, ...]


@dataclass(slots=True)
class ShareMarkdownParseMetrics:
    """Deterministic parser work counters used by security regression gates."""

    inline_steps: int = 0
    speculative_scan_steps: int = 0

    @property
    def operations(self) -> int:
        return self.inline_steps + self.speculative_scan_steps


@dataclass(slots=True)
class _Budget:
    speculative_scan_limit: int
    metrics: ShareMarkdownParseMetrics
    structural_nodes: int = 0
    blocks: int = 0

    def take_structure(self) -> bool:
        if self.structural_nodes >= _MAX_STRUCTURAL_NODES:
            return False
        self.structural_nodes += 1
        return True

    def take_block(self) -> bool:
        if self.blocks >= _MAX_BLOCKS:
            return False
        self.blocks += 1
        return True

    def take_speculative_scan(self) -> bool:
        if self.metrics.speculative_scan_steps >= self.speculative_scan_limit:
            return False
        self.metrics.speculative_scan_steps += 1
        return True


def _append_text(nodes: list[InlineNode], value: str) -> None:
    if not value:
        return
    if nodes and isinstance(nodes[-1], Text):
        previous = nodes[-1]
        nodes[-1] = Text(previous.value + value)
    else:
        nodes.append(Text(value))


def _link_syntax(
    source: str,
    start: int,
    budget: _Budget,
) -> tuple[str, str, int] | None:
    """Return label, destination and exclusive end for one Markdown link."""

    if start >= len(source) or source[start] != "[":
        return None
    cursor = start + 1
    escaped = False
    close_label = -1
    while cursor < len(source):
        if not budget.take_speculative_scan():
            return None
        character = source[cursor]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "[":
            # Nested labels would permit nested anchors after recursive inline
            # rendering.  They are outside this restricted dialect, and
            # rejecting at the next opener also prevents repeated tail scans.
            return None
        elif character == "]":
            close_label = cursor
            break
        elif character == "\n":
            return None
        cursor += 1
    if close_label < 0 or close_label + 1 >= len(source) or source[close_label + 1] != "(":
        return None

    destination_start = close_label + 2
    cursor = destination_start
    escaped = False
    parenthesis_depth = 0
    close_destination = -1
    while cursor < len(source):
        if not budget.take_speculative_scan():
            return None
        character = source[cursor]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character in "[]":
            # Raw brackets in destinations are deliberately unsupported. URLs
            # can percent-encode them; stopping here makes failed candidates
            # non-overlapping and keeps aggregate link scanning linear.
            return None
        elif character == "(":
            parenthesis_depth += 1
        elif character == ")":
            if parenthesis_depth:
                parenthesis_depth -= 1
            else:
                close_destination = cursor
                break
        elif character == "\n":
            return None
        cursor += 1
    if close_destination < 0:
        return None
    return (
        source[start + 1 : close_label],
        source[destination_start:close_destination],
        close_destination + 1,
    )


def _safe_link(destination: str) -> str | None:
    value = destination.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    if (
        not value
        or len(value) > _MAX_LINK_CHARS
        or value != value.strip()
        or "\\" in value
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None and not 1 <= port <= 65_535
    ):
        return None
    return value


def _find_inline_marker(
    source: str,
    marker: str,
    start: int,
    budget: _Budget,
) -> int:
    """Find a delimiter without allowing aggregate speculative rescans."""

    last_start = len(source) - len(marker)
    cursor = start
    while cursor <= last_start:
        if not budget.take_speculative_scan():
            return -1
        if source.startswith(marker, cursor):
            return cursor
        cursor += 1
    return -1


def _parse_inlines(source: str, budget: _Budget, *, depth: int = 0) -> tuple[InlineNode, ...]:
    nodes: list[InlineNode] = []
    plain: list[str] = []

    def flush_plain() -> None:
        if plain:
            _append_text(nodes, "".join(plain))
            plain.clear()

    cursor = 0
    while cursor < len(source):
        budget.metrics.inline_steps += 1
        if budget.structural_nodes >= _MAX_STRUCTURAL_NODES:
            plain.append(source[cursor:])
            break
        character = source[cursor]
        if character == "\n":
            flush_plain()
            if budget.take_structure():
                nodes.append(LineBreak())
            else:
                plain.append("\n")
            cursor += 1
            continue
        if character == "\\" and cursor + 1 < len(source):
            escaped = source[cursor + 1]
            if escaped in r"\\`*{}[]()#+-.!_|>":
                plain.append(escaped)
                cursor += 2
                continue
        if character == "`":
            run_end = cursor + 1
            while run_end < len(source) and source[run_end] == "`":
                budget.metrics.inline_steps += 1
                run_end += 1
            marker = source[cursor:run_end]
            if len(marker) > _MAX_CODE_FENCE_RUN:
                plain.append(marker)
                cursor = run_end
                continue
            close = _find_inline_marker(source, marker, run_end, budget)
            if close >= 0:
                flush_plain()
                value = source[run_end:close].replace("\n", " ")
                if value.startswith(" ") and value.endswith(" ") and value.strip():
                    value = value[1:-1]
                if budget.take_structure():
                    nodes.append(CodeSpan(value))
                else:
                    _append_text(nodes, source[cursor : close + len(marker)])
                cursor = close + len(marker)
                continue
            # Consume the complete delimiter run.  Retrying each suffix of one
            # unmatched run is quadratic even before searching for a closer.
            plain.append(marker)
            cursor = run_end
            continue
        if (
            depth < _MAX_INLINE_DEPTH
            and source.startswith(("**", "__"), cursor)
        ):
            marker = source[cursor : cursor + 2]
            close = _find_inline_marker(source, marker, cursor + 2, budget)
            if close > cursor + 2:
                flush_plain()
                if budget.take_structure():
                    children = _parse_inlines(source[cursor + 2 : close], budget, depth=depth + 1)
                    nodes.append(Strong(children))
                else:
                    _append_text(nodes, source[cursor : close + 2])
                cursor = close + 2
                continue
        if character == "!" and cursor + 1 < len(source) and source[cursor + 1] == "[":
            image = _link_syntax(source, cursor + 1, budget)
            if image is not None:
                # Images in message Markdown are deliberately inert.  Public
                # images may only come from the share's token-bound artifacts.
                _label, _destination, end = image
                plain.append(source[cursor:end])
                cursor = end
                continue
        if character == "[" and depth < _MAX_INLINE_DEPTH:
            parsed_link = _link_syntax(source, cursor, budget)
            if parsed_link is not None:
                label, destination, end = parsed_link
                safe_href = _safe_link(destination)
                if safe_href is None:
                    plain.append(source[cursor:end])
                else:
                    flush_plain()
                    if budget.take_structure():
                        children = _parse_inlines(label, budget, depth=depth + 1)
                        nodes.append(Link(href=safe_href, children=children))
                    else:
                        _append_text(nodes, source[cursor:end])
                cursor = end
                continue
        plain.append(character)
        cursor += 1
    flush_plain()
    return tuple(nodes)


def _split_table_row(line: str) -> tuple[list[str], bool]:
    cells: list[str] = []
    current: list[str] = []
    had_separator = False
    cursor = 0
    code_marker = 0
    while cursor < len(line):
        character = line[cursor]
        if character == "\\" and cursor + 1 < len(line):
            current.extend((character, line[cursor + 1]))
            cursor += 2
            continue
        if character == "`":
            run_end = cursor + 1
            while run_end < len(line) and line[run_end] == "`":
                run_end += 1
            run_size = run_end - cursor
            if code_marker == run_size:
                code_marker = 0
            elif code_marker == 0:
                code_marker = run_size
            current.append(line[cursor:run_end])
            cursor = run_end
            continue
        if character == "|" and code_marker == 0:
            had_separator = True
            cells.append("".join(current).strip())
            current.clear()
        else:
            current.append(character)
        cursor += 1
    cells.append("".join(current).strip())
    stripped = line.strip()
    if stripped.startswith("|") and cells and not cells[0]:
        cells.pop(0)
    if stripped.endswith("|") and cells and not cells[-1]:
        cells.pop()
    return cells, had_separator


def _table_shape(lines: list[str], index: int) -> tuple[list[str], list[str], list[str | None]] | None:
    if index + 1 >= len(lines):
        return None
    header, header_has_pipe = _split_table_row(lines[index])
    delimiters, delimiter_has_pipe = _split_table_row(lines[index + 1])
    if (
        not header_has_pipe
        or not delimiter_has_pipe
        or not header
        or len(header) != len(delimiters)
        or len(header) > _MAX_TABLE_COLUMNS
    ):
        return None
    alignments: list[str | None] = []
    for delimiter in delimiters:
        normalized = delimiter.strip()
        if not _TABLE_DELIMITER.fullmatch(normalized):
            return None
        if normalized.startswith(":") and normalized.endswith(":"):
            alignments.append("center")
        elif normalized.endswith(":"):
            alignments.append("right")
        elif normalized.startswith(":"):
            alignments.append("left")
        else:
            alignments.append(None)
    return header, delimiters, alignments


def _is_block_start(lines: list[str], index: int) -> bool:
    line = lines[index]
    return bool(
        not line.strip()
        or _HEADING.match(line)
        or _FENCE.match(line)
        or _UNORDERED_ITEM.match(line)
        or _ORDERED_ITEM.match(line)
        or _table_shape(lines, index)
    )


def parse_share_markdown(
    source: str,
    *,
    metrics: ShareMarkdownParseMetrics | None = None,
) -> ShareMarkdownDocument:
    """Parse the explicitly supported Markdown subset into an immutable AST."""

    if not isinstance(source, str):
        raise TypeError("share Markdown source must be text")
    if len(source) > _MAX_SOURCE_CHARS:
        raise ValueError("share Markdown source exceeds its size limit")
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    blocks: list[BlockNode] = []
    parse_metrics = metrics if metrics is not None else ShareMarkdownParseMetrics()
    budget = _Budget(
        speculative_scan_limit=parse_metrics.speculative_scan_steps
        + max(
            _SPECULATIVE_SCAN_FLOOR,
            len(normalized) * _SPECULATIVE_SCAN_FACTOR,
        ),
        metrics=parse_metrics,
    )
    index = 0

    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        if not budget.take_block():
            remainder = "\n".join(lines[index:])
            blocks.append(Paragraph((Text(remainder),)))
            break

        fence = _FENCE.match(lines[index])
        if fence:
            marker = fence.group(1)
            info = fence.group(2).strip().split(maxsplit=1)
            language = info[0] if info and _LANGUAGE.fullmatch(info[0]) else None
            code_lines: list[str] = []
            index += 1
            while index < len(lines):
                closing = re.match(r"^ {0,3}([`~]+)[ \t]*$", lines[index])
                if (
                    closing
                    and closing.group(1)[0] == marker[0]
                    and len(closing.group(1)) >= len(marker)
                ):
                    index += 1
                    break
                code_lines.append(lines[index])
                index += 1
            blocks.append(CodeBlock("\n".join(code_lines), language))
            continue

        heading = _HEADING.match(lines[index])
        if heading:
            blocks.append(
                Heading(
                    level=len(heading.group(1)),
                    children=_parse_inlines(heading.group(2) or "", budget),
                )
            )
            index += 1
            continue

        table_shape = _table_shape(lines, index)
        if table_shape is not None:
            header_values, _delimiters, alignments = table_shape
            header = tuple(
                TableCell(_parse_inlines(value, budget), alignments[position])
                for position, value in enumerate(header_values)
            )
            index += 2
            body: list[tuple[TableCell, ...]] = []
            while index < len(lines) and len(body) < _MAX_TABLE_ROWS:
                values, had_pipe = _split_table_row(lines[index])
                if not lines[index].strip() or not had_pipe:
                    break
                values = (values + [""] * len(header))[: len(header)]
                body.append(
                    tuple(
                        TableCell(_parse_inlines(value, budget), alignments[position])
                        for position, value in enumerate(values)
                    )
                )
                index += 1
            blocks.append(Table(header=header, rows=tuple(body)))
            continue

        unordered = _UNORDERED_ITEM.match(lines[index])
        ordered = _ORDERED_ITEM.match(lines[index])
        if unordered or ordered:
            is_ordered = ordered is not None
            start = int(ordered.group(1)) if ordered else 1
            items: list[ListItem] = []
            while index < len(lines):
                match = _ORDERED_ITEM.match(lines[index]) if is_ordered else _UNORDERED_ITEM.match(lines[index])
                if match is None:
                    break
                content = match.group(2) if is_ordered else match.group(1)
                items.append(ListItem(_parse_inlines(content, budget)))
                index += 1
            blocks.append(ListBlock(ordered=is_ordered, start=start, items=tuple(items)))
            continue

        paragraph_lines = [lines[index]]
        index += 1
        while index < len(lines) and not _is_block_start(lines, index):
            paragraph_lines.append(lines[index])
            index += 1
        blocks.append(Paragraph(_parse_inlines("\n".join(paragraph_lines), budget)))

    return ShareMarkdownDocument(tuple(blocks))


def _clean_text(value: str) -> str:
    return "".join(
        character if character in "\n\t" or ord(character) >= 32 else "\ufffd"
        for character in value
    )


def _render_inlines(nodes: tuple[InlineNode, ...]) -> str:
    rendered: list[str] = []
    for node in nodes:
        if isinstance(node, Text):
            rendered.append(html.escape(_clean_text(node.value)))
        elif isinstance(node, LineBreak):
            rendered.append("<br>")
        elif isinstance(node, Strong):
            rendered.append(f"<strong>{_render_inlines(node.children)}</strong>")
        elif isinstance(node, CodeSpan):
            rendered.append(f"<code>{html.escape(_clean_text(node.value))}</code>")
        elif isinstance(node, Link):
            href = html.escape(node.href, quote=True)
            rendered.append(
                f'<a href="{href}" target="_blank" rel="noopener noreferrer nofollow">'
                f"{_render_inlines(node.children)}</a>"
            )
        else:  # pragma: no cover - defensive closure for future AST changes.
            raise TypeError("unknown share Markdown inline node")
    return "".join(rendered)


def _alignment_class(alignment: str | None) -> str:
    return f' class="align-{alignment}"' if alignment in {"left", "center", "right"} else ""


def render_share_markdown(source: str) -> str:
    """Render source through the safe AST; raw source can never become HTML."""

    document = parse_share_markdown(source)
    rendered: list[str] = []
    for block in document.blocks:
        if isinstance(block, Paragraph):
            rendered.append(f"<p>{_render_inlines(block.children)}</p>")
        elif isinstance(block, Heading):
            rendered.append(
                f"<h{block.level}>{_render_inlines(block.children)}</h{block.level}>"
            )
        elif isinstance(block, CodeBlock):
            language = (
                f' data-language="{html.escape(block.language, quote=True)}"'
                if block.language
                else ""
            )
            rendered.append(
                f"<pre{language}><code>{html.escape(_clean_text(block.value))}</code></pre>"
            )
        elif isinstance(block, ListBlock):
            tag = "ol" if block.ordered else "ul"
            start = f' start="{block.start}"' if block.ordered and block.start != 1 else ""
            items = "".join(
                f"<li>{_render_inlines(item.children)}</li>" for item in block.items
            )
            rendered.append(f"<{tag}{start}>{items}</{tag}>")
        elif isinstance(block, Table):
            header = "".join(
                f'<th scope="col"{_alignment_class(cell.alignment)}>'
                f"{_render_inlines(cell.children)}</th>"
                for cell in block.header
            )
            rows = "".join(
                "<tr>"
                + "".join(
                    f"<td{_alignment_class(cell.alignment)}>{_render_inlines(cell.children)}</td>"
                    for cell in row
                )
                + "</tr>"
                for row in block.rows
            )
            rendered.append(
                '<div class="markdown-table-wrap" role="region" aria-label="表格" tabindex="0">'
                f"<table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table></div>"
            )
        else:  # pragma: no cover - defensive closure for future AST changes.
            raise TypeError("unknown share Markdown block node")
    return "".join(rendered)


__all__ = [
    "ShareMarkdownDocument",
    "ShareMarkdownParseMetrics",
    "parse_share_markdown",
    "render_share_markdown",
]
