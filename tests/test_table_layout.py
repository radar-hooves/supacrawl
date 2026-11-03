"""A page laid out as one table: the text cell is the content, the other cells are chrome."""

from pathlib import Path

from supacrawl.services.converter import MarkdownConverter

ESSAY = (Path(__file__).parent / "fixtures" / "table_layout" / "essay.html").read_text(encoding="utf-8")
URL = "https://garden.example/essays/slow.html"


def _md(html: str, **kwargs: object) -> str:
    return MarkdownConverter().convert(html, base_url=URL, **kwargs)  # type: ignore[arg-type]


def test_layout_table_keeps_the_text_cell_and_the_title_as_text() -> None:
    md = _md(ESSAY)
    assert md.startswith("# On Slow Gardens\n\nThe first thing to understand about gardens")
    assert "None of this is complicated" in md
    for chrome in ("logo.gif", "nav-essays", "Example Garden Site", "Contact", "Deutsch", "Français", "|"):
        assert chrome not in md, chrome


def test_title_comes_from_the_title_tag_when_the_page_has_no_title_image() -> None:
    html = ESSAY.replace(
        '<img src="/img/title-slow-gardens.gif" alt="On Slow Gardens" width="400" height="40"><br>', ""
    )
    md = _md(html)
    assert md.startswith("# On Slow Gardens\n\nThe first thing")
    assert "logo.gif" not in md


def test_title_is_not_doubled_when_the_text_opens_with_it() -> None:
    html = ESSAY.replace(
        '<img src="/img/title-slow-gardens.gif" alt="On Slow Gardens" width="400" height="40"><br>',
        "<b>On Slow Gardens</b><br><br>",
    )
    assert _md(html).count("On Slow Gardens") == 1


def test_layout_table_is_kept_when_main_content_is_not_asked_for() -> None:
    assert "logo.gif" in _md(ESSAY, only_main_content=False)


def test_modern_article_with_a_small_data_table_is_unchanged() -> None:
    prose = " ".join(["Sentence about gardens and soil and patience."] * 30)
    html = (
        f"<html><head><title>Post</title></head><body><article><h1>Post</h1><p>{prose}</p>"
        "<table><tr><td>Plant</td><td>Depth</td></tr><tr><td>Bean</td><td>5 cm</td></tr></table>"
        "</article></body></html>"
    )
    md = _md(html)
    assert "| Plant | Depth |" in md or ("Plant" in md and "Bean" in md)
    assert md.startswith("# Post")


def test_data_table_with_header_cells_is_never_unwrapped() -> None:
    cells = "".join(f"<tr><th>Row {i}</th><td>{'word ' * 30}</td></tr>" for i in range(3))
    html = f"<html><body><table>{cells}</table></body></html>"
    assert "Row 0" in _md(html) and "Row 2" in _md(html)


def test_table_of_equal_cells_is_not_a_layout() -> None:
    cells = "".join(f"<td>{'word ' * 30}{i}</td>" for i in range(4))
    html = f"<html><body><table><tr>{cells}</tr></table></body></html>"
    md = _md(html)
    assert "word word" in md and md.count("|") > 4


def test_nested_one_cell_layout_table_is_unwrapped_and_br_runs_are_paragraphs() -> None:
    assert ESSAY.count("<table") == 2, "the fixture nests a one-cell layout table in the text cell"
    md = _md(ESSAY)
    assert "|" not in md
    assert "![" not in md
    assert md.count("\n\n") >= 4
    assert md.splitlines()[0] == "# On Slow Gardens"


def test_data_table_inside_the_nested_text_cell_still_renders_as_a_table() -> None:
    table = (
        "<table><tr><th>Plant</th><th>Depth</th></tr><tr><td>Bean</td><td>5 cm</td></tr>"
        "<tr><td>Pea</td><td>4 cm</td></tr></table>"
    )
    html = ESSAY.replace("</font>\n</td></tr>", f"{table}</font>\n</td></tr>", 1)
    md = _md(html)
    assert "| Plant | Depth |" in md
    assert "| Bean | 5 cm |" in md
    assert md.startswith("# On Slow Gardens")


CHROME_ESSAY = (Path(__file__).parent / "fixtures" / "table_layout" / "essay_chrome.html").read_text(encoding="utf-8")
STUB = (
    "<html><head><title>X</title></head><body><nav class='site-nav'>{c}</nav><div id='cookie-banner'>{c}</div>"
    "<main><p>This page has moved.</p></main><footer>{c}</footer></body></html>"
).format(c="navigation " * 200)


def test_essay_with_logo_promo_and_translation_links_comes_out_as_its_article() -> None:
    md = _md(CHROME_ESSAY, only_main_content=True, content_mode=0.5)
    assert "The first thing to understand about gardens" in md
    assert "None of this is complicated" in md
    for chrome in ("logo.gif", "Example Garden Site", "Want to plant", "Translation", "flag-"):
        assert chrome not in md, chrome


def test_a_stub_main_is_not_replaced_by_the_cookie_banner() -> None:
    assert _md(STUB, only_main_content=True, content_mode=0.5) == "This page has moved."
