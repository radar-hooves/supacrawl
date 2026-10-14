"""HTML to Markdown converter with high-quality output.

Uses a pure Playwright + markdownify approach:
1. Playwright renders the page fully (JavaScript execution)
2. BeautifulSoup cleans the HTML (removes boilerplate)
3. markdownify converts to markdown (preserves tables and structure)

This approach treats each page like an actual browser, ensuring JavaScript-rendered
content is captured and table structure is preserved.

Site-Specific Preprocessors
===========================
Some documentation frameworks produce HTML that converts poorly to markdown.
We maintain a registry of site-specific preprocessors that improve output quality.

To add a new preprocessor:
1. Create a detection function: _detect_<name>(soup) -> bool
2. Create a handler function: _preprocess_<name>(soup) -> None
3. Register in SITE_PREPROCESSORS with documentation

See SITE_PREPROCESSORS below for the current registry.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter as BaseMarkdownConverter

LOGGER = logging.getLogger(__name__)


# =============================================================================
# Site-Specific Preprocessor Registry
# =============================================================================


@dataclass
class SitePreprocessor:
    """Registration for a site-specific HTML preprocessor.

    Attributes:
        name: Short identifier (e.g., "mkdocs_material")
        description: What this preprocessor handles
        examples: Example sites using this framework
        detect: Function to check if this preprocessor applies
        preprocess: Function to transform the HTML
    """

    name: str
    description: str
    examples: list[str]
    detect: Callable[[BeautifulSoup], bool]
    preprocess: Callable[[BeautifulSoup], None]


def _detect_mkdocs_material(soup: BeautifulSoup) -> bool:
    """Detect if the page is built with MkDocs Material theme.

    Checks for characteristic MkDocs Material markers:
    - md-content class (main content wrapper)
    - data-md-* attributes (Material Design data attributes)
    - Combination of admonition + headerlink classes
    """
    # Check for MkDocs Material specific classes
    if soup.select_one(".md-content, .md-main, [data-md-component]"):
        return True

    # Check for combination of MkDocs-specific elements
    has_headerlinks = bool(soup.select_one("a.headerlink"))
    has_admonitions = bool(soup.select_one("div.admonition"))
    has_tabbed = bool(soup.select_one("div.tabbed-set"))
    has_highlighttable = bool(soup.select_one("table.highlighttable"))

    # If we see multiple MkDocs patterns, it's likely MkDocs
    mkdocs_indicators = sum([has_headerlinks, has_admonitions, has_tabbed, has_highlighttable])
    return mkdocs_indicators >= 2


def _detect_css_counter_lists(soup: BeautifulSoup) -> bool:
    """Detect if the page uses CSS counter-based lists.

    Checks for <p> elements with data-list-level attributes, which are used
    by some documentation sites instead of native <ol>/<li> elements.
    """
    return bool(soup.select("p[data-list-level]"))


def _preprocess_css_counter_lists(soup: BeautifulSoup) -> None:
    """Preprocess CSS counter-based lists for better markdown conversion.

    Converts <p> elements with data-list-level attributes to proper <ol>/<li> elements.
    Handles nested hierarchies based on level values.

    Args:
        soup: BeautifulSoup object to modify in-place
    """
    list_items = soup.select("p[data-list-level]")
    if not list_items:
        return

    # Process consecutive groups of list items
    i = 0
    while i < len(list_items):
        # Find consecutive items (allowing for some non-list elements in between)
        group = [list_items[i]]
        current = list_items[i]

        for j in range(i + 1, len(list_items)):
            next_item = list_items[j]
            # Check if next_item follows current within a reasonable distance
            if _is_within_proximity(current, next_item, max_distance=10):
                group.append(next_item)
                current = next_item
            else:
                break

        # Convert this group to nested lists
        if group:
            _build_nested_list(soup, group)
            i += len(group)
        else:
            i += 1


def _is_within_proximity(elem1, elem2, max_distance: int = 10) -> bool:
    """Check if two elements are within proximity in the DOM.

    Args:
        elem1: First element
        elem2: Second element
        max_distance: Maximum number of siblings to check

    Returns:
        True if elem2 is within max_distance siblings of elem1
    """
    current = elem1
    for _ in range(max_distance):
        current = current.find_next_sibling()
        if current is None:
            return False
        if current == elem2:
            return True
    return False


def _get_list_level(item: Tag, default: int = 1) -> int:
    """Safely extract list level from data-list-level attribute.

    Args:
        item: BeautifulSoup Tag element
        default: Default level if parsing fails

    Returns:
        Integer level value, or default if invalid
    """
    try:
        level_str = str(item.get("data-list-level") or str(default))
        return int(level_str)
    except ValueError, TypeError:
        return default


def _build_nested_list(soup: BeautifulSoup, items: list[Tag]) -> None:
    """Build a nested list structure from CSS counter list items.

    Args:
        soup: BeautifulSoup object for creating new tags
        items: List of <p> elements with data-list-level attributes
    """
    if not items:
        return

    # Track current list at each level
    lists_by_level: dict[int, Tag] = {}
    last_li_by_level: dict[int, Tag] = {}

    # Find the root level (minimum level in the group)
    root_level = min(_get_list_level(item) for item in items)

    # Create root list
    root_list = soup.new_tag("ol")
    lists_by_level[root_level] = root_list
    anchor = items[0]  # We'll replace this element with the root list

    for item in items:
        level = _get_list_level(item)

        # Get or create the list for this level
        if level not in lists_by_level:
            # Need to create a nested list
            # Find the parent level (highest level less than current)
            parent_level = max(lvl for lvl in lists_by_level.keys() if lvl < level)
            parent_li = last_li_by_level.get(parent_level)

            # Create new nested list
            nested_list = soup.new_tag("ol")
            if parent_li is not None:
                parent_li.append(nested_list)
            else:
                # Fallback: append to parent list
                lists_by_level[parent_level].append(nested_list)

            lists_by_level[level] = nested_list

            # Clean up deeper levels that are now invalid
            for lvl in list(lists_by_level.keys()):
                if lvl > level:
                    del lists_by_level[lvl]
                    if lvl in last_li_by_level:
                        del last_li_by_level[lvl]

        # Create the list item
        li = soup.new_tag("li")

        # Move content from <p> to <li>
        for child in list(item.children):
            li.append(child.extract())

        # Append to the appropriate list
        lists_by_level[level].append(li)
        last_li_by_level[level] = li

        # Clean up levels deeper than current
        for lvl in list(lists_by_level.keys()):
            if lvl > level:
                del lists_by_level[lvl]
                if lvl in last_li_by_level:
                    del last_li_by_level[lvl]

    # Replace the first item with the complete list structure
    anchor.replace_with(root_list)

    # Remove the other items (their content has been moved)
    for item in items[1:]:
        item.decompose()


def _detect_wordpress(soup: BeautifulSoup) -> bool:
    """Detect if the page is a WordPress site.

    Detection signals:
    - Classes with wp- prefix (wp-content, wp-block, etc.)
    - Post-related classes (post-, hentry, entry-content)
    - WordPress meta generator tag

    Args:
        soup: BeautifulSoup object to analyze

    Returns:
        True if WordPress site detected, False otherwise
    """
    # Check for wp- prefixed classes
    if soup.select("[class*='wp-']"):
        return True

    # Check for post-related classes
    if soup.select("[class*='post-'], .hentry, .entry-content"):
        return True

    # Check for WordPress meta generator
    meta_gen = soup.find("meta", attrs={"name": "generator"})
    if meta_gen and "wordpress" in str(meta_gen.get("content") or "").lower():
        return True

    return False


def _preprocess_wordpress(soup: BeautifulSoup) -> None:
    """Preprocess WordPress HTML for better markdown conversion.

    Handles WordPress-specific elements:
    - Preserves page title H1 by moving it into main content
    - Fixed navigation elements (.fixed-nav, .fixed-nav-prev, .fixed-nav-next)
    - Post navigation (.post-navigation, .nav-links)
    - Share widgets (.share-simple-wrapper, .sharedaddy)
    - Related posts sections (.related-posts, .section-post-related)

    Args:
        soup: BeautifulSoup object to modify in-place
    """
    # Preserve page title: move H1 from header into main content
    # Look for H1 in common WordPress header locations
    title_h1 = None
    for selector in ["#Subheader h1.title", ".entry-title", ".page-title", "header h1"]:
        title_h1 = soup.select_one(selector)
        if title_h1:
            break

    if title_h1:
        # Find main content area to prepend the title
        main_content = (
            soup.find("main") or soup.find("article") or soup.find(id="Content") or soup.find(class_="entry-content")
        )

        if main_content:
            # Extract the title and prepend it to main content
            title_copy = soup.new_tag("h1")
            title_copy.string = title_h1.get_text(strip=True)
            main_content.insert(0, title_copy)

    # Remove fixed navigation (common in BeTheme and similar themes)
    for selector in [".fixed-nav", ".fixed-nav-prev", ".fixed-nav-next"]:
        for elem in soup.select(selector):
            elem.decompose()

    # Remove post navigation
    for selector in [".post-navigation", ".nav-links", ".post-pager"]:
        for elem in soup.select(selector):
            elem.decompose()

    # Remove share widgets
    for selector in [".share-simple-wrapper", ".sharedaddy", ".social-share"]:
        for elem in soup.select(selector):
            elem.decompose()

    # Remove related posts sections
    for selector in [".related-posts", ".section-post-related", ".yarpp-related"]:
        for elem in soup.select(selector):
            elem.decompose()

    # Remove rating/feedback forms
    for selector in [".rich-reviews", ".feedback-form", "[class*='rating']", "[class*='review-form']"]:
        for elem in soup.select(selector):
            elem.decompose()

    # Remove images with data:image/svg placeholder (lazy loading placeholders)
    for img in soup.find_all("img", src=lambda x: x and x.startswith("data:image/svg+xml")):
        img.decompose()


def _preprocess_mkdocs_material(soup: BeautifulSoup) -> None:
    """Preprocess MkDocs Material HTML for better markdown conversion.

    Handles MkDocs Material-specific elements:
    - Strip permalink anchors from headings (headerlink class)
    - Convert line-numbered code tables to proper code blocks
    - Convert admonitions to blockquotes with bold titles
    - Add clear separators for tabbed content

    Args:
        soup: BeautifulSoup object to modify in-place
    """
    # 1. Strip permalink anchors from headings
    for anchor in soup.select("a.headerlink"):
        anchor.decompose()

    # 2. Convert line-numbered code tables to proper code blocks
    for table in soup.select("table.highlighttable"):
        # Find the code cell
        code_cell = table.select_one("td.code")
        if code_cell:
            # Find the code element
            code_elem = code_cell.select_one("code")
            if code_elem:
                # Get the text content, preserving line breaks
                code_text = code_elem.get_text()

                # Try to detect language from parent class
                lang = ""
                highlight_div = table.find_parent("div", class_="highlight")
                if highlight_div:
                    for cls in highlight_div.get("class") or []:
                        # Classes like "language-python" or just language names
                        if cls.startswith("language-"):
                            lang = cls.replace("language-", "")
                            break

                # Create new pre/code structure
                new_pre = soup.new_tag("pre")
                new_code = soup.new_tag("code")
                if lang:
                    new_code["class"] = f"language-{lang}"
                new_code.string = code_text
                new_pre.append(new_code)

                # Replace the table with the new pre block
                table.replace_with(new_pre)

    # 3. Convert admonitions to blockquotes with bold titles
    for admonition in soup.select("div.admonition"):
        # Get the type (note, warning, tip, example, etc.)
        admon_type = "Note"
        for cls in admonition.get("class") or []:
            if cls != "admonition":
                admon_type = cls.capitalize()
                break

        # Get the title if present
        title_elem = admonition.select_one("p.admonition-title")
        title_text = title_elem.get_text(strip=True) if title_elem else admon_type
        if title_elem:
            title_elem.decompose()

        # Get remaining content
        content_parts = []
        for child in admonition.children:
            if hasattr(child, "get_text"):
                text = child.get_text(strip=True)
                if text:
                    content_parts.append(text)
            elif isinstance(child, str) and child.strip():
                content_parts.append(child.strip())

        content = " ".join(content_parts)

        # Create blockquote with bold title
        blockquote = soup.new_tag("blockquote")
        title_p = soup.new_tag("p")
        strong = soup.new_tag("strong")
        strong.string = f"{title_text}:"
        title_p.append(strong)
        title_p.append(f" {content}")
        blockquote.append(title_p)

        admonition.replace_with(blockquote)

    # 4. Handle tabbed content - add language/tab headers
    for tabbed_set in soup.select("div.tabbed-set"):
        # Get tab labels
        labels = []
        for label_elem in tabbed_set.select("div.tabbed-labels label"):
            label_text = label_elem.get_text(strip=True)
            if label_text:
                labels.append(label_text)

        # Get tab content blocks
        tab_blocks = tabbed_set.select("div.tabbed-block")

        # Create a container for the processed tabs
        container = soup.new_tag("div")

        for i, block in enumerate(tab_blocks):
            # Add a header for each tab
            label = labels[i] if i < len(labels) else f"Tab {i + 1}"
            header = soup.new_tag("h4")
            header.string = label
            container.append(header)

            # Append the block content
            for child in list(block.children):
                container.append(child.extract())

        tabbed_set.replace_with(container)


# =============================================================================
# Preprocessor Registry
# =============================================================================

SITE_PREPROCESSORS: list[SitePreprocessor] = [
    SitePreprocessor(
        name="mkdocs_material",
        description=(
            "MkDocs with Material theme. Handles: permalink anchors in headings, "
            "line-numbered code tables, admonition blocks, tabbed content panels."
        ),
        examples=[
            "help.ctrader.com",
            "fastapi.tiangolo.com",
            "docs.pydantic.dev",
            "squidfunk.github.io/mkdocs-material",
        ],
        detect=_detect_mkdocs_material,
        preprocess=_preprocess_mkdocs_material,
    ),
    SitePreprocessor(
        name="css_counter_lists",
        description=(
            "CSS counter-based lists. Handles: <p> elements with data-list-level attributes "
            "used instead of native <ol>/<li> elements, preserves hierarchy and nesting."
        ),
        examples=[
            "Sites using CSS counters for list styling",
        ],
        detect=_detect_css_counter_lists,
        preprocess=_preprocess_css_counter_lists,
    ),
    SitePreprocessor(
        name="wordpress",
        description=(
            "WordPress sites (43% of the web). Handles: fixed navigation (.fixed-nav), "
            "post navigation (.post-navigation), share widgets (.share-simple-wrapper), "
            "related posts sections (.section-post-related)."
        ),
        examples=[
            "Any WordPress site with BeTheme, Divi, Avada, or similar themes",
        ],
        detect=_detect_wordpress,
        preprocess=_preprocess_wordpress,
    ),
    # Add new preprocessors here following the same pattern:
    # SitePreprocessor(
    #     name="sphinx_rtd",
    #     description="Sphinx with ReadTheDocs theme. Handles: ...",
    #     examples=["readthedocs.io sites"],
    #     detect=_detect_sphinx_rtd,
    #     preprocess=_preprocess_sphinx_rtd,
    # ),
]


def apply_site_preprocessors(soup: BeautifulSoup) -> list[str]:
    """Apply all matching site-specific preprocessors.

    Iterates through registered preprocessors, detects which ones apply,
    and runs their preprocessing functions.

    Args:
        soup: BeautifulSoup object to preprocess in-place

    Returns:
        List of preprocessor names that were applied
    """
    applied = []
    for preprocessor in SITE_PREPROCESSORS:
        try:
            if preprocessor.detect(soup):
                LOGGER.debug(f"Detected {preprocessor.name}, applying preprocessor")
                preprocessor.preprocess(soup)
                applied.append(preprocessor.name)
        except Exception as e:
            LOGGER.warning(f"Preprocessor {preprocessor.name} failed: {e}")
    return applied


_MARKUP_ATTR = re.compile(r"\s*<[a-zA-Z][^>]*>")
_MAX_EXPANSION_PASSES = 4


def expand_markup_attributes(soup: BeautifulSoup) -> int:
    """Move HTML carried in a custom element's attribute into its children.

    Component frameworks (PACMAN's ``<def-content content="<p>…</p>">``) ship the
    body as an attribute value, so a DOM-to-markdown pass sees only the headings
    around it. Only a hyphenated (custom) element with no text of its own, and
    only an attribute whose value opens with a tag, qualifies; titles, data blobs
    and plain-text attributes are never injected. Returns the elements expanded.
    """
    expanded = 0
    for _ in range(_MAX_EXPANSION_PASSES):
        changed = False
        for el in soup.find_all(lambda t: "-" in t.name):
            if el.get_text(strip=True):
                continue
            for value in el.attrs.values():
                if not isinstance(value, str) or not _MARKUP_ATTR.match(value):
                    continue
                fragment = BeautifulSoup(value, "html.parser")
                if not fragment.get_text(strip=True) and not fragment.find(["img", "table"]):
                    continue
                for child in list(fragment.contents):
                    el.append(child.extract())
                expanded += 1
                changed = True
                break
        if not changed:
            break
    return expanded


class AbsoluteUrlConverter(BaseMarkdownConverter):
    """Markdownify converter that resolves relative URLs to absolute.

    Key features:
    - Resolves relative URLs to absolute
    - Preserves table structure with proper markdown formatting
    - Handles links inside table cells correctly
    """

    def __init__(self, base_url: str | None = None, **kwargs):
        """Initialize with base URL for resolving relative links."""
        super().__init__(**kwargs)
        self.base_url = base_url

    def convert_a(self, el, text, parent_tags):
        """Convert anchor tags, resolving relative URLs.

        Strips javascript: pseudo-protocol links (UI interactions with no semantic meaning).
        """
        href = el.get("href", "")
        title = el.get("title", "")

        # Clean up text - preserve content even if whitespace-only
        text = text.strip() if text else ""
        if not text:
            # Try to get text from nested elements
            text = el.get_text(strip=True)
        if not text:
            return ""

        # Strip javascript: pseudo-protocol links (case-insensitive) - remove entirely
        # These are UI controls (print, share, etc.) with no semantic content value
        if href.lower().strip().startswith("javascript:"):
            return ""

        # Resolve relative URL to absolute
        if href and self.base_url and not urlparse(href).netloc:
            href = urljoin(self.base_url, href)

        if title:
            return f'[{text}]({href} "{title}")'
        return f"[{text}]({href})"

    def convert_img(self, el, text, parent_tags):
        """Convert image tags, resolving relative URLs."""
        src = el.get("src", "")
        alt = el.get("alt", "")
        title = el.get("title", "")

        # Resolve relative URL to absolute
        if src and self.base_url and not urlparse(src).netloc:
            src = urljoin(self.base_url, src)

        if not src:
            return ""

        if title:
            return f'![{alt}]({src} "{title}")'
        return f"![{alt}]({src})"


class MarkdownConverter:
    """Convert HTML to clean markdown.

    Uses a pure Playwright + markdownify approach that preserves table structure
    and handles JavaScript-rendered content properly.

    The extraction process:
    1. Clean HTML by removing boilerplate (scripts, styles, nav, footer, etc.)
    2. Find main content area using CSS selectors
    3. Convert to markdown using markdownify (preserves tables)

    Usage:
        converter = MarkdownConverter()
        markdown = converter.convert(html, base_url="https://example.com")
    """

    # Tags to remove completely
    REMOVE_TAGS = [
        "script",
        "style",
        "noscript",
        "iframe",
        "svg",
        "path",
        "canvas",
        "video",
        "audio",
        "source",
        "track",
        "embed",
        "object",
        "param",
        "template",
    ]

    # Tags to remove only when cleaning for main content
    BOILERPLATE_TAGS = [
        "nav",
        "footer",
        "header",
        "aside",
    ]

    # CSS selectors for boilerplate removal
    BOILERPLATE_SELECTORS = [
        # Navigation patterns
        "[role='navigation']",
        "[role='banner']",
        "[role='contentinfo']",
        ".navigation",
        ".nav-menu",
        ".navbar",
        ".menu",
        ".sidebar",
        ".toc",
        ".table-of-contents",
        # Footer patterns
        "[class*='site-footer']",
        "[id*='site-footer']",
        ".footer",
        "#footer",
        # Cookie/consent patterns
        "[class*='cookie']",
        "[id*='cookie']",
        "[class*='consent']",
        "[id*='consent']",
        "[class*='gdpr']",
        "[class*='privacy-banner']",
        # Popup/modal patterns
        "[class*='popup']",
        "[class*='modal']",
        "[class*='overlay']",
        # Advertisement patterns
        "[class*='advertisement']",
        "[class*='ad-wrapper']",
        "[class*='sponsored']",
        "[class*='promo']",
        # Social widgets
        "[class*='social-share']",
        "[class*='share-buttons']",
        "[class*='social-links']",
        # Tracking pixels and hidden elements
        "img[width='1']",
        "img[height='1']",
        "[style*='display:none']",
        "[style*='display: none']",
        "[hidden]",
        ".hidden",
        # Related/recommended content
        "[class*='related-']",
        "[class*='recommended']",
        "[class*='also-read']",
        # Comments sections
        "[class*='comment']",
        "#comments",
        ".disqus",
    ]

    # Main content selectors (in priority order)
    MAIN_CONTENT_SELECTORS = [
        # Framework-specific
        "#mw-content-text",  # Wikipedia
        ".mw-parser-output",
        ".rst-content",  # ReadTheDocs
        ".document",  # Sphinx
        ".markdown-body",  # GitHub
        ".notion-page-content",  # Notion
        # Facebook/Meta developer docs
        "#documentation_body_pagelet",  # Facebook docs main content
        "._4-u2",  # Facebook docs content wrapper
        # Semantic elements
        "main[role='main']",
        "main#content",
        "main.content",
        "main",
        "article.post-content",
        "article.entry-content",
        "article.content",
        "article",
        "[role='main']",
        # Common ID patterns
        "#main-content",
        "#content",
        "#main",
        "#article",
        "#post",
        # Common class patterns
        ".main-content",
        ".content",
        ".post-content",
        ".article-content",
        ".entry-content",
        ".page-content",
        ".body-content",
    ]

    def convert(
        self,
        html: str,
        base_url: str | None = None,
        only_main_content: bool = True,
        remove_boilerplate: bool = True,
        include_tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
        content_mode: float = 0.5,
        query: str | None = None,
    ) -> str:
        """Convert HTML to markdown.

        Uses markdownify for conversion, which preserves table structure correctly.

        Args:
            html: Raw HTML content (should be post-JavaScript rendering from Playwright)
            base_url: Base URL for resolving relative links
            only_main_content: Extract main content area only
            remove_boilerplate: Remove nav, footer, ads, etc.
            include_tags: CSS selectors for elements to include.
                         When specified, takes precedence over only_main_content.
            exclude_tags: CSS selectors for elements to exclude.
                         Applied before include_tags filtering.
            content_mode: Precision/recall dial in [0.0, 1.0]. Low = recall-biased
                         (accept more, prune less); high = precision-biased (demand
                         denser output, prune more). Default 0.5.
            query: Optional query string. When set, sections are filtered post-cascade
                   to retain only query-relevant content. Flat pages are not filtered.

        Returns:
            Clean markdown string
        """
        if not html or not html.strip():
            return ""

        return self._convert_with_patterns(
            html,
            base_url,
            only_main_content,
            remove_boilerplate,
            include_tags,
            exclude_tags,
            content_mode=content_mode,
            query=query,
        )

    def _convert_with_patterns(
        self,
        html: str,
        base_url: str | None = None,
        only_main_content: bool = True,
        remove_boilerplate: bool = True,
        include_tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
        content_mode: float = 0.5,
        query: str | None = None,
    ) -> str:
        """Extract content using pattern-based approach.

        Args:
            html: Raw HTML content
            base_url: Base URL for resolving relative links
            only_main_content: Extract main content area only
            remove_boilerplate: Remove nav, footer, ads, etc.
            include_tags: CSS selectors for elements to include
            exclude_tags: CSS selectors for elements to exclude
            content_mode: Precision/recall dial in [0.0, 1.0].
            query: Optional query string for section-relevance filtering.

        Returns:
            Markdown string
        """
        try:
            soup = BeautifulSoup(html, "html.parser")

            if expand_markup_attributes(soup):
                html = str(soup)

            if remove_boilerplate:
                self._remove_boilerplate(soup)

            # Apply site-specific preprocessors (auto-detected)
            apply_site_preprocessors(soup)

            # Apply exclude_tags first (before include_tags)
            if exclude_tags:
                self._apply_exclude_tags(soup, exclude_tags)

            content_element = None

            # include_tags takes precedence over only_main_content and the cascade.
            if include_tags:
                content_element = self._apply_include_tags(soup, include_tags)
            elif only_main_content:
                from supacrawl.services.content_filter import extract as cf_extract

                content_element = cf_extract(
                    soup=soup,
                    html=html,
                    main_content_selectors=self.MAIN_CONTENT_SELECTORS,
                    content_mode=content_mode,
                    query=query,
                )

            if content_element:
                html_to_convert = str(content_element)
            else:
                body = soup.find("body")
                html_to_convert = str(body) if body else str(soup)

            converter = AbsoluteUrlConverter(
                base_url=base_url,
                heading_style="atx",
                bullets="-",
                code_language="",
                strip=["script", "style", "nav", "footer", "header"],
                wrap=False,
                wrap_width=0,
            )
            markdown = converter.convert(html_to_convert)

            return self._clean_whitespace(markdown)

        except Exception as e:
            LOGGER.warning(f"Pattern-based conversion failed: {e}")
            try:
                soup = BeautifulSoup(html, "html.parser")
                return soup.get_text(separator="\n\n", strip=True)
            except Exception:
                return ""

    def _apply_exclude_tags(self, soup: BeautifulSoup, exclude_tags: list[str]) -> None:
        """Remove elements matching exclude_tags selectors.

        Args:
            soup: BeautifulSoup object to modify in-place
            exclude_tags: List of CSS selectors for elements to remove
        """
        for selector in exclude_tags:
            try:
                for element in soup.select(selector):
                    element.decompose()
            except Exception as e:
                LOGGER.warning(f"Invalid exclude_tags selector '{selector}': {e}")

    def _apply_include_tags(self, soup: BeautifulSoup, include_tags: list[str]) -> Tag | None:
        """Extract only elements matching include_tags selectors.

        Args:
            soup: BeautifulSoup object to search
            include_tags: List of CSS selectors for elements to include

        Returns:
            A wrapper Tag containing all matched elements, or None if no matches
        """
        matched_elements: list[Tag] = []

        for selector in include_tags:
            try:
                for element in soup.select(selector):
                    # Avoid duplicates (e.g., nested matches)
                    if element not in matched_elements:
                        matched_elements.append(element)
            except Exception as e:
                LOGGER.warning(f"Invalid include_tags selector '{selector}': {e}")

        if not matched_elements:
            LOGGER.debug("No elements matched include_tags selectors")
            return None

        # Create a wrapper div to hold all matched elements
        wrapper = soup.new_tag("div")
        for element in matched_elements:
            # Copy the element to avoid modifying the original structure
            wrapper.append(element.extract())

        LOGGER.debug(f"include_tags matched {len(matched_elements)} elements")
        return wrapper

    def _remove_boilerplate(self, soup: BeautifulSoup) -> None:
        """Remove boilerplate elements in-place."""
        # Remove always-unwanted tags (scripts, styles, etc.)
        for tag_name in self.REMOVE_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        # Remove structural boilerplate (nav, footer, etc.)
        for tag_name in self.BOILERPLATE_TAGS:
            for tag in soup.find_all(tag_name):
                # Don't remove if it's the main content area
                if not self._is_main_content(tag):
                    tag.decompose()

        # Remove elements matching boilerplate CSS selectors
        for selector in self.BOILERPLATE_SELECTORS:
            try:
                for element in soup.select(selector):
                    if not self._is_main_content(element):
                        element.decompose()
            except Exception:
                pass

    def _is_main_content(self, element) -> bool:
        """Check if element is likely main content."""
        if element is None or not isinstance(element, Tag):
            return False

        try:
            el_id = str(element.get("id") or "").lower()
            el_class = " ".join(element.get("class") or []).lower()
            el_role = str(element.get("role") or "").lower()

            main_indicators = ["main", "content", "article", "post", "entry"]
            combined = f"{el_id} {el_class} {el_role}"

            return any(indicator in combined for indicator in main_indicators)
        except Exception:
            return False

    def _clean_whitespace(self, markdown: str) -> str:
        """Clean up excessive whitespace."""
        lines = []
        prev_empty = False

        for line in markdown.splitlines():
            stripped = line.rstrip()
            is_empty = not stripped

            if is_empty:
                if not prev_empty:
                    lines.append("")
                prev_empty = True
            else:
                lines.append(stripped)
                prev_empty = False

        return "\n".join(lines).strip()
