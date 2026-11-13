# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the lightweight markdown renderer used by the library reader."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ui.markdown_render import parse_markdown, strip_markdown  # noqa: E402


def test_parses_headings_paragraphs_lists() -> None:
    blocks = parse_markdown(
        "# 标题一\n\n## 标题二\n\n正文第一段\n继续同一段。\n\n- 甲\n- 乙\n\n1. 一\n2. 二\n"
    )
    kinds = [block["kind"] for block in blocks]
    assert kinds == ["h1", "h2", "p", "ul", "ol"]
    assert blocks[0]["segments"] == [("标题一", frozenset())]
    assert [item[0][0] for item in blocks[3]["items"]] == ["甲", "乙"]
    assert [item[0][0] for item in blocks[4]["items"]] == ["一", "二"]


def test_parses_quote_hr_and_fenced_code() -> None:
    blocks = parse_markdown("> 引用\n\n---\n\n```python\nprint(1)\n```\n")
    assert [block["kind"] for block in blocks] == ["quote", "hr", "code"]
    assert blocks[0]["segments"] == [("引用", frozenset())]
    assert blocks[2]["lang"] == "python"
    assert blocks[2]["text"] == "print(1)"


def test_inline_styles_and_links() -> None:
    blocks = parse_markdown("**粗体** 与 *斜体* 与 `代码` 与 [链接](https://a.b) 结尾")
    assert len(blocks) == 1
    segments = blocks[0]["segments"]
    styles = {segment[0]: segment[1] for segment in segments}
    assert "粗体" in styles and styles["粗体"] == frozenset({"strong"})
    assert "斜体" in styles and styles["斜体"] == frozenset({"em"})
    assert "代码" in styles and styles["代码"] == frozenset({"code"})
    assert "链接" in styles and styles["链接"] == frozenset()
    assert "https://a.b" not in "".join(segment[0] for segment in segments)


def test_strip_markdown_removes_symbols() -> None:
    sample = (
        "# 标题\n\n一段**粗体**和*斜体*，含 `代码`。\n\n- 甲\n- 乙\n\n> 引用\n\n[链接](https://x)\n"
    )
    plain = strip_markdown(sample)
    for symbol in ("#", "**", "*", "`", "- 甲", ">", "](https://"):
        assert symbol not in plain
    assert "粗体" in plain and "斜体" in plain and "代码" in plain
    assert plain.startswith("标题")
    assert "链接" in plain


def test_strip_markdown_handles_empty_and_plain_text() -> None:
    assert strip_markdown("") == ""
    assert strip_markdown("   \n  \n") == ""
    assert strip_markdown("纯文本一行") == "纯文本一行"


def test_parse_markdown_handles_crlf() -> None:
    blocks = parse_markdown("# 标题\r\n\r\n正文\r\n")
    assert [block["kind"] for block in blocks] == ["h1", "p"]
