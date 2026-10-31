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


DOC = "# Top\n\nintro\n\n## Alpha Option\n\nalpha body\n\n### Sub\n\nsub body\n\n```\n# not a heading\n```\n\n## Beta\n\nbeta body"


def test_a_section_runs_to_the_next_heading_of_its_level_with_its_subsections():
    from supacrawl.excerpt import select_section

    out = select_section(DOC, "Alpha Option")
    assert out is not None and out.startswith("## Alpha Option") and "sub body" in out
    assert "# not a heading" in out and "beta body" not in out


def test_a_section_is_found_by_anchor_and_the_last_runs_to_the_end():
    from supacrawl.excerpt import select_section

    assert select_section(DOC, "#alpha-option") == select_section(DOC, "alpha option")
    assert select_section(DOC, "beta") == "## Beta\n\nbeta body"


def test_an_unknown_section_is_none():
    from supacrawl.excerpt import select_section

    assert select_section(DOC, "gamma") is None


def test_a_table_under_a_heading_is_ranked_by_the_headings_name():
    filler = "\n\n".join(f"## Opt{i}\n\nfiller about option {i} and the default allowed values" for i in range(400))
    doc = f"{filler}\n\n## zetaThing\n\nintro text\n\n| Name | Value |\n|---|---|\n| allowedValues | a, b |"
    text, _ = bound_markdown(doc, 3000, "what are the allowed values of zetaThing")
    assert "| allowedValues | a, b |" in text


def test_the_section_the_query_names_comes_first_whole_on_a_reference_page():
    """A long section is split into blocks; a row deep in it shares few words with the query, while hundreds
    of sibling sections share its ordinary words. The heading the query names wins (the Renovate shape)."""
    common = "The default settings this option applies: schedule, prCreation and prConcurrentLimit are read here. "
    filler = "\n\n".join(f"## `opt{i}`\n\n{common * 3}" for i in range(300))
    named = "## `vulnerabilityAlerts`\n\nConfig to apply when a PR is needed.\n\n" + "\n\n".join(
        f"Paragraph {i} about enabling the dependency graph and alerts." for i in range(60)
    )
    answer = "| Name | Value |\n|---|---|\n| default | {schedule: [], prCreation: immediate} |"
    doc = f"{filler}\n\n{named}\n\n{answer}\n\n## `zzz`\n\n{common}"
    text, strategy = bound_markdown(
        doc, 6000, "What are the default settings that the vulnerabilityAlerts option applies?"
    )
    assert strategy == "query_ranked"
    assert answer in text, "the named section's table was not kept"
    assert "## `vulnerabilityAlerts`" in text


def test_heading_names_matches_an_identifier_however_the_heading_spells_it():
    from supacrawl.excerpt import heading_names, query_tokens

    q = query_tokens("What does minimumReleaseAgeBehaviour do, and the Vulnerability Alerts section?")
    assert heading_names("`minimumReleaseAgeBehaviour`", q)
    assert heading_names("Minimum Release Age Behaviour", q)
    assert heading_names("Vulnerability Alerts", q)
    assert not heading_names("Release", q)
    assert not heading_names("", q)
