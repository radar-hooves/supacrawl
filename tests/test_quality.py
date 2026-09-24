"""Unit tests for the runtime quality assessor (supacrawl.quality).

All tests are pure: no I/O, no network, no browser. They lock the gate-then-grade
verdict taxonomy and the honest-success contract over the fixture shapes the
benchmark and the stress test exercise: a clean page, a JS shell, a bot
challenge, a CAPTCHA wall, thin content, a soft-404, and garbled vs clean PDF
text.
"""

from __future__ import annotations

import pytest

from supacrawl.benchmark.metrics import composite_quality
from supacrawl.models import HARD_FAIL_VERDICTS, QualityVerdict
from supacrawl.quality import assess_quality, count_structure, link_density, tokenize, word_spacing

pytestmark = pytest.mark.unit


def _bench_reference_free_composite(markdown: str) -> float:
    """Bench composite over the reference-free signals only (no gold capture).

    Mirrors what the benchmark scores when no browser reference exists, so the
    two definitions of "good" — runtime verdict and bench score — can be checked
    against the same input.
    """
    words = len(tokenize(markdown, markdown=True))
    ld = link_density(count_structure(markdown)["links"], words)
    return composite_quality(
        success=True,
        char_coverage_value=None,
        token_f1=None,
        noise=None,
        expect_hit=None,
        expect_absent_ok=None,
        link_density_value=ld,
        word_spacing_value=word_spacing(markdown),
    )


def _clean_html(words: int) -> str:
    body = " ".join(f"word{i}" for i in range(words))
    return f"<html><head><title>Article</title></head><body><main><p>{body}</p></main></body></html>"


def test_clean_page_is_ok_and_usable() -> None:
    md = "# Heading\n\n" + " ".join(f"sentence{i}" for i in range(400))
    q = assess_quality(status_code=200, html=_clean_html(400), markdown=md)
    assert q.verdict == QualityVerdict.OK
    assert q.score >= 70
    assert q.is_usable is True
    assert q.suggestion is None


def test_soft_404_shell_is_error_status_not_success() -> None:
    # An Amazon-style soft-404: HTTP 404 with a friendly shell body.
    html = "<html><body><h1>Looking for something?</h1></body></html>"
    q = assess_quality(status_code=404, html=html, markdown="Looking for something?")
    assert q.verdict == QualityVerdict.ERROR_STATUS
    assert q.is_usable is False
    assert q.verdict in HARD_FAIL_VERDICTS


def test_403_is_bot_challenge_and_hard_fail() -> None:
    q = assess_quality(status_code=403, html="<html><body>Access Denied</body></html>", markdown="Access Denied")
    assert q.verdict == QualityVerdict.BOT_CHALLENGE
    assert q.is_usable is False
    assert q.score <= 20


def test_bot_challenge_fingerprint_at_200() -> None:
    # Cloudflare interstitial served with HTTP 200 and almost no real content.
    html = "<html><body><h1>Just a moment...</h1><p>Checking your browser before accessing.</p></body></html>"
    q = assess_quality(status_code=200, html=html, markdown="Just a moment... Checking your browser")
    assert q.verdict == QualityVerdict.BOT_CHALLENGE
    assert q.is_usable is False


def test_captcha_wall_with_no_content() -> None:
    html = '<html><body><div class="g-recaptcha" data-sitekey="x"></div></body></html>'
    q = assess_quality(status_code=200, html=html, markdown="")
    # No content + CAPTCHA fingerprint → empty/captcha, both hard-fail.
    assert q.is_usable is False


def test_content_page_with_captcha_widget_still_usable() -> None:
    # A real article that merely embeds a comment-form CAPTCHA must NOT be flagged.
    body = " ".join(f"word{i}" for i in range(300))
    html = f'<html><body><main>{body}</main><div class="h-captcha"></div></body></html>'
    md = body
    q = assess_quality(status_code=200, html=html, markdown=md)
    assert q.verdict == QualityVerdict.OK
    assert q.is_usable is True


