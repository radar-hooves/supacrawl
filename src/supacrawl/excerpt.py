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
_SECTION_BLOCK_CHARS = 4000
_GAP_MARKER = "[…]"
_TABLE_SEPARATOR = re.compile(r"^\|?\s*:?-{3,}")
_WORD = re.compile(r"\w+")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


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


def _blocks(markdown: str) -> list[tuple[str, str]]:
    """``(block, heading)`` pairs to rank. A short section (heading and everything under it) is one block, so
    its body is ranked by its title; a longer one is split on blank lines, each block carrying the section's
    heading so a row deep in it is still ranked as part of the section named above it."""
    sections: list[list[str]] = []
    for block in re.split(r"\n\s*\n", markdown):
        if not block.strip():
            continue
        if not sections or _HEADING.match(block.split("\n", 1)[0]):
            sections.append([])
        sections[-1].append(block)
    blocks: list[tuple[str, str]] = []
    for parts in sections:
        match = _HEADING.match(parts[0].split("\n", 1)[0])
        heading = match.group(2) if match else ""
        if len("\n\n".join(parts)) <= _SECTION_BLOCK_CHARS:
            blocks.append(("\n\n".join(parts), heading))
            continue
        if len(parts) > 1 and match and "\n" not in parts[0]:
            parts = ["\n\n".join(parts[:2]), *parts[2:]]
        for block in parts:
            for piece in _split_long_block(block) if len(block) > _BLOCK_SPLIT_CHARS else [block]:
                blocks.append((piece, heading))
    return blocks


def query_tokens(query: str) -> list[str]:
    """The words of a query, lower-cased; an identifier such as ``vulnerabilityAlerts`` stays one token."""
    return _tokens(query)


def heading_names(heading: str, q_tokens: list[str]) -> bool:
    """Whether ``heading`` is what the query asks about: its words run together equal one query token (an
    identifier, however the heading spaces or cases it), or its whole text appears in the query."""
    words = _tokens(re.sub(r"[`*_\[\]]", "", heading))
    if not words:
        return False
    return "".join(words) in q_tokens or f" {' '.join(words)} " in f" {' '.join(q_tokens)} "


def _head(markdown: str, max_chars: int) -> str:
    cut = markdown[:max_chars]
    newline = cut.rfind("\n")
    return cut[:newline] if newline > max_chars * 0.8 else cut


def bound_markdown(markdown: str, max_chars: int, query: str | None = None) -> tuple[str, str]:
    """Return ``(text, strategy)`` with ``len(text) <= max_chars``.

    ``strategy`` is ``"none"`` when the document already fits, ``"query_ranked"``
    when passages were chosen by relevance to ``query``, else ``"head"``. A
    section whose heading the query names (``heading_names``) comes first,
    whole: on a reference page the answer is under the option's own name, and
    hundreds of sibling sections share the query's ordinary words.
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
    corpus = [_tokens(f"{heading} {block}") or ["_"] for block, heading in blocks]
    scores = list(BM25Okapi(corpus).get_scores(q_tokens))
    top = max(scores)
    if top <= 0:
        return _head(markdown, max_chars), "head"
    for i, (_, heading) in enumerate(blocks):
        if heading and heading_names(heading, q_tokens):
            scores[i] = top + 1.0

    chosen: set[int] = set()
    used = 0
    for i in sorted(range(len(blocks)), key=lambda i: scores[i], reverse=True):
        if scores[i] <= 0:
            break
        cost = len(blocks[i][0]) + len(_GAP_MARKER) + 4
        if used + cost <= max_chars:
            chosen.add(i)
            used += cost

    parts: list[str] = []
    previous = -2
    for i in sorted(chosen):
        if i != previous + 1:
            parts.append(_GAP_MARKER)
        parts.append(blocks[i][0])
        previous = i
    text = "\n\n".join(parts)
    if not chosen:
        return _head(markdown, max_chars), "head"
    return text[:max_chars], "query_ranked"


def _slug(text: str) -> str:
    """A heading's anchor: lower-case words joined by hyphens, as GitHub and most doc sites make them."""
    return "-".join(_tokens(re.sub(r"[`*_\[\]]", "", text)))


def select_section(markdown: str, section: str) -> str | None:
    """The part of ``markdown`` under the heading named by ``section``, or None when none matches.

    ``section`` is a heading's text or its anchor (``#`` optional), compared
    case-insensitively by anchor slug. The section runs from the heading to the
    next heading of the same or higher level, so its subsections come with it.
    Fenced code is never read as headings.
    """
    wanted = _slug(section.lstrip("#"))
    if not wanted:
        return None
    lines = markdown.split("\n")
    start = level = None
    fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        match = None if fence else _HEADING.match(line)
        if not match:
            continue
        depth = len(match.group(1))
        if start is None:
            if _slug(match.group(2)) == wanted:
                start, level = i, depth
        elif depth <= level:  # type: ignore[operator]
            return "\n".join(lines[start:i]).strip()
    return None if start is None else "\n".join(lines[start:]).strip()
