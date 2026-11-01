"""Link-index pages: every link with its context, and a quality verdict that knows a page of links is a page."""

from pathlib import Path

import pytest

from supacrawl.link_index import extract_page_links, is_link_index
from supacrawl.models import QualityVerdict
from supacrawl.quality import assess_quality

FIXTURES = Path(__file__).parent / "fixtures" / "link_index"
LETTERS_URL = "https://www.berkshirehathaway.com/letters/letters.html"
MEMOS_URL = "https://www.oaktreecapital.com/insights/memos"
CHROME_URL = "https://harbourside.example/letters/"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="cp1252", errors="replace")


def _body(links):
    return [link for link in links if not link.chrome]


def test_letters_page_lists_every_letter_with_year_context() -> None:
    links = extract_page_links(_html("letters.html"), LETTERS_URL)
    letters = [link for link in _body(links) if link.text.isdigit() or "letter" in link.text.lower()]
    years = {link.text for link in letters if link.text.isdigit()}
    assert {"1977", "2003", "2004", "2024"} <= years
    pdfs = [link for link in letters if link.url.endswith(".pdf")]
    assert pdfs and all(link.url.startswith("https://www.berkshirehathaway.com/") for link in pdfs)


def test_chrome_page_separates_navigation_footer_and_content() -> None:
    links = {link.url: link for link in extract_page_links(_html("letters-with-chrome.html"), CHROME_URL)}
    for chrome in ("/", "/about/", "/investors/", "/contact/", "/privacy/", "/terms/", "/sitemap/"):
        url = "https://harbourside.example" + chrome
        # /about/ and /investors/ also appear in the page's prose, which makes them content links.
        if chrome in ("/about/",):
            assert not links[url].chrome
        else:
            assert links[url].chrome, url
    assert not links["https://harbourside.example/letters/2003.pdf"].chrome
    assert "2003" in links["https://harbourside.example/letters/2003.pdf"].context or "2003" in (
        links["https://harbourside.example/letters/2003.pdf"].text
    )
    assert "https://harbourside.example/letters/" not in links  # the page itself
    assert not any(url.startswith("mailto:") for url in links)
    both = links["https://harbourside.example/letters/2001.html"]
    assert both.text == "2001 letter (HTML)"
    assert both.heading == "Letters to Shareholders"


def test_memo_archive_gives_year_tab_heading_date_and_javascript_pdf_links() -> None:
    links = extract_page_links(_html("memos.html"), MEMOS_URL)
    by_url = {link.url: link for link in links}
    repeal = by_url["https://www.oaktreecapital.com/insights/memo/shall-we-repeal-the-laws-of-economics---part-iii"]
    assert not repeal.chrome
    assert repeal.heading == "2026"
    assert "Sep 22, 2026" in repeal.context
    assert by_url["https://www.oaktreecapital.com/about"].chrome
    pdfs = [link for link in _body(links) if ".pdf" in link.url]
    assert pdfs, "javascript:openPDF(...) links keep the URL they quote"
    assert any(link.heading == "Compilations" for link in links)


@pytest.mark.parametrize(
    ("name", "url"),
    [("letters.html", LETTERS_URL), ("memos.html", MEMOS_URL), ("letters-with-chrome.html", CHROME_URL)],
)
def test_listing_is_a_link_index_and_graded_ok(name: str, url: str) -> None:
    html = _html(name)
    assert is_link_index(extract_page_links(html, url))
    # The markdown a prose-scoring extractor leaves of a listing: a few words.
    verdict = assess_quality(status_code=200, html=html, markdown="2020 onwards", url=url)
    assert verdict.verdict is QualityVerdict.OK
    assert verdict.score >= 70
    assert any(reason.startswith("link index:") for reason in verdict.reasons)
    # With no url given, relative links still count.
    assert assess_quality(status_code=200, html=html, markdown="2020 onwards").verdict is QualityVerdict.OK


def test_page_of_links_is_still_thin_without_enough_content_links() -> None:
    html = "<html><body><p>Hi</p><a href='/a'>A</a><a href='/b'>B</a></body></html>"
    verdict = assess_quality(status_code=200, html=html, markdown="Hi A B", url="https://x.example/")
    assert verdict.verdict is QualityVerdict.THIN


def test_chrome_only_links_do_not_make_an_index() -> None:
    nav = "".join(f"<a href='/p{i}'>Page {i}</a>" for i in range(12))
    html = f"<html><body><nav>{nav}</nav><div id='app'></div><script src='/_next/static/x.js'></script></body></html>"
    verdict = assess_quality(status_code=200, html=html, markdown="Page", url="https://x.example/")
    assert verdict.verdict is not QualityVerdict.OK