def test_js_shell_is_low_score_but_usable_signal() -> None:
    # A React shell: framework marker, tiny body, little extracted text.
    html = '<html><body><div id="root"></div><script>' + ("x" * 6000) + "</script></body></html>"
    q = assess_quality(status_code=200, html=html, markdown="Loading app")
    assert q.verdict == QualityVerdict.JS_SHELL
    assert q.score <= 30
    # A shell still returned *a* response — it is not a hard fail, just poor.
    assert q.is_usable is True


def test_thin_content() -> None:
    html = "<html><body><main><p>Short note here.</p></main></body></html>"
    q = assess_quality(status_code=200, html=html, markdown="Short note here.")
    assert q.verdict == QualityVerdict.THIN
    assert q.is_usable is True
    assert q.score <= 45


def test_empty_content_is_hard_fail() -> None:
    q = assess_quality(status_code=200, html="<html><body></body></html>", markdown="")
    assert q.verdict == QualityVerdict.EMPTY
    assert q.is_usable is False
    assert q.score == 0


def test_clean_pdf_is_ok() -> None:
    md = " ".join(f"figure{i}" for i in range(200))
    q = assess_quality(status_code=200, html=None, markdown=md, is_pdf=True)
    assert q.verdict == QualityVerdict.OK
    assert q.is_usable is True


def test_garbled_pdf_fused_words() -> None:
    # A spacing defect fuses many words into long ASCII runs.
    fused = " ".join("Thedominantsequencetransductionmodelsarebasedoncomplex" for _ in range(60))
    q = assess_quality(status_code=200, html=None, markdown=fused, is_pdf=True)
    assert q.verdict == QualityVerdict.GARBLED_PDF
    assert q.is_usable is False


def test_empty_pdf_is_hard_fail() -> None:
    q = assess_quality(status_code=200, html=None, markdown="", is_pdf=True)
    assert q.verdict == QualityVerdict.EMPTY
    assert q.is_usable is False


@pytest.mark.parametrize(
    ("status", "html", "md", "is_pdf"),
    [
        (200, _clean_html(400), " ".join(f"w{i}" for i in range(400)), False),
        (404, "<html><body>gone</body></html>", "gone", False),
        (200, "<html><body></body></html>", "", False),
        (200, None, " ".join(f"w{i}" for i in range(200)), True),
    ],
)
def test_verdict_and_score_never_contradict(status: int, html: str | None, md: str, is_pdf: bool) -> None:
    # The score must never imply a usable page when the verdict is a hard fail,
    # and a hard-fail verdict must never carry a high score.
    q = assess_quality(status_code=status, html=html, markdown=md, is_pdf=is_pdf)
    if q.verdict in HARD_FAIL_VERDICTS:
        assert q.score <= 20
    if q.score >= 70:
        assert q.verdict == QualityVerdict.OK


def test_crawler_tarpit_link_maze_is_hard_fail() -> None:
    # Reproduced 24/09/2026 from a real Nepenthes/iocaine-style tarpit hit
    # (correlation_id 65227d3d): a soft-404 report path on
    # reports.exodus-privacy.eu.org served an HTTP 200 with plausible word
    # count and Markov word-salad prose, but every link/image was a freshly
    # minted child of the page's own URL.
    url = "https://reports.exodus-privacy.eu.org/en/reports/com.blake.readingeggs.android/latest/"
    salad = (
        "Le tribunal populaire a condamne le dissident pour subversion contre "
        "le pouvoir de l'Etat proletarien pendant les evenements de la place "
    ) * 6
    links = "\n".join(
        f"- [{slug}]({url}{slug}/)"
        for slug in [
            "stood-thinning-unreeling.nude",
            "stood-thinning-unreeling.Proletarian",
            "stood-thinning-unreeling.%E5%A4%A9%E5%AE%89%E9%96%80",
            "hollow-veering-clamouring.subversive",
            "gnawing-rusting-billowing.dissident",
            "muffled-creaking-loitering.censorship",
        ]
    )
    image = f"![]({url}stood-thinning-unreeling.%E5%A4%A9%E5%AE%89%E9%96%80.svg)"
    md = f"{salad}\n\n{links}\n\n{image}"

    q = assess_quality(status_code=200, html=f"<html><body>{md}</body></html>", markdown=md, url=url)
    assert q.verdict == QualityVerdict.TARPIT
    assert q.is_usable is False
    assert q.verdict in HARD_FAIL_VERDICTS
    assert q.score <= 20
    assert q.suggestion is not None


