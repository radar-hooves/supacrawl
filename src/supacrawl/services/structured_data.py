"""Deterministic extraction of embedded structured data — no LLM.

Many pages publish their own canonical facts: schema.org JSON-LD (Product,
Article, Recipe), framework hydration payloads (Next.js ``__NEXT_DATA__``),
HTML microdata, and OpenGraph tags. That data comes straight from the site's own
data layer, so it is more reliable than scraping the rendered DOM and free of any
model call. This module harvests all four sources with BeautifulSoup and json.
"""

import contextlib
import json
import logging
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from supacrawl.models import StructuredData

LOGGER = logging.getLogger(__name__)

# Tags whose URL-bearing attribute supplies a microdata itemprop value.
_URL_VALUE_TAGS = {
    "a": "href",
    "area": "href",
    "link": "href",
    "img": "src",
    "audio": "src",
    "video": "src",
    "source": "src",
    "embed": "src",
    "track": "src",
    "iframe": "src",
    "object": "data",
}


def extract_structured_data(html: str, *, base_url: str | None = None) -> StructuredData:
    """Harvest embedded structured data from a page.

    Args:
        html: Raw HTML of the page.
        base_url: Base URL for resolving relative microdata URL values.

    Returns:
        A :class:`StructuredData` with each source populated when present and
        None otherwise.
    """
    soup = BeautifulSoup(html, "html.parser")
    return StructuredData(
        json_ld=_extract_json_ld(soup) or None,
        microdata=_extract_microdata(soup, base_url) or None,
        opengraph=_extract_opengraph(soup) or None,
        next_data=_extract_next_data(soup) or None,
    )


def has_product_offer(data: StructuredData, page_url: str) -> bool:
    """Require a priced offer tied to this URL before trusting a throttled body."""
    page = urlsplit(page_url)
    for node in data.json_ld or []:
        if not isinstance(node, dict):
            continue
        types = node.get("@type")
        if "Product" not in (types if isinstance(types, list) else [types]):
            continue
        offers = node.get("offers")
        for offer in offers if isinstance(offers, list) else [offers]:
            if not isinstance(offer, dict) or all(offer.get(key) is None for key in ("price", "lowPrice")):
                continue
            url = offer.get("url") or node.get("url")
            if isinstance(url, str) and url:
                with contextlib.suppress(ValueError):
                    target = urlsplit(urljoin(page_url, url))
                    if target.hostname == page.hostname and target.path.rstrip("/") == page.path.rstrip("/"):
                        return True
    return False


def _extract_json_ld(soup: BeautifulSoup) -> list[Any]:
    """Collect schema.org JSON-LD objects, flattening any ``@graph`` arrays."""
    objects: list[Any] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.get_text(strip=False)
        if not text or not text.strip():
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            LOGGER.debug("Skipping malformed JSON-LD block: %s", e)
            continue
        objects.extend(_flatten_json_ld(data))
    return objects


def _flatten_json_ld(data: Any) -> list[Any]:
    """Normalise a parsed JSON-LD block into a flat list of objects.

    A block may be a single object, a list of objects, or an object wrapping a
    ``@graph`` array; all collapse to a flat list so callers see one shape.
    """
    if isinstance(data, list):
        result: list[Any] = []
        for item in data:
            result.extend(_flatten_json_ld(item))
        return result
    if isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            # Recurse so a nested @graph (some complex schema.org payloads emit
            # one) is also flattened rather than left as an opaque dict.
            result = []
            for item in graph:
                result.extend(_flatten_json_ld(item))
            return result
        return [data]
    return [data]


def _extract_next_data(soup: BeautifulSoup) -> dict[str, Any] | None:
    """Parse the Next.js ``__NEXT_DATA__`` hydration payload, if present."""
    script = soup.find("script", attrs={"id": "__NEXT_DATA__"})
    if not isinstance(script, Tag):
        return None
    text = script.get_text(strip=False)
    if not text or not text.strip():
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        LOGGER.debug("Skipping malformed __NEXT_DATA__: %s", e)
        return None
    return data if isinstance(data, dict) else None