def test_bot_wall_with_links_is_still_a_wall() -> None:
    links = "".join(f"<a href='/l{i}'>Link {i}</a>" for i in range(10))
    html = f"<html><body><h1>Just a moment...</h1><p>Checking your browser before accessing</p>{links}</body></html>"
    assert assess_quality(status_code=403, html=html, markdown="Just a moment", url="https://x.example/").verdict is (
        QualityVerdict.BOT_CHALLENGE
    )


def test_tarpit_of_generated_children_is_not_rescued_by_being_links() -> None:
    kids = "".join(f"<a href='/maze/stood-thinning-word{i}x'>stood thinning word{i}x</a>" for i in range(8))
    html = f"<html><body><main>{kids}</main></body></html>"
    md = "\n".join(f"[stood-thinning-word{i}x](/maze/stood-thinning-word{i}x)" for i in range(8))
    verdict = assess_quality(status_code=200, html=html, markdown=md, url="https://x.example/maze")
    assert verdict.verdict is QualityVerdict.TARPIT


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "url", "expected"), [("memos.html", MEMOS_URL, 9), ("letters.html", LETTERS_URL, 48)])
async def test_scrape_serves_a_listing_with_link_details(
    monkeypatch: pytest.MonkeyPatch, name: str, url: str, expected: int
) -> None:
    from supacrawl.services.http_fetch import HttpFetchResult
    from supacrawl.services.scrape import ScrapeService

    async def fake_fetch(_url: str, **_kwargs: object) -> HttpFetchResult:
        return HttpFetchResult(url=url, html=_html(name), status_code=200, content_type="text/html", headers={})

    monkeypatch.setattr("supacrawl.services.scrape.fetch_static", fake_fetch)
    result = await ScrapeService().scrape(url, formats=["markdown", "linkDetails"], max_age=0)
    assert result.success
    assert result.quality is not None and result.quality.verdict is QualityVerdict.OK
    assert result.data is not None and result.data.link_details is not None
    documents = [
        link
        for link in result.data.link_details
        if not link.chrome and (".pdf" in link.url or "/memo" in link.url or "/letters/" in link.url)
    ]
    assert len(documents) >= expected


_YOUNG = (
    "<html><body><main><h1>Letters</h1><ul>"
    "<li><a href='/letters/2025.pdf'>2025 letter</a></li><li><a href='/letters/2024.pdf'>2024 letter</a></li>"
    "</ul></main></body></html>"
)


def test_declared_link_index_is_ok_from_one_content_link_undeclared_is_thin() -> None:
    url = "https://fund.example/letters/"
    undeclared = assess_quality(status_code=200, html=_YOUNG, markdown="Letters 2025 letter 2024 letter", url=url)
    assert undeclared.verdict is QualityVerdict.THIN
    declared = assess_quality(
        status_code=200, html=_YOUNG, markdown="Letters 2025 letter 2024 letter", url=url, link_index=True
    )
    assert declared.verdict is QualityVerdict.OK
    one = "<html><body><main><a href='/a.pdf'>First letter</a></main></body></html>"
    assert assess_quality(status_code=200, html=one, markdown="First letter", url=url, link_index=True).verdict is (
        QualityVerdict.OK
    )
    assert assess_quality(status_code=200, html=one, markdown="First letter", url=url).verdict is QualityVerdict.THIN


def test_declared_link_index_does_not_excuse_walls_errors_shells_or_chrome_only_pages() -> None:
    url = "https://fund.example/letters/"
    wall = "<html><body><h1>Just a moment...</h1><p>Checking your browser before accessing</p><a href='/a'>A</a></body></html>"
    assert assess_quality(status_code=403, html=wall, markdown="Just a moment", url=url, link_index=True).verdict is (
        QualityVerdict.BOT_CHALLENGE
    )
    assert assess_quality(status_code=404, html=_YOUNG, markdown="x", url=url, link_index=True).verdict is (
        QualityVerdict.ERROR_STATUS
    )
    shell = "<html><body><nav><a href='/x'>Home</a></nav><div id='app'></div><script src='/_next/static/a.js'></script></body></html>"
    assert assess_quality(status_code=200, html=shell, markdown="Home", url=url, link_index=True).verdict is not (
        QualityVerdict.OK
    )
    assert assess_quality(
        status_code=200, html="<html><body></body></html>", markdown="", url=url, link_index=True
    ).verdict is (QualityVerdict.EMPTY)
    kids = "".join(f"<a href='/maze/stood-thinning-word{i}x'>stood thinning word{i}x</a>" for i in range(8))
    md = "\n".join(f"[stood-thinning-word{i}x](/maze/stood-thinning-word{i}x)" for i in range(8))
    assert (
        assess_quality(
            status_code=200,
            html=f"<html><body><main>{kids}</main></body></html>",
            markdown=md,
            url="https://x.example/maze",
            link_index=True,
        ).verdict
        is QualityVerdict.TARPIT
    )
