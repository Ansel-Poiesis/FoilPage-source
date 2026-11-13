# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Minimal Markdown rendering for the desktop workbench.

Dependency-free subset covering everything FoilPage articles emit: ATX headings,
bold / italic / inline code, ordered & unordered lists, blockquotes,
thematic breaks and fenced code blocks. Links render as their label text
(the URL stays in the source .md file).

The module is deliberately pure (no tkinter) so it can be unit-tested and
reused by both the reader and the library card previews.
"""
from __future__ import annotations

import re

__all__ = ["parse_markdown", "strip_markdown"]

_FENCE_RE = re.compile(r"^```(.*)$")
_ATX_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$")
_UL_RE = re.compile(r"^[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\d+[.)]\s+(.*)$")
_HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")

_INLINE_RE = re.compile(
    r"(?P<code>`+)(?P<code_text>.+?)(?P=code)"
    r"|(?P<link>\[[^\]]+\]\([^)]*\))"
    r"|(?P<strong>\*\*|__)(?P<strong_text>.+?)(?P=strong)"
    r"|(?P<em>\*|_)(?P<em_text>.+?)(?P=em)"
)
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _inline(text: str) -> list[tuple[str, frozenset[str]]]:
    """Split a line into (text, style-set) segments."""
    segments: list[tuple[str, frozenset[str]]] = []
    pos = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > pos:
            segments.append((text[pos:match.start()], frozenset()))
        if match.group("code") is not None:
            segments.append((match.group("code_text"), frozenset({"code"})))
        elif match.group("link") is not None:
            link = _LINK_RE.match(match.group("link"))
            label = link.group(1) if link else match.group("link")
            segments.append((label, frozenset()))
        elif match.group("strong") is not None:
            segments.append((match.group("strong_text"), frozenset({"strong"})))
        else:
            segments.append((match.group("em_text"), frozenset({"em"})))
        pos = match.end()
    if pos < len(text):
        segments.append((text[pos:], frozenset()))
    return segments


def _is_block_start(stripped: str) -> bool:
    return bool(
        _ATX_RE.match(stripped)
        or _FENCE_RE.match(stripped)
        or _UL_RE.match(stripped)
        or _OL_RE.match(stripped)
        or _BLOCKQUOTE_RE.match(stripped)
        or _HR_RE.match(stripped)
    )


def parse_markdown(text: str) -> list[dict]:
    """Parse markdown text into a list of block dicts.

    Blocks are one of:
      {"kind": "h1"|"h2"|"h3", "segments": [...]}
      {"kind": "p", "segments": [...]}
      {"kind": "ul"|"ol", "items": [[...], ...]}
      {"kind": "quote", "segments": [...]}
      {"kind": "code", "lang": str, "text": str}
      {"kind": "hr"}
    where a segment is a (text, frozenset-of-styles) tuple.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    blocks: list[dict] = []
    index = 0
    count = len(lines)
    while index < count:
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue

        fence = _FENCE_RE.match(stripped)
        if fence:
            code_lines: list[str] = []
            index += 1
            while index < count:
                current = lines[index].strip()
                if _FENCE_RE.match(current):
                    break
                code_lines.append(lines[index])
                index += 1
            index += 1  # skip the closing fence
            blocks.append({"kind": "code", "lang": fence.group(1).strip(), "text": "\n".join(code_lines)})
            continue

        if _HR_RE.match(stripped):
            blocks.append({"kind": "hr"})
            index += 1
            continue

        heading = _ATX_RE.match(stripped)
        if heading:
            level = min(len(heading.group(1)), 3)
            blocks.append({
                "kind": f"h{level}",
                "segments": _inline(heading.group(2).strip()),
            })
            index += 1
            continue

        ul = _UL_RE.match(stripped)
        ol = _OL_RE.match(stripped)
        if ul or ol:
            ordered = bool(ol)
            items: list[list[tuple[str, frozenset[str]]]] = []
            while index < count:
                current = lines[index].strip()
                if not current:
                    break
                item_match = _OL_RE.match(current) if ordered else _UL_RE.match(current)
                if not item_match:
                    break
                items.append(_inline(item_match.group(1).strip()))
                index += 1
            blocks.append({"kind": "ol" if ordered else "ul", "items": items})
            continue

        quote = _BLOCKQUOTE_RE.match(stripped)
        if quote:
            quote_lines: list[str] = []
            while index < count:
                current = lines[index].strip()
                if not current:
                    break
                quote_match = _BLOCKQUOTE_RE.match(current)
                if not quote_match:
                    break
                quote_lines.append(quote_match.group(1).strip())
                index += 1
            blocks.append({"kind": "quote", "segments": _inline(" ".join(quote_lines))})
            continue

        paragraph: list[str] = [stripped]
        index += 1
        while index < count:
            current = lines[index].strip()
            if not current or _is_block_start(current):
                break
            paragraph.append(current)
            index += 1
        blocks.append({"kind": "p", "segments": _inline(" ".join(paragraph))})
    return blocks


def _block_plain(block: dict) -> str:
    kind = block["kind"]
    if kind == "code":
        return block.get("text", "")
    if kind in ("ul", "ol"):
        return " ".join(
            "".join(segment for segment, _ in item) for item in block.get("items", [])
        )
    if kind == "hr":
        return ""
    return "".join(segment for segment, _ in block.get("segments", []))


def strip_markdown(text: str) -> str:
    """Return plain text with markdown syntax removed (card previews)."""
    parts = [_block_plain(block) for block in parse_markdown(text)]
    return " ".join(part for part in parts if part).strip()