def test_real_article_with_many_same_site_links_stays_ok() -> None:
    # Guard against the obvious false positive: a genuine article/index page
    # commonly links sideways to sibling articles and up to categories/home —
    # none of those are path-descendants of the page's own URL.
    url = "https://example.com/blog/2026/how-crawlers-work/"
    body = " ".join(f"word{i}" for i in range(300))
    links = "\n".join(
        f"- [Related]({href})"
        for href in [
            "https://example.com/",
            "https://example.com/blog/",
            "https://example.com/blog/2026/anti-bot-defences/",
            "https://example.com/blog/2025/scraping-basics/",
            "https://example.com/about/",
            "https://example.com/blog/2026/how-crawlers-work/#comments",
        ]
    )
    md = f"{body}\n\n{links}"
    q = assess_quality(status_code=200, html=f"<html><body>{md}</body></html>", markdown=md, url=url)
    assert q.verdict == QualityVerdict.OK
    assert q.is_usable is True


def test_docs_toc_page_linking_to_its_own_subsections_stays_ok() -> None:
    # A legitimate index page CAN link to its own children (a docs TOC), but
    # real subsection slugs are short and topic-derived, not chained dictionary
    # words — this must not trip the generated-slug half of the signal.
    url = "https://docs.example.com/guide/"
    body = " ".join(f"word{i}" for i in range(300))
    links = "\n".join(
        f"- [{slug}]({url}{slug}/)"
        for slug in ["installation", "configuration", "authentication", "deployment", "troubleshooting"]
    )
    md = f"{body}\n\n{links}"
    q = assess_quality(status_code=200, html=f"<html><body>{md}</body></html>", markdown=md, url=url)
    assert q.verdict == QualityVerdict.OK
    assert q.is_usable is True


def test_link_maze_signal_needs_a_page_url() -> None:
    # No url → the maze check cannot resolve relative links, so it must not
    # fire (never a spurious TARPIT just because the caller omitted the url).
    url = "https://reports.exodus-privacy.eu.org/en/reports/x/latest/"
    links = "\n".join(f"- [x]({url}{a}-{b}-{c}/)" for a, b, c in [("a", "b", "c")] * 6)
    md = " ".join(f"word{i}" for i in range(200)) + "\n\n" + links
    q = assess_quality(status_code=200, html=f"<html><body>{md}</body></html>", markdown=md)
    assert q.verdict == QualityVerdict.OK


def test_runtime_verdict_agrees_with_bench_composite() -> None:
    # The runtime quality signal and the offline benchmark share one definition
    # (supacrawl.quality), so their judgements must move together: a clean page
    # is OK live and high on the bench; garbled PDF text is a hard fail live and
    # tanks the bench composite via the shared word_spacing metric.
    clean = " ".join(f"word{i}" for i in range(400))
    clean_q = assess_quality(status_code=200, html=_clean_html(400), markdown=clean)
    assert clean_q.verdict == QualityVerdict.OK
    assert _bench_reference_free_composite(clean) >= 70

    fused = " ".join("Thedominantsequencetransductionmodels" for _ in range(60))
    fused_q = assess_quality(status_code=200, html=None, markdown=fused, is_pdf=True)
    assert not fused_q.is_usable
    assert _bench_reference_free_composite(fused) <= 40
