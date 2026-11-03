"""Branding extraction service for design system analysis."""

import re
from typing import Any, Literal
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from supacrawl.models import BrandingColors, BrandingProfile


class BrandingExtractor:
    """Extract brand identity information from HTML and CSS.

    Extracts:
    - Colour palette (primary, secondary, accent, background, text)
    - Fonts (family names, Google Fonts)
    - Logo URL
    - Colour scheme (light/dark)
    - Typography settings
    - Spacing constants
    """

    def extract(self, html: str, base_url: str) -> BrandingProfile:
        """Extract branding information from HTML.

        Args:
            html: HTML content to analyze
            base_url: Base URL for resolving relative URLs

        Returns:
            BrandingProfile with extracted brand identity
        """
        soup = BeautifulSoup(html, "html.parser")

        # Extract CSS from style tags and linked stylesheets
        css_content = self._extract_css(soup)

        # Extract colours from CSS
        colors = self._extract_colors(css_content, soup)

        # Extract fonts
        fonts = self._extract_fonts(css_content, soup)

        # Extract logo
        logo = self._extract_logo(soup, base_url)

        # Detect colour scheme
        color_scheme = self._detect_color_scheme(soup, css_content)

        # Extract typography
        typography = self._extract_typography(css_content)

        # Extract spacing
        spacing = self._extract_spacing(css_content)

        # Extract image URLs
        images = self._extract_brand_images(soup, base_url)

        return BrandingProfile(
            color_scheme=color_scheme,
            logo=logo,
            colors=colors,
            fonts=fonts,
            typography=typography,
            spacing=spacing,
            images=images,
        )

    def _extract_css(self, soup: BeautifulSoup) -> str:
        """Extract CSS content from style tags.

        Args:
            soup: BeautifulSoup parsed HTML

        Returns:
            Combined CSS content
        """
        css_parts = []

        # Get inline styles
        for style_tag in soup.find_all("style"):
            if style_tag.string:
                css_parts.append(style_tag.string)

        return "\n".join(css_parts)

    def _extract_colors(self, css: str, soup: BeautifulSoup) -> BrandingColors:
        """Extract colour palette from CSS.

        Args:
            css: CSS content
            soup: BeautifulSoup parsed HTML

        Returns:
            BrandingColors with detected colours
        """
        colors = BrandingColors()

        # Extract CSS custom properties (CSS variables)
        css_vars = self._extract_css_variables(css)

        # Common naming patterns for colours
        primary_patterns = ["primary", "main", "brand"]
        secondary_patterns = ["secondary", "accent-2"]
        accent_patterns = ["accent", "highlight"]
        bg_patterns = ["background", "bg", "surface"]
        text_patterns = ["text", "foreground", "fg"]

        # Try to match CSS variables to colour roles
        for var_name, var_value in css_vars.items():
            var_lower = var_name.lower()

            if any(p in var_lower for p in primary_patterns) and "color" in var_lower:
                if not colors.primary:
                    colors.primary = var_value
            elif any(p in var_lower for p in secondary_patterns) and "color" in var_lower:
                if not colors.secondary:
                    colors.secondary = var_value
            elif any(p in var_lower for p in accent_patterns) and "color" in var_lower:
                if not colors.accent:
                    colors.accent = var_value
            elif any(p in var_lower for p in bg_patterns) and "color" in var_lower:
                if not colors.background:
                    colors.background = var_value
            elif any(p in var_lower for p in text_patterns) and "color" in var_lower and not colors.text_primary:
                colors.text_primary = var_value

        # Fallback: extract meta theme-color
        theme_color = soup.find("meta", attrs={"name": "theme-color"})
        if theme_color and not colors.primary:
            content = theme_color.get("content")
            if content:
                colors.primary = str(content)

        return colors

    def _extract_css_variables(self, css: str) -> dict[str, str]:
        """Extract CSS custom properties (variables).

        Args:
            css: CSS content

        Returns:
            Dictionary of variable names to values
        """
        variables = {}

        # Match --variable-name: value; patterns
        pattern = r"--([a-zA-Z0-9-]+)\s*:\s*([^;]+);"
        matches = re.findall(pattern, css)

        for name, value in matches:
            variables[name] = value.strip()

        return variables

    def _extract_fonts(self, css: str, soup: BeautifulSoup) -> list[dict[str, str]]:
        """Extract font families from CSS and HTML.

        Args:
            css: CSS content
            soup: BeautifulSoup parsed HTML

        Returns:
            List of font dictionaries
        """
        fonts = []
        seen_families = set()

        # Extract from font-family declarations
        font_pattern = r"font-family\s*:\s*([^;]+);"
        matches = re.findall(font_pattern, css)

        for match in matches:
            # Split by comma and clean
            families = [f.strip().strip('"').strip("'") for f in match.split(",")]
            for family in families:
                # Skip generic families
                if family.lower() in ["serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui"]:
                    continue
                if family and family not in seen_families:
                    fonts.append({"family": family})
                    seen_families.add(family)

        # Extract from Google Fonts links
        for link in soup.find_all("link", href=True):
            href = str(link.get("href", ""))
            if "fonts.googleapis.com" in href or "fonts.gstatic.com" in href:
                # Try to extract family name from URL
                family_match = re.search(r"family=([^:&]+)", href)
                if family_match:
                    family = family_match.group(1).replace("+", " ")
                    if family not in seen_families:
                        fonts.append({"family": family})
                        seen_families.add(family)

        return fonts[:10]  # Limit to 10 fonts

    def _extract_logo(self, soup: BeautifulSoup, base_url: str) -> str | None:
        """Extract logo URL from common locations.

        Searches ``<img>`` tags first, then falls back to CSS
        ``background-image``, inline SVGs, ARIA attributes, and
        site-builder-specific patterns (Wix, Framer, Squarespace).

        Args:
            soup: BeautifulSoup parsed HTML
            base_url: Base URL for resolving relative URLs

        Returns:
            Absolute logo URL or None
        """
        # Phase 1: High-confidence <img> selectors with explicit logo semantics
        logo_img_selectors = [
            "img.logo",
            "img#logo",
            "[class*='logo'] img",
            ".navbar-brand img",
            ".site-logo img",
            # ARIA-based: accessible logos
            "[role='img'][aria-label*='logo']",
            "img[alt*='logo' i]",
        ]

        for selector in logo_img_selectors:
            el = soup.select_one(selector)
            if el:
                src = el.get("src")
                if not src and el.name != "img":
                    # Matched a container — look for nested <img>
                    nested = el.find("img")
                    if nested:
                        src = nested.get("src")
                if src and isinstance(src, str) and not src.startswith("data:"):
                    return urljoin(base_url, str(src))

        # Phase 2: Site-builder-specific patterns
        builder_logo = self._extract_builder_logo(soup, base_url)
        if builder_logo:
            return builder_logo

        # Phase 3: CSS background-image on logo-related elements
        bg_logo_selectors = [
            ".logo",
            "#logo",
            "[class*='logo']",
            ".navbar-brand",
            ".site-logo",
        ]
        for selector in bg_logo_selectors:
            el = soup.select_one(selector)
            if el:
                url = self._extract_background_image_url(el)
                if url:
                    return urljoin(base_url, url)

        # Phase 4: Low-confidence fallbacks
        # header img — only if it looks like a logo (small dimensions or SVG)
        header_img = self._extract_header_logo_img(soup, base_url)
        if header_img:
            return header_img

        # Fallback: meta og:image
        og_image = soup.find("meta", property="og:image")
        if og_image:
            content = og_image.get("content")
            if content:
                return urljoin(base_url, str(content))

        return None

    def _extract_builder_logo(self, soup: BeautifulSoup, base_url: str) -> str | None:
        """Extract logo from site-builder-specific HTML patterns.

        Handles Wix, Framer, and Squarespace non-semantic markup.

        Args:
            soup: BeautifulSoup parsed HTML
            base_url: Base URL for resolving relative URLs

        Returns:
            Absolute logo URL or None
        """
        # Wix: logos inside a link to / near the top, often using
        # <wow-image> or <img> inside nested non-semantic divs.
        # Wix header logos commonly live inside the first <a href="/"> in
        # a section whose id contains "header" or "comp-".
        for a_tag in soup.find_all("a", href="/"):
            img = a_tag.find("img")
            if img:
                src = img.get("src")
                if src and isinstance(src, str) and not src.startswith("data:"):
                    return urljoin(base_url, src)
            # Check for SVG logo inside the link
            svg = a_tag.find("svg")
            if svg:
                # Can't return a URL for inline SVG — skip to next pattern
                continue

        # Framer: data-framer-name="Logo" or data-framer-component-type="Logo"
        for attr in ("data-framer-name", "data-framer-component-type"):
            logo_el = soup.find(None, attrs={attr: re.compile(r"logo", re.IGNORECASE)})
            if logo_el:
                img = logo_el.find("img")
                if img:
                    src = img.get("src")
                    if src and isinstance(src, str) and not src.startswith("data:"):
                        return urljoin(base_url, src)
                # Check background-image on the element itself
                url = self._extract_background_image_url(logo_el)
                if url:
                    return urljoin(base_url, url)

        # Squarespace: logo inside data-section-type="header" or
        # .header-display-desktop img, or .site-title img
        sqsp_selectors = [
            "[data-section-type='header'] img",
            ".header-display-desktop img",
            ".site-title img",
            ".site-branding img",
        ]
        for selector in sqsp_selectors:
            el = soup.select_one(selector)
            if el:
                src = el.get("src")
                if src and isinstance(src, str) and not src.startswith("data:"):
                    return urljoin(base_url, src)

        return None

    @staticmethod
    def _extract_header_logo_img(soup: BeautifulSoup, base_url: str) -> str | None:
        """Extract a plausible logo from ``header img`` with size validation.

        Skips images that look like hero banners or content images based on
        explicit width/height attributes exceeding typical logo dimensions.

        Args:
            soup: BeautifulSoup parsed HTML
            base_url: Base URL for resolving relative URLs

        Returns:
            Absolute logo URL or None
        """
        header = soup.find("header")
        if not header:
            return None

        for img in header.find_all("img"):
            src = img.get("src")
            if not src or not isinstance(src, str) or src.startswith("data:"):
                continue

            # SVG files are almost certainly logos, not hero images
            if ".svg" in src.lower():
                return urljoin(base_url, src)

            # Check explicit dimensions — skip images wider than 600px
            # (likely hero/banner images, not logos)
            width = img.get("width")
            if width:
                try:
                    if int(str(width).rstrip("px")) > 600:
                        continue
                except ValueError, TypeError:
                    pass

            return urljoin(base_url, src)

        return None

    @staticmethod
    def _extract_background_image_url(element: Any) -> str | None:
        """Extract the first URL from an element's inline background-image style.

        Args:
            element: BeautifulSoup element

        Returns:
            Raw URL string or None
        """
        style = element.get("style", "")
        if not style or not isinstance(style, str):
            return None
        match = re.search(r"""background(?:-image)?\s*:[^;]*url\(\s*(['"]?)(.*?)\1\s*\)""", style)
        if match:
            url = match.group(2).strip()
            if url and not url.startswith("data:"):
                return url
        return None

    def _detect_color_scheme(self, soup: BeautifulSoup, css: str) -> Literal["light", "dark"] | None:
        """Detect if site uses light or dark colour scheme.

        Args:
            soup: BeautifulSoup parsed HTML
            css: CSS content

        Returns:
            "light", "dark", or None
        """
        # Check for dark mode meta tag
        color_scheme_meta = soup.find("meta", attrs={"name": "color-scheme"})
        if color_scheme_meta:
            content = str(color_scheme_meta.get("content", "")).lower()
            if "dark" in content:
                return "dark"
            if "light" in content:
                return "light"

        # Check for dark mode classes on html or body
        html_tag = soup.find("html")
        body_tag = soup.find("body")

        for tag in [html_tag, body_tag]:
            if tag and hasattr(tag, "get"):
                classes = tag.get("class")
                if classes:
                    if isinstance(classes, list):
                        classes_str = " ".join(str(c) for c in classes).lower()
                    else:
                        classes_str = str(classes).lower()

                    if "dark" in classes_str or "night" in classes_str:
                        return "dark"

        # Check CSS for dark mode indicators
        if "prefers-color-scheme: dark" in css or "--dark" in css:
            return "dark"

        return "light"  # Default to light

    def _extract_typography(self, css: str) -> dict[str, Any]:
        """Extract typography settings from CSS.

        Args:
            css: CSS content

        Returns:
            Dictionary with font families and sizes
        """
        typography: dict[str, Any] = {
            "fontFamilies": {},
            "fontSizes": {},
        }

        # Extract font sizes for headings and body
        size_patterns = {
            "h1": r"h1\s*{[^}]*font-size\s*:\s*([^;]+);",
            "h2": r"h2\s*{[^}]*font-size\s*:\s*([^;]+);",
            "h3": r"h3\s*{[^}]*font-size\s*:\s*([^;]+);",
            "body": r"body\s*{[^}]*font-size\s*:\s*([^;]+);",
        }

        for element, pattern in size_patterns.items():
            match = re.search(pattern, css, re.DOTALL)
            if match:
                typography["fontSizes"][element] = match.group(1).strip()

        return typography if typography["fontSizes"] else {}

    def _extract_spacing(self, css: str) -> dict[str, Any]:
        """Extract spacing constants from CSS.

        Args:
            css: CSS content

        Returns:
            Dictionary with spacing values
        """
        spacing: dict[str, Any] = {}

        # Extract CSS variables related to spacing
        css_vars = self._extract_css_variables(css)

        for var_name, var_value in css_vars.items():
            var_lower = var_name.lower()
            if "spacing" in var_lower or "gap" in var_lower or "margin" in var_lower:
                spacing[var_name] = var_value

        # Extract border-radius
        radius_pattern = r"border-radius\s*:\s*([^;]+);"
        matches = re.findall(radius_pattern, css)
        if matches:
            # Use most common value
            spacing["borderRadius"] = matches[0].strip()

        return spacing

    def _extract_brand_images(self, soup: BeautifulSoup, base_url: str) -> dict[str, str]:
        """Extract brand-related image URLs.

        Args:
            soup: BeautifulSoup parsed HTML
            base_url: Base URL for resolving relative URLs

        Returns:
            Dictionary of image type to URL
        """
        images = {}

        # Favicon
        favicon = soup.find("link", rel="icon")
        if not favicon:
            favicon = soup.find("link", rel="shortcut icon")
        if favicon:
            href = favicon.get("href")
            if href:
                images["favicon"] = urljoin(base_url, str(href))

        # OG Image
        og_image = soup.find("meta", property="og:image")
        if og_image:
            content = og_image.get("content")
            if content:
                images["ogImage"] = urljoin(base_url, str(content))

        # Apple Touch Icon
        apple_icon = soup.find("link", rel="apple-touch-icon")
        if apple_icon:
            href = apple_icon.get("href")
            if href:
                images["appleTouchIcon"] = urljoin(base_url, str(href))

        return images