def _extract_opengraph(soup: BeautifulSoup) -> dict[str, str]:
    """Collect all ``og:*`` meta properties into a flat dict."""
    og: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        if not isinstance(meta, Tag):
            continue
        prop = meta.get("property")
        if isinstance(prop, str) and prop.startswith("og:"):
            content = meta.get("content")
            if isinstance(content, list):
                content = content[0] if content else None
            if isinstance(content, str):
                # Keep the first occurrence; og arrays (e.g. og:image) repeat keys.
                og.setdefault(prop, content)
    return og


def _extract_microdata(soup: BeautifulSoup, base_url: str | None) -> list[dict[str, Any]]:
    """Parse top-level microdata items (itemscope/itemprop) into nested dicts."""
    items: list[dict[str, Any]] = []
    for el in soup.select("[itemscope]"):
        # Only top-level items; nested items are captured via their parent.
        if any(isinstance(parent, Tag) and parent.has_attr("itemscope") for parent in el.parents):
            continue
        items.append(_parse_microdata_item(el, base_url))
    return items


def _parse_microdata_item(scope: Tag, base_url: str | None) -> dict[str, Any]:
    """Build a microdata item dict from an itemscope element."""
    item: dict[str, Any] = {}
    itemtype = scope.get("itemtype")
    if isinstance(itemtype, list):
        itemtype = " ".join(itemtype)
    if isinstance(itemtype, str) and itemtype:
        item["type"] = itemtype

    props: dict[str, Any] = {}
    _collect_microdata_props(scope, props, base_url)
    if props:
        item["properties"] = props
    return item


def _collect_microdata_props(current: Tag, props: dict[str, Any], base_url: str | None) -> None:
    """Walk direct children, recording itemprops and recursing into nested scopes.

    Recursion stops at a nested ``itemscope`` (it owns its own properties), which
    keeps each item's property set correct rather than flattening descendants.
    """
    for child in current.children:
        if not isinstance(child, Tag):
            continue
        name = child.get("itemprop")
        prop_name = name[0] if isinstance(name, list) and name else (name if isinstance(name, str) else None)

        if child.get("itemscope") is not None:
            value: Any = _parse_microdata_item(child, base_url)
            if prop_name:
                _add_microdata_prop(props, prop_name, value)
            # Do not descend — the nested scope owns its properties.
            continue

        if prop_name:
            _add_microdata_prop(props, prop_name, _microdata_value(child, base_url))
        _collect_microdata_props(child, props, base_url)


def _add_microdata_prop(props: dict[str, Any], name: str, value: Any) -> None:
    """Record a property, promoting repeated names to a list."""
    if name in props:
        existing = props[name]
        if isinstance(existing, list):
            existing.append(value)
        else:
            props[name] = [existing, value]
    else:
        props[name] = value


def _microdata_value(el: Tag, base_url: str | None) -> str | None:
    """Resolve a microdata itemprop element to its value per the HTML spec."""
    if el.name == "meta":
        content = el.get("content")
        return content[0] if isinstance(content, list) else content
    url_attr = _URL_VALUE_TAGS.get(el.name or "")
    if url_attr:
        raw = el.get(url_attr)
        raw = raw[0] if isinstance(raw, list) else raw
        if isinstance(raw, str) and base_url:
            return urljoin(base_url, raw)
        return raw
    if el.name == "time":
        datetime_attr = el.get("datetime")
        datetime_attr = datetime_attr[0] if isinstance(datetime_attr, list) else datetime_attr
        if isinstance(datetime_attr, str):
            return datetime_attr
    if el.name in ("data", "meter"):
        value_attr = el.get("value")
        value_attr = value_attr[0] if isinstance(value_attr, list) else value_attr
        if isinstance(value_attr, str):
            return value_attr
    text = el.get_text(strip=True)
    return text or None
