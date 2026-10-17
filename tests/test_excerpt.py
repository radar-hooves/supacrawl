"""bound_markdown spends a character budget on the passages that match the query."""

from __future__ import annotations

import pytest

from supacrawl.excerpt import bound_markdown

pytestmark = pytest.mark.unit

pytest.importorskip("rank_bm25")


def _table(rows: int, needle_row: int) -> str:
    head = "| Field | Meaning |\n|---|---|"
    body = [
        f"| Field{i} | {'the needle NumberOfDays is ninety' if i == needle_row else 'ordinary filler text'} |"
        for i in range(rows)
    ]
    return "\n".join([head, *body])


def test_a_document_that_fits_is_returned_whole():
    assert bound_markdown("small", 100, "x") == ("small", "none")


def test_no_query_cuts_from_the_head():
    text, strategy = bound_markdown("a\n" * 500, 100)
    assert strategy == "head" and len(text) <= 100


def test_query_finds_a_row_deep_inside_a_giant_table():
    doc = "# Intro\n\n" + "filler paragraph\n\n" * 200 + _table(3000, needle_row=2500)
    text, strategy = bound_markdown(doc, 3000, "NumberOfDays ninety")

    assert strategy == "query_ranked"
    assert len(text) <= 3000
    assert "Field2500" in text
    assert "| Field | Meaning |" in text, "a lone row lost its column header"


def test_a_query_matching_nothing_falls_back_to_the_head():
    text, strategy = bound_markdown("word " * 1000, 100, "zzzunmatched")
    assert strategy == "head" and len(text) <= 100
