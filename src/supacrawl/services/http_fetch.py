"""HTTP-first fetch: a cheap httpx GET for pages that do not need a browser.

Most pages render their content server-side; launching Playwright for them
wastes seconds and laptop battery. This module performs a single httpx GET so
``ScrapeService`` can try the cheap path first and escalate to a full browser
render only when a render-needed or bot-challenge signal fires.
"""

import logging
from dataclasses import dataclass, field

import httpx

from supacrawl.services._pdf_sniff import MAX_PDF_SIZE, is_pdf_bytes
from supacrawl.services.url_guard import guarded_request

LOGGER = logging.getLogger(__name__)

# A realistic desktop Chrome UA so static fetches look like an ordinary browser
# rather than a bare HTTP client (which more sites block outright).
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Don't pull multi-megabyte documents over the fast path — the browser handles
# heavy pages. 5 MB is comfortably above any text/HTML article.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024


@dataclass
class HttpFetchResult:
    """Result of a successful HTTP-first fetch.

    Attributes:
        url: Final URL after following redirects.
        html: Decoded response body.
        status_code: HTTP status code.
        content_type: Lowercased content type without parameters.
        headers: Response headers (for CDN detection).
        raw_bytes: Raw response body when the content-type is application/pdf,
            so the caller can pass it directly to the PDF extractor without a
            second download.
    """

    url: str
    html: str
    status_code: int
    content_type: str
    headers: dict[str, str]
    raw_bytes: bytes | None = field(default=None)


async def fetch_static(
    url: str,
    *,
    timeout_ms: int,
    headers: dict[str, str] | None = None,
    accept_language: str | None = None,
    proxy: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> HttpFetchResult | None:
    """Fetch a URL with a single httpx GET, without a browser.

    Args:
        url: URL to fetch.
        timeout_ms: Total request timeout in milliseconds.
        headers: Caller-supplied headers (e.g. Authorization); these overlay the
            default browser-like headers and win on key collisions.
        accept_language: Accept-Language header value (typically derived from the
            scrape's locale config).
        proxy: Optional proxy URL passed straight to httpx.
        max_bytes: Maximum response body size to accept on the fast path.

    Returns:
        An :class:`HttpFetchResult` for HTML/PDF responses, or ``None`` when the
        fast path is not viable (network error, non-HTML/non-PDF content type,
        oversized body, or oversized PDF) and the caller should fall back to the
        browser.  For ``application/pdf`` responses, ``raw_bytes`` carries the
        downloaded bytes so the caller can route them to the PDF extractor
        without a second download.
    """
    request_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": accept_language or "en-US,en;q=0.9",
    }
    if headers:
        request_headers.update(headers)

    try:
        # follow_redirects=False: redirects are followed by hand inside
        # guarded_request so every hop — not just the first URL — is
        # validated and pinned to the address it resolved to (#152).
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_ms / 1000.0),
            proxy=proxy,
            headers=request_headers,
        ) as client:
            response = await guarded_request(client, "GET", url)
    except Exception as e:
        # Any transport failure simply disqualifies the fast path; the browser
        # path runs next and produces the real error if it also fails.
        LOGGER.debug("HTTP-first fetch failed for %s: %s", url, e)
        return None

    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()

    # PDFs are routed to the dedicated PDF extractor — carry the bytes through
    # rather than discarding them.  The PDF size limit is more generous than the
    # HTML cap; enforce it separately so real PDFs are not silently dropped.
    #
    # application/octet-stream: some servers (e.g. academic repositories)
    # declare a generic binary type instead of application/pdf; sniff the
    # magic bytes to confirm.
    #
    # Missing content-type (content_type == ""): pre-patch behaviour was to
    # fall through to the HTML path (the `if content_type and ...` guard below
    # was falsy for an empty string).  We preserve that by sniffing for PDF
    # magic bytes first; if the body IS a PDF we route it to the extractor
    # (strictly better than before), and if it is not we let it fall through
    # to the HTML path exactly as before — no regression.
    _is_pdf_content_type = content_type == "application/pdf"
    _is_octet_stream = content_type == "application/octet-stream"
    _missing_content_type = not content_type
    if _is_pdf_content_type or _is_octet_stream or _missing_content_type:
        # Cheap Content-Length pre-check: avoid buffering an oversized PDF
        # body only to discard it.  The post-read len() guard below is the
        # backstop for servers that omit or under-report Content-Length.
        content_length_header = response.headers.get("content-length", "")
        if content_length_header.strip().isdigit():
            declared_size = int(content_length_header.strip())
            if declared_size > MAX_PDF_SIZE:
                limit_mb = MAX_PDF_SIZE / (1024 * 1024)
                LOGGER.debug(
                    "HTTP-first skipped PDF for %s: Content-Length %d bytes exceeds %s MB cap (pre-read)",
                    url,
                    declared_size,
                    f"{limit_mb:.0f}",
                )
                return None

        body = response.content

        # For octet-stream, only carry bytes through if the body actually
        # contains the %PDF- signature within the sniff window.
        if _is_octet_stream and not is_pdf_bytes(body):
            LOGGER.debug("HTTP-first skipped for %s: octet-stream body is not a PDF", url)
            return None

        # For missing content-type: sniff for PDF magic bytes.  If not a PDF,
        # fall through to the HTML path below (restores pre-patch behaviour;
        # the `if content_type and ...` guard is falsy for empty strings).
        if _missing_content_type and not is_pdf_bytes(body):
            pass  # fall through to HTML path
        else:
            if len(body) > MAX_PDF_SIZE:
                size_mb = len(body) / (1024 * 1024)
                limit_mb = MAX_PDF_SIZE / (1024 * 1024)
                LOGGER.debug(
                    "HTTP-first skipped PDF for %s: %s MB exceeds %s MB cap",
                    url,
                    f"{size_mb:.1f}",
                    f"{limit_mb:.0f}",
                )
                return None
            LOGGER.debug("HTTP-first fetched PDF for %s (%d bytes)", url, len(body))
            return HttpFetchResult(
                url=str(response.url),
                html="",
                status_code=response.status_code,
                content_type=content_type,
                headers=dict(response.headers),
                raw_bytes=body,
            )

    # Markup and plain text are fast-pathable (a .txt, an llms.txt, a raw source
    # file is its own markdown); other content belongs to the browser.
    if content_type and "html" not in content_type and "xml" not in content_type and content_type != "text/plain":
        LOGGER.debug("HTTP-first skipped for %s: non-HTML content type %r", url, content_type)
        return None

    # response.content is a cached property in httpx — no second network call;
    # the bytes are identical to the read at line 140 for the missing-content-type
    # non-PDF fall-through path.  The re-read is intentional and free.
    body = response.content
    if len(body) > max_bytes:
        LOGGER.debug("HTTP-first skipped for %s: body %d bytes exceeds %d cap", url, len(body), max_bytes)
        return None

    return HttpFetchResult(
        url=str(response.url),
        html=response.text,
        status_code=response.status_code,
        content_type=content_type,
        headers=dict(response.headers),
    )
