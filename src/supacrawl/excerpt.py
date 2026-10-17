"""Bound a long markdown document to a character budget.

Without a query the document is cut from the head. With a query (and
``rank_bm25``) the budget is spent on the passages that best match it, in
document order, so a fact deep inside a large table survives the cut.
"""

from __future__ import annotations

import logging
import re

LOGGER = logging.getLogger(__name__)

# Blocks longer than this (tables, long code) are split by line so one giant
# block cannot hide the matching rows from the ranking.
_BLOCK_SPLIT_CHARS = 1500
_GAP_MARKER = "[…]"
_TABLE_SEPARATOR = re.compile(r"^\|?\s*:?-{3,}")
_WORD = re.compile(r"\w+")


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _split_long_block(block: str) -> list[str]:
    """Split an oversized block into line groups, repeating a table's header on each."""
    lines = block.split("\n")
    header: list[str] = []
    if len(lines) > 2 and lines[0].startswith("|") and _TABLE_SEPARATOR.match(lines[1]):
        header, lines = lines[:2], lines[2:]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) > _BLOCK_SPLIT_CHARS:
            chunks.append("\n".join([*header, *current]))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join([*header, *current]))
    return chunks


def _blocks(markdown: str) -> list[str]:
    blocks: list[str] = []
    for block in re.split(r"\n\s*\n", markdown):
        if not block.strip():
            continue
        blocks.extend(_split_long_block(block) if len(block) > _BLOCK_SPLIT_CHARS else [block])
    return blocks


def _head(markdown: str, max_chars: int) -> str:
    cut = markdown[:max_chars]
    newline = cut.rfind("\n")
    return cut[:newline] if newline > max_chars * 0.8 else cut


def bound_markdown(markdown: str, max_chars: int, query: str | None = None) -> tuple[str, str]:
    """Return ``(text, strategy)`` with ``len(text) <= max_chars``.

    ``strategy`` is ``"none"`` when the document already fits, ``"query_ranked"``
    when passages were chosen by relevance to ``query``, else ``"head"``.
    """
    if len(markdown) <= max_chars:
        return markdown, "none"
    q_tokens = _tokens(query) if query else []
    if not q_tokens:
        return _head(markdown, max_chars), "head"
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        return _head(markdown, max_chars), "head"

    blocks = _blocks(markdown)
    corpus = [_tokens(b) or ["_"] for b in blocks]
    scores = BM25Okapi(corpus).get_scores(q_tokens)
    if not any(s > 0 for s in scores):
        return _head(markdown, max_chars), "head"

    chosen: set[int] = set()
    used = 0
    for i in sorted(range(len(blocks)), key=lambda i: scores[i], reverse=True):
        if scores[i] <= 0:
            break
        cost = len(blocks[i]) + len(_GAP_MARKER) + 4
        if used + cost <= max_chars:
            chosen.add(i)
            used += cost

    parts: list[str] = []
    previous = -2
    for i in sorted(chosen):
        if i != previous + 1:
            parts.append(_GAP_MARKER)
        parts.append(blocks[i])
        previous = i
    text = "\n\n".join(parts)
    if not chosen:
        return _head(markdown, max_chars), "head"
    return text[:max_chars], "query_ranked"
