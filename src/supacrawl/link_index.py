"""Read the links of a page that exists to link to other pages.

A shareholder-letters page, a memo archive, a documentation index or a release list is a page of links:
its text is short, its value is where it points. :func:`extract_page_links` returns every link in the HTML with
what says what it is (anchor text, the text of its own block, the nearest heading or label before it) and whether
the site's chrome (navigation, banner, search, a header or footer outside any article) holds it or the page's
content does. :func:`is_link_index` says whether the content holds enough links to call the page an index.

Pure and offline: nothing here fetches a link.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from supacrawl.models import PageLink

#: Distinct content links, each with anchor text, at which a page is a link index.
MIN_INDEX_LINKS = 5
#: Characters kept of a link's own block and of its anchor text.
CONTEXT_CHARS = 200

_CHROME_TAGS = frozenset({"nav"})
_PAGE_CHROME_TAGS = frozenset({"header", "footer", "aside"})
_SECTIONING = frozenset({"article", "section", "main", "aside", "nav"})
_CHROME_ROLES = frozenset({"navigation", "banner", "contentinfo", "search"})
_DROPPED = ("script", "style", "noscript", "template", "svg")
_LABELS = ("h1", "h2", "h3", "h4", "h5", "h6", "button", "summary", "legend", "caption", "th", "dt", "label")
_QUOTED_URL = re.compile(r"""['"]((?:https?://|/)[^'"\s]+)['"]""")
_NOT_PAGES = frozenset({"mailto", "tel", "data", "sms"})


def _squash(text: str) -> str:
    return " ".join(text.split())


def _href(raw: str) -> str | None:
    """The address a link's href carries: itself, or the quoted URL inside a ``javascript:`` call."""
    href = raw.strip()
    if href.lower().startswith("javascript:"):
        found = _QUOTED_URL.search(href)
        return found.group(1) if found else None
    if not href or href.startswith("#") or href.split(":", 1)[0].lower() in _NOT_PAGES:
        return None
    return href


def _in_chrome(element: Tag) -> bool:
    """Inside navigation, a navigational role, an aside, or a header or footer that is not an article's own."""
    for ancestor in element.parents:
        if not isinstance(ancestor, Tag):
            continue
        if ancestor.name in _CHROME_TAGS or str(ancestor.get("role") or "").lower() in _CHROME_ROLES:
            return True
        if ancestor.name in _PAGE_CHROME_TAGS and (
            ancestor.name == "aside"
            or ancestor.find_parent(lambda tag: isinstance(tag, Tag) and tag.name in _SECTIONING) is None
        ):
            return True
    return False


def _own_block(link: Tag) -> str:
    """The text of the largest element that holds this link and no other."""
    block: Tag = link
    for ancestor in link.parents:
        if not isinstance(ancestor, Tag) or ancestor.name in ("body", "html", "[document]"):
            break
        if len(ancestor.find_all(["a", "area"], href=True, limit=2)) > 1:
            break
        block = ancestor
    return _squash(block.get_text(" "))[:CONTEXT_CHARS]


def _heading_before(link: Tag) -> str | None:
    """The text of the nearest heading or label that precedes the link in the page and does not hold it."""
    ancestors = {id(parent) for parent in link.parents}
    label = link.find_previous(_LABELS)
    while label is not None:
        if id(label) not in ancestors and (text := _squash(label.get_text(" "))):
            return text[:CONTEXT_CHARS]
        label = label.find_previous(_LABELS)
    return None


def extract_page_links(html: str, page_url: str) -> list[PageLink]:
    """Every link in the HTML, once per address, in page order.

    A link seen in the chrome and again in the content is a content link. A link to the page itself, to a bare
    fragment, or to a non-web scheme is left out; so is one whose href is a ``javascript:`` call that quotes no URL.

    Args:
        html: The page's HTML as served or rendered.
        page_url: The page's address, which resolves relative hrefs (a ``<base href>`` overrides it).

    Returns:
        One :class:`PageLink` per distinct resolved address.
    """
    soup = BeautifulSoup(html, "html.parser")
    for dropped in soup(_DROPPED):
        dropped.decompose()
    base = soup.find("base", href=True)
    base_url = urljoin(page_url, str(base["href"])) if isinstance(base, Tag) else page_url
    own = page_url.split("#", 1)[0]
    found: dict[str, PageLink] = {}
    texts: dict[str, list[str]] = {}
    for link in soup.find_all(["a", "area"], href=True):
        href = _href(str(link["href"]))
        if href is None:
            continue
        url = urljoin(base_url, href).split("#", 1)[0]
        if urlsplit(url).scheme not in ("http", "https") or url == own:
            continue
        chrome = _in_chrome(link)
        text = _squash(link.get_text(" ")) or _squash(str(link.get("title") or ""))
        if not text and (image := link.find("img", alt=True)) is not None:
            text = _squash(str(image["alt"]))
        held = found.get(url)
        if held is not None and not (held.chrome and not chrome):
            if text and text not in texts[url]:
                texts[url].append(text)
            continue
        # First sighting, or the first content sighting of an address seen so far only in the chrome.
        found[url] = PageLink(
            url=url,
            text="",
            context=_own_block(link),
            heading=_heading_before(link),
            chrome=chrome,
        )
        texts[url] = [text] if text else []
    return [link.model_copy(update={"text": " / ".join(texts[url])[:CONTEXT_CHARS]}) for url, link in found.items()]


def is_link_index(links: list[PageLink]) -> bool:
    """Whether the content holds enough distinct linked pages, each named by its anchor, to be an index."""
    return sum(1 for link in links if not link.chrome and link.text) >= MIN_INDEX_LINKS


__all__ = ["MIN_INDEX_LINKS", "extract_page_links", "is_link_index"]
