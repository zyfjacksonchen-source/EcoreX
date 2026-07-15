from __future__ import annotations

import pytest

from ecorex.control_plane.share_markdown import (
    ShareMarkdownDocument,
    ShareMarkdownParseMetrics,
    parse_share_markdown,
    render_share_markdown,
)


def test_restricted_markdown_ast_renders_chat_parity_subset() -> None:
    source = """# 执行摘要

已完成 **两项**，详见 [`交付物`](https://example.com/report?q=one&lang=zh)。

- 读取资料
- 生成 `report.pdf`

3. 检查表格
4. 确认图片

| 项目 | 状态 | 耗时 |
| :--- | :---: | ---: |
| 报告 | **完成** | 12s |

```python
print("<safe>")
```
"""

    document = parse_share_markdown(source)
    assert isinstance(document, ShareMarkdownDocument)
    rendered = render_share_markdown(source)

    assert "<h1>执行摘要</h1>" in rendered
    assert "已完成 <strong>两项</strong>" in rendered
    assert (
        '<a href="https://example.com/report?q=one&amp;lang=zh" target="_blank" '
        'rel="noopener noreferrer nofollow"><code>交付物</code></a>'
    ) in rendered
    assert "<ul><li>读取资料</li><li>生成 <code>report.pdf</code></li></ul>" in rendered
    assert '<ol start="3"><li>检查表格</li><li>确认图片</li></ol>' in rendered
    assert '<th scope="col" class="align-center">状态</th>' in rendered
    assert '<td class="align-right">12s</td>' in rendered
    assert '<pre data-language="python"><code>print(&quot;&lt;safe&gt;&quot;)</code></pre>' in rendered


@pytest.mark.parametrize(
    "source",
    [
        '<script>alert("x")</script>',
        '<img src=x onerror="alert(1)">',
        '[run](javascript:alert(1))',
        '[payload](data:text/html,<script>alert(1)</script>)',
        '[credentials](https://user:secret@example.com/private)',
        '![external](https://evil.example/tracker.png)',
        '[protocol-relative](//evil.example/path)',
        '[encoded](&#106;avascript:alert(1))',
    ],
)
def test_restricted_markdown_never_promotes_active_or_image_content(source: str) -> None:
    rendered = render_share_markdown(source)

    assert "<script" not in rendered.casefold()
    assert "<img" not in rendered.casefold()
    assert "<a " not in rendered.casefold()
    assert 'href="javascript:' not in rendered.casefold()
    assert 'href="data:' not in rendered.casefold()


def test_raw_html_and_table_cells_are_text_not_renderer_extensions() -> None:
    rendered = render_share_markdown(
        "| name | value |\n| --- | --- |\n| <svg/onload=alert(1)> | a\\|b |"
    )

    assert "<svg" not in rendered
    assert "&lt;svg/onload=alert(1)&gt;" in rendered
    assert "a|b" in rendered
    assert rendered.count("<table>") == 1


def test_markdown_size_and_structural_budgets_are_bounded() -> None:
    boundary = "x" * 1_000_000
    rendered = render_share_markdown(boundary)
    assert rendered == f"<p>{boundary}</p>"

    with pytest.raises(ValueError, match="size limit"):
        render_share_markdown(boundary + "x")

    # Adversarially dense valid markup exhausts the structural-node budget and
    # becomes inert text instead of allocating an unbounded AST.
    dense = "**x**" * 100_000
    dense_rendered = render_share_markdown(dense)
    assert len(dense_rendered) < 1_500_000
    assert dense_rendered.startswith("<p><strong>x</strong>")
    assert "**x**" in dense_rendered


@pytest.mark.parametrize(
    "source",
    [
        "[x" * 10_000,
        ("![x" * 6_666) + "![",
        "[" * 20_000,
        ("[x](" * 4_000),
    ],
)
def test_unmatched_link_image_and_brackets_have_a_linear_operation_budget(source: str) -> None:
    metrics = ShareMarkdownParseMetrics()
    document = parse_share_markdown(source, metrics=metrics)

    assert isinstance(document, ShareMarkdownDocument)
    assert metrics.speculative_scan_steps <= max(1_024, len(source) * 2)
    assert metrics.operations <= len(source) * 3 + 1_024

    rendered = render_share_markdown(source)
    assert "<a " not in rendered
    assert "<img" not in rendered


@pytest.mark.parametrize(
    "source,maximum_factor",
    [
        ("[x" * 500_000, 3),
        ("x" + "`" * 999_999, 2),
    ],
    ids=("unmatched-link-1m", "unmatched-code-run-1m"),
)
def test_million_character_inline_adversaries_remain_bounded(
    source: str,
    maximum_factor: int,
) -> None:
    metrics = ShareMarkdownParseMetrics()
    document = parse_share_markdown(source, metrics=metrics)

    assert len(document.blocks) == 1
    assert metrics.speculative_scan_steps <= len(source) * 2
    assert metrics.operations <= len(source) * maximum_factor + 1_024
