"""Measured image sizes for a page, so a caller can tell the photograph from the icons.

``extract_images`` lists every URL on a page; this module says how big each one
really is. Candidates come from the HTML (``<img>``, ``srcset``, ``<picture>``,
``og:image``, and links to image files, which is where a site publishes its
"original file"). Each is then sized from the first bytes of the file itself,
since a width attribute or a ``1920px-`` thumbnail name describes the
rendering, not the original.
"""

import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from PIL import ImageFile

from supacrawl.models import ImageDetail
from supacrawl.services.http_fetch import DEFAULT_USER_AGENT
from supacrawl.services.url_guard import guarded_stream
from supacrawl.utils import gather_results

LOGGER = logging.getLogger(__name__)

# The size sits in the file header, but a JPEG's EXIF and embedded preview can push it past 64 KB.
HEADER_BYTES = 524288
MAX_PROBES = 40
PROBE_CONCURRENCY = 8
PROBE_TIMEOUT_S = 8.0
# Below this on the long side an image is a UI icon, not a photograph.
ICON_MAX_SIDE = 128

_IMAGE_PATH = re.compile(r"\.(?:jpe?g|png|webp|gif|avif|tiff?|bmp)$", re.IGNORECASE)
_ICON_HINTS = ("favicon", "sprite", "/icons/", "icon-", "-icon", "/static/images/", "1x1", "pixel", "tracking")
_THUMB_WIDTH = re.compile(r"/(\d+)px-[^/]*$")
_SIZE_ATTR = re.compile(r"^\s*(\d+)")
_CONTENT_RANGE_TOTAL = re.compile(r"/(\d+)\s*$")


def _int_attr(value: object) -> int | None:
    match = _SIZE_ATTR.match(value) if isinstance(value, str) else None
    return int(match.group(1)) if match else None


def _is_candidate(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and not parsed.path.lower().endswith((".svg", ".ico"))


def collect_candidates(html: str, base_url: str) -> dict[str, int | None]:
    """Candidate image URLs on a page, each with a width hint when the HTML gives one."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, int | None] = {}

    def add(raw: object, hint: int | None = None) -> None:
        if not isinstance(raw, str) or not raw.strip() or raw.startswith("data:"):
            return
        url = urljoin(base_url, raw.strip())
        if not _is_candidate(url):
            return
        thumb = _THUMB_WIDTH.search(urlparse(url).path)
        hint = max(hint or 0, int(thumb.group(1)) if thumb else 0) or None
        found[url] = max(hint or 0, found.get(url) or 0) or None

    def add_srcset(srcset: object) -> None:
        if not isinstance(srcset, str):
            return
        for part in srcset.split(","):
            pieces = part.strip().split()
            if not pieces:
                continue
            width = int(pieces[1][:-1]) if len(pieces) > 1 and re.fullmatch(r"\d+w", pieces[1]) else None
            add(pieces[0], width)

    for img in soup.find_all("img"):
        add(img.get("src"), _int_attr(img.get("width")))
        add_srcset(img.get("srcset"))
    for source in soup.find_all("source"):
        add(source.get("src"))
        add_srcset(source.get("srcset"))
    for meta in soup.find_all("meta"):
        if meta.get("property") in ("og:image", "og:image:url") or meta.get("name") == "twitter:image":
            add(meta.get("content"))
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if isinstance(href, str) and _IMAGE_PATH.search(urlparse(href).path):
            add(href)
    return found


async def _probe(client: httpx.AsyncClient, url: str, hint: int | None, semaphore: asyncio.Semaphore) -> ImageDetail:
    """Size one image from its first bytes; an unreadable one keeps only its hint."""
    detail = ImageDetail(url=url, width=hint)
    async with semaphore:
        try:
            async with guarded_stream(client, "GET", url) as response:
                if response.status_code >= 400:
                    return detail
                total = _CONTENT_RANGE_TOTAL.search(response.headers.get("content-range", ""))
                length = response.headers.get("content-length")
                if response.status_code == 200 and length and length.isdigit():
                    detail.bytes = int(length)
                elif total:
                    detail.bytes = int(total.group(1))
                parser = ImageFile.Parser()
                fed = 0
                async for chunk in response.aiter_bytes():
                    parser.feed(chunk)
                    fed += len(chunk)
                    if parser.image is not None or fed >= HEADER_BYTES:
                        break
            if parser.image is not None:
                detail.width, detail.height = parser.image.size
        except Exception as exc:
            LOGGER.debug("Image size probe failed for %s: %s", url, exc)
    return detail


async def measure_images(html: str, base_url: str) -> list[ImageDetail]:
    """Sized images on a page, largest first, with site icons flagged ``icon``."""
    candidates = collect_candidates(html, base_url)
    # Unhinted and large-hinted candidates first: a 60px thumbnail is not worth a probe.
    ordered = sorted(candidates.items(), key=lambda kv: -(kv[1] or 10**6))[:MAX_PROBES]
    semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(PROBE_TIMEOUT_S),
        headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": base_url, "Range": f"bytes=0-{HEADER_BYTES - 1}"},
    ) as client:
        details = await gather_results(_probe(client, url, hint, semaphore) for url, hint in ordered)
    for detail in details:
        side = max(detail.width or 0, detail.height or 0)
        detail.icon = any(hint in detail.url.lower() for hint in _ICON_HINTS) or (0 < side < ICON_MAX_SIDE)
    return sorted(details, key=lambda d: (d.icon, -((d.width or 0) * (d.height or d.width or 0))))
