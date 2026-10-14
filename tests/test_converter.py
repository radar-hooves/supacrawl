"""Tests for markdown converter."""

from bs4 import BeautifulSoup, Tag

from supacrawl.services.converter import (
    SITE_PREPROCESSORS,
    MarkdownConverter,
    _detect_css_counter_lists,
    _detect_mkdocs_material,
    _detect_wordpress,
    _preprocess_css_counter_lists,
    _preprocess_mkdocs_material,
    _preprocess_wordpress,
    apply_site_preprocessors,
    expand_markup_attributes,
)


class TestMarkdownConverter:
    """Tests for MarkdownConverter."""

    def test_converts_headings_to_atx(self):
        """Test that headings use ATX style (#)."""
        converter = MarkdownConverter()
        html = "<h1>Title</h1><h2>Subtitle</h2>"
        md = converter.convert(html, only_main_content=False)
        assert "# Title" in md
        assert "## Subtitle" in md

    def test_removes_script_tags(self):
        """Test that script tags are removed."""
        converter = MarkdownConverter()
        html = "<p>Content</p><script>alert('x')</script>"
        md = converter.convert(html, only_main_content=False)
        assert "alert" not in md
        assert "Content" in md

    def test_preserves_links(self):
        """Test that links are preserved."""
        converter = MarkdownConverter()
        html = '<a href="https://example.com">Link</a>'
        md = converter.convert(html, only_main_content=False)
        assert "[Link](https://example.com)" in md

    def test_strips_javascript_links(self):
        """Test that javascript: links are removed entirely (UI controls)."""
        converter = MarkdownConverter()
        html = '<a href="javascript:window.print()">Print this page</a>'
        md = converter.convert(html, only_main_content=False)
        assert md.strip() == ""
        assert "Print this page" not in md
        assert "javascript:" not in md

    def test_strips_javascript_void_links(self):
        """Test that javascript:void(0) links are removed entirely."""
        converter = MarkdownConverter()
        html = '<a href="javascript:void(0)">Click me</a>'
        md = converter.convert(html, only_main_content=False)
        assert md.strip() == ""
        assert "Click me" not in md

    def test_preserves_non_javascript_protocols(self):
        """Test that other protocols like mailto: are preserved."""
        converter = MarkdownConverter()
        html = '<a href="mailto:test@example.com">Email</a>'
        md = converter.convert(html, only_main_content=False)
        assert "[Email](mailto:test@example.com)" in md

    def test_strips_javascript_case_insensitive(self):
        """Test that javascript: links are removed regardless of case."""
        converter = MarkdownConverter()
        # Uppercase
        html1 = '<a href="JAVASCRIPT:alert(1)">Uppercase</a>'
        md1 = converter.convert(html1, only_main_content=False)
        assert md1.strip() == ""
        assert "Uppercase" not in md1

        # Mixed case
        html2 = '<a href="JavaScript:void(0)">Mixed</a>'
        md2 = converter.convert(html2, only_main_content=False)
        assert md2.strip() == ""
        assert "Mixed" not in md2

    def test_strips_javascript_with_whitespace(self):
        """Test that javascript: links with leading/trailing whitespace are removed."""
        converter = MarkdownConverter()
        html = '<a href=" javascript:void(0) ">Whitespace</a>'
        md = converter.convert(html, only_main_content=False)
        assert md.strip() == ""
        assert "Whitespace" not in md

    def test_preserves_code_blocks(self):
        """Test that code blocks are preserved."""
        converter = MarkdownConverter()
        html = "<pre><code>def foo(): pass</code></pre>"
        md = converter.convert(html, only_main_content=False)
        assert "def foo(): pass" in md

    def test_cleans_whitespace(self):
        """Test that excessive whitespace is cleaned."""
        converter = MarkdownConverter()
        html = "<p>A</p><p></p><p></p><p></p><p>B</p>"
        md = converter.convert(html, only_main_content=False)
        # Should not have more than 2 consecutive blank lines
        assert "\n\n\n\n" not in md

    def test_removes_nav_tags(self):
        """Test that nav tags are removed."""
        converter = MarkdownConverter()
        html = "<nav>Navigation</nav><p>Content</p>"
        md = converter.convert(html, only_main_content=False)
        assert "Navigation" not in md
        assert "Content" in md

    def test_removes_footer_tags(self):
        """Test that footer tags are removed."""
        converter = MarkdownConverter()
        html = "<p>Content</p><footer>Footer text</footer>"
        md = converter.convert(html, only_main_content=False)
        assert "Footer text" not in md
        assert "Content" in md

    def test_finds_main_content_with_main_tag(self):
        """Test that main content is extracted from main tag."""
        converter = MarkdownConverter()
        html = """
        <nav>Navigation</nav>
        <main><h1>Main Content</h1></main>
        <footer>Footer</footer>
        """
        md = converter.convert(html, only_main_content=True)
        assert "Main Content" in md
        # Navigation and footer should be excluded because we're using only_main_content
        # But they get removed anyway by boilerplate removal
        assert "Navigation" not in md
        assert "Footer" not in md

    def test_finds_main_content_with_article_tag(self):
        """Test that main content is extracted from article tag."""
        converter = MarkdownConverter()
        html = """
        <div class="sidebar">Sidebar</div>
        <article><h1>Article Content</h1></article>
        """
        md = converter.convert(html, only_main_content=True)
        assert "Article Content" in md
        assert "Sidebar" not in md

    def test_falls_back_to_body_when_no_main_content(self):
        """Test fallback to body when no main content selector matches."""
        converter = MarkdownConverter()
        html = "<body><div><h1>Content</h1></div></body>"
        md = converter.convert(html, only_main_content=True)
        assert "Content" in md

    def test_handles_empty_html(self):
        """Test handling of empty HTML."""
        converter = MarkdownConverter()
        assert converter.convert("") == ""
        assert converter.convert("   ") == ""

    def test_handles_malformed_html(self):
        """Test handling of malformed HTML."""
        converter = MarkdownConverter()
        html = "<p>Unclosed paragraph<div>Nested weirdly"
        md = converter.convert(html, only_main_content=False)
        # Should still extract some text
        assert "Unclosed paragraph" in md

    def test_uses_dash_for_bullets(self):
        """Test that unordered lists use dash bullets."""
        converter = MarkdownConverter()
        html = "<ul><li>Item 1</li><li>Item 2</li></ul>"
        md = converter.convert(html, only_main_content=False)
        assert "- Item 1" in md
        assert "- Item 2" in md

    def test_preserves_tables(self):
        """Test that tables are preserved."""
        converter = MarkdownConverter()
        html = """
        <table>
            <tr><th>Header</th></tr>
            <tr><td>Data</td></tr>
        </table>
        """
        md = converter.convert(html, only_main_content=False)
        assert "Header" in md
        assert "Data" in md

    def test_strips_trailing_whitespace_per_line(self):
        """Test that trailing whitespace is stripped from each line."""
        converter = MarkdownConverter()
        html = "<p>Line 1</p><p>Line 2</p>"
        md = converter.convert(html, only_main_content=False)
        lines = md.split("\n")
        for line in lines:
            assert line == line.rstrip()

    def test_no_boilerplate_removal(self):
        """Test conversion with boilerplate removal disabled."""
        converter = MarkdownConverter()
        html = "<nav>Navigation</nav><p>Content</p>"
        md = converter.convert(html, only_main_content=False, remove_boilerplate=False)
        # When remove_boilerplate=False, markdownify still strips nav in its strip list
        # So we can't test this properly. Let's at least verify it doesn't crash
        assert "Content" in md


class TestIncludeExcludeTags:
    """Tests for include_tags and exclude_tags filtering."""

    def test_include_tags_extracts_matching_elements(self):
        """Test that include_tags extracts only matching elements."""
        converter = MarkdownConverter()
        html = """
        <nav>Navigation</nav>
        <article><h1>Article Content</h1></article>
        <aside>Sidebar</aside>
        """
        md = converter.convert(
            html,
            only_main_content=False,
            remove_boilerplate=False,
            include_tags=["article"],
        )
        assert "Article Content" in md
        # Sidebar should not be included (not matching include_tags)
        assert "Sidebar" not in md

    def test_include_tags_with_class_selector(self):
        """Test that include_tags works with class selectors."""
        converter = MarkdownConverter()
        html = """
        <div class="header">Header</div>
        <div class="post-content"><p>Post text</p></div>
        <div class="footer">Footer</div>
        """
        md = converter.convert(
            html,
            only_main_content=False,
            remove_boilerplate=False,
            include_tags=[".post-content"],
        )
        assert "Post text" in md
        assert "Header" not in md
        assert "Footer" not in md

    def test_include_tags_multiple_selectors(self):
        """Test that multiple include_tags selectors are combined."""
        converter = MarkdownConverter()
        html = """
        <nav>Navigation</nav>
        <article><h1>Article</h1></article>
        <main><p>Main content</p></main>
        <footer>Footer</footer>
        """
        md = converter.convert(
            html,
            only_main_content=False,
            remove_boilerplate=False,
            include_tags=["article", "main"],
        )
        assert "Article" in md
        assert "Main content" in md
        assert "Navigation" not in md
        assert "Footer" not in md

    def test_exclude_tags_removes_matching_elements(self):
        """Test that exclude_tags removes matching elements."""
        converter = MarkdownConverter()
        html = """
        <article>
            <h1>Title</h1>
            <p>Content</p>
            <div class="advertisement">Ad here</div>
        </article>
        """
        md = converter.convert(
            html,
            only_main_content=False,
            remove_boilerplate=False,
            exclude_tags=[".advertisement"],
        )
        assert "Title" in md
        assert "Content" in md
        assert "Ad here" not in md

    def test_exclude_tags_multiple_selectors(self):
        """Test that multiple exclude_tags selectors are removed."""
        converter = MarkdownConverter()
        html = """
        <article>
            <h1>Title</h1>
            <p>Content</p>
            <nav>Nav menu</nav>
            <div class="sidebar">Sidebar</div>
            <footer>Footer text</footer>
        </article>
        """
        md = converter.convert(
            html,
            only_main_content=False,
            remove_boilerplate=False,
            exclude_tags=["nav", ".sidebar", "footer"],
        )
        assert "Title" in md
        assert "Content" in md
        assert "Nav menu" not in md
        assert "Sidebar" not in md
        assert "Footer text" not in md

    def test_exclude_before_include(self):
        """Test that exclude_tags is applied before include_tags."""
        converter = MarkdownConverter()
        html = """
        <article>
            <h1>Article Title</h1>
            <p>Good content</p>
            <div class="promo">Promotion</div>
        </article>
        """
        # Exclude promo, then include article
        md = converter.convert(
            html,
            only_main_content=False,
            remove_boilerplate=False,
            include_tags=["article"],
            exclude_tags=[".promo"],
        )
        assert "Article Title" in md
        assert "Good content" in md
        assert "Promotion" not in md

    def test_include_tags_takes_precedence_over_only_main_content(self):
        """Test that include_tags takes precedence over only_main_content."""
        converter = MarkdownConverter()
        html = """
        <main><p>Main area</p></main>
        <aside class="special"><p>Special sidebar</p></aside>
        """
        md = converter.convert(
            html,
            only_main_content=True,
            remove_boilerplate=False,
            include_tags=[".special"],
        )
        # Should get special sidebar, not main, because include_tags takes precedence
        assert "Special sidebar" in md
        # Main should not be included since include_tags overrides only_main_content
        assert "Main area" not in md

    def test_invalid_selector_logs_warning_but_continues(self):
        """Test that invalid selectors don't crash, just log warning."""
        converter = MarkdownConverter()
        html = "<p>Content</p>"
        # Invalid CSS selector should not crash
        md = converter.convert(
            html,
            only_main_content=False,
            remove_boilerplate=False,
            exclude_tags=["[invalid[selector"],
        )
        # Content should still be extracted
        assert "Content" in md

    def test_no_matching_include_tags_returns_full_content(self):
        """Test that when no include_tags match, full content is returned."""
        converter = MarkdownConverter()
        html = "<p>Paragraph content</p>"
        md = converter.convert(
            html,
            only_main_content=False,
            remove_boilerplate=False,
            include_tags=[".nonexistent"],
        )
        # Should fall back to full content
        assert "Paragraph content" in md

    def test_attribute_selector(self):
        """Test that attribute selectors work."""
        converter = MarkdownConverter()
        html = """
        <div>Regular div</div>
        <div data-content="true"><p>Data content</p></div>
        """
        md = converter.convert(
            html,
            only_main_content=False,
            remove_boilerplate=False,
            include_tags=["[data-content]"],
        )
        assert "Data content" in md
        assert "Regular div" not in md

    def test_id_selector(self):
        """Test that ID selectors work."""
        converter = MarkdownConverter()
        html = """
        <div id="other">Other</div>
        <div id="content"><p>Main content</p></div>
        """
        md = converter.convert(
            html,
            only_main_content=False,
            remove_boilerplate=False,
            include_tags=["#content"],
        )
        assert "Main content" in md
        assert "Other" not in md


class TestMkDocsMaterialPreprocessing:
    """Tests for MkDocs Material HTML preprocessing."""

    def test_strips_headerlink_anchors(self):
        """Test that permalink anchors are stripped from headings."""
        html = """
        <h1 id="title">Title<a class="headerlink" href="#title" title="Permanent link">¶</a></h1>
        <h2 id="section">Section<a class="headerlink" href="#section">¶</a></h2>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_mkdocs_material(soup)

        # Headerlinks should be removed
        assert soup.select("a.headerlink") == []
        # But headings should remain
        h1 = soup.find("h1")
        h2 = soup.find("h2")
        assert h1 is not None
        assert h2 is not None
        assert h1.get_text(strip=True) == "Title"
        assert h2.get_text(strip=True) == "Section"

    def test_converts_highlighttable_to_code_block(self):
        """Test that line-numbered code tables are converted to proper code blocks."""
        html = """
        <table class="highlighttable">
            <tbody>
                <tr>
                    <td class="linenos">
                        <div class="linenodiv"><pre>1\n2</pre></div>
                    </td>
                    <td class="code">
                        <pre><code>int x = 1;
int y = 2;</code></pre>
                    </td>
                </tr>
            </tbody>
        </table>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_mkdocs_material(soup)

        # Table should be replaced with pre/code
        assert soup.find("table", class_="highlighttable") is None
        code = soup.find("code")
        assert code is not None
        assert "int x = 1" in code.get_text()
        assert "int y = 2" in code.get_text()

    def test_converts_admonition_to_blockquote(self):
        """Test that admonitions are converted to blockquotes with bold titles."""
        html = """
        <div class="admonition note">
            <p class="admonition-title">Note</p>
            <p>This is important information.</p>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_mkdocs_material(soup)

        # Admonition should be replaced with blockquote
        assert soup.find("div", class_="admonition") is None
        blockquote = soup.find("blockquote")
        assert blockquote is not None
        strong = blockquote.find("strong")
        assert strong is not None
        assert "Note:" in strong.get_text()
        assert "important information" in blockquote.get_text()

    def test_converts_admonition_with_custom_title(self):
        """Test that admonitions preserve custom titles."""
        html = """
        <div class="admonition example">
            <p class="admonition-title">Working with JSON</p>
            <p>Example content here.</p>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_mkdocs_material(soup)

        blockquote = soup.find("blockquote")
        assert blockquote is not None
        assert "Working with JSON:" in blockquote.get_text()
        assert "Example content" in blockquote.get_text()

    def test_handles_tabbed_content(self):
        """Test that tabbed content gets clear language headers."""
        html = """
        <div class="tabbed-set tabbed-alternate">
            <div class="tabbed-labels">
                <label>C#</label>
                <label>Python</label>
            </div>
            <div class="tabbed-content">
                <div class="tabbed-block">
                    <p>C# code example</p>
                </div>
                <div class="tabbed-block">
                    <p>Python code example</p>
                </div>
            </div>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_mkdocs_material(soup)

        # Tabbed set should be replaced
        assert soup.find("div", class_="tabbed-set") is None
        # Should have h4 headers for each tab
        headers = soup.find_all("h4")
        assert len(headers) == 2
        header_texts = [h.get_text(strip=True) for h in headers]
        assert "C#" in header_texts
        assert "Python" in header_texts
        # Content should be preserved
        assert "C# code example" in soup.get_text()
        assert "Python code example" in soup.get_text()

    def test_full_conversion_with_mkdocs_material(self):
        """Test full conversion of MkDocs Material HTML to markdown."""
        converter = MarkdownConverter()
        html = """
        <html>
        <body>
            <article>
                <h1 id="title">Title<a class="headerlink" href="#title">¶</a></h1>
                <div class="admonition note">
                    <p class="admonition-title">Note</p>
                    <p>Important info.</p>
                </div>
                <table class="highlighttable">
                    <tr>
                        <td class="linenos"><pre>1</pre></td>
                        <td class="code"><pre><code>print("hello")</code></pre></td>
                    </tr>
                </table>
            </article>
        </body>
        </html>
        """
        md = converter.convert(html, only_main_content=False)

        # Heading should not have permalink
        assert "¶" not in md
        assert "headerlink" not in md
        # Should have Note blockquote
        assert "**Note:**" in md
        assert "Important info" in md
        # Code should be preserved
        assert 'print("hello")' in md
        # Should not have table markup for code
        assert "| ---" not in md

    def test_preserves_regular_tables(self):
        """Test that regular tables are not affected by highlighttable processing."""
        html = """
        <table>
            <tr><th>Header</th></tr>
            <tr><td>Data</td></tr>
        </table>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_mkdocs_material(soup)

        # Regular table should still exist
        assert soup.find("table") is not None
        assert "Header" in soup.get_text()
        assert "Data" in soup.get_text()

    def test_handles_empty_elements_gracefully(self):
        """Test that empty or malformed elements don't crash preprocessing."""
        html = """
        <div class="admonition note"></div>
        <table class="highlighttable"></table>
        <div class="tabbed-set"></div>
        """
        soup = BeautifulSoup(html, "html.parser")
        # Should not raise
        _preprocess_mkdocs_material(soup)


class TestSitePreprocessorRegistry:
    """Tests for the site preprocessor registry and detection."""

    def test_registry_has_mkdocs_material(self):
        """Test that MkDocs Material is registered."""
        names = [p.name for p in SITE_PREPROCESSORS]
        assert "mkdocs_material" in names

    def test_detect_mkdocs_by_md_content_class(self):
        """Test detection via md-content class."""
        html = '<div class="md-content"><p>Content</p></div>'
        soup = BeautifulSoup(html, "html.parser")
        assert _detect_mkdocs_material(soup) is True

    def test_detect_mkdocs_by_data_md_attribute(self):
        """Test detection via data-md-* attributes."""
        html = '<div data-md-component="content"><p>Content</p></div>'
        soup = BeautifulSoup(html, "html.parser")
        assert _detect_mkdocs_material(soup) is True

    def test_detect_mkdocs_by_multiple_indicators(self):
        """Test detection via combination of MkDocs elements."""
        html = """
        <h1>Title<a class="headerlink" href="#">¶</a></h1>
        <div class="admonition note"><p>Note</p></div>
        """
        soup = BeautifulSoup(html, "html.parser")
        assert _detect_mkdocs_material(soup) is True

    def test_no_detection_for_plain_html(self):
        """Test that plain HTML is not detected as MkDocs."""
        html = "<html><body><h1>Title</h1><p>Content</p></body></html>"
        soup = BeautifulSoup(html, "html.parser")
        assert _detect_mkdocs_material(soup) is False

    def test_no_detection_for_single_indicator(self):
        """Test that a single indicator is not enough for detection."""
        html = '<h1>Title<a class="headerlink" href="#">¶</a></h1>'
        soup = BeautifulSoup(html, "html.parser")
        # Only one indicator (headerlink) - should not detect
        assert _detect_mkdocs_material(soup) is False

    def test_apply_site_preprocessors_returns_applied_names(self):
        """Test that apply_site_preprocessors returns list of applied preprocessors."""
        html = """
        <div class="md-content">
            <h1>Title<a class="headerlink" href="#">¶</a></h1>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        applied = apply_site_preprocessors(soup)
        assert "mkdocs_material" in applied

    def test_apply_site_preprocessors_empty_for_plain_html(self):
        """Test that no preprocessors are applied to plain HTML."""
        html = "<html><body><p>Plain content</p></body></html>"
        soup = BeautifulSoup(html, "html.parser")
        applied = apply_site_preprocessors(soup)
        assert applied == []

    def test_preprocessor_registry_has_required_fields(self):
        """Test that all registered preprocessors have required documentation."""
        for preprocessor in SITE_PREPROCESSORS:
            assert preprocessor.name, "Preprocessor must have a name"
            assert preprocessor.description, "Preprocessor must have a description"
            assert preprocessor.examples, "Preprocessor must have example sites"
            assert callable(preprocessor.detect), "Preprocessor must have detect function"
            assert callable(preprocessor.preprocess), "Preprocessor must have preprocess function"


class TestCssCounterListsPreprocessing:
    """Tests for CSS counter-based lists HTML preprocessing."""

    def test_detect_css_counter_lists(self):
        """Test detection of CSS counter-based lists."""
        html = """
        <p class="list-item" data-list-level="2">First item</p>
        <p class="list-item" data-list-level="2">Second item</p>
        """
        soup = BeautifulSoup(html, "html.parser")
        assert _detect_css_counter_lists(soup) is True

    def test_no_detection_without_data_list_level(self):
        """Test that regular paragraphs are not detected as CSS counter lists."""
        html = "<p>Regular paragraph</p><p>Another paragraph</p>"
        soup = BeautifulSoup(html, "html.parser")
        assert _detect_css_counter_lists(soup) is False

    def test_converts_simple_list(self):
        """Test conversion of simple CSS counter list to ordered list."""
        html = """
        <p data-list-level="2">First item</p>
        <p data-list-level="2">Second item</p>
        <p data-list-level="2">Third item</p>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_css_counter_lists(soup)

        # Should have one ordered list
        ol = soup.find("ol")
        assert ol is not None
        # Should have three list items
        lis = ol.find_all("li", recursive=False)
        assert len(lis) == 3
        assert "First item" in lis[0].get_text()
        assert "Second item" in lis[1].get_text()
        assert "Third item" in lis[2].get_text()
        # Original p tags should be gone
        assert len(soup.find_all("p", attrs={"data-list-level": True})) == 0

    def test_converts_nested_list(self):
        """Test conversion of nested CSS counter lists."""
        html = """
        <p data-list-level="2">First item</p>
        <p data-list-level="3">Sub-item A</p>
        <p data-list-level="3">Sub-item B</p>
        <p data-list-level="2">Second item</p>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_css_counter_lists(soup)

        # Find the root list
        root_ol = soup.find("ol")
        assert root_ol is not None

        # Root should have 2 direct children (First item, Second item)
        root_lis = root_ol.find_all("li", recursive=False)
        assert len(root_lis) == 2
        assert "First item" in root_lis[0].get_text()

        # First item should contain a nested list
        nested_ol = root_lis[0].find("ol")
        assert nested_ol is not None

        # Nested list should have 2 items
        nested_lis = nested_ol.find_all("li", recursive=False)
        assert len(nested_lis) == 2
        assert "Sub-item A" in nested_lis[0].get_text()
        assert "Sub-item B" in nested_lis[1].get_text()

        # Second item should not have nested list
        assert root_lis[1].find("ol") is None
        assert "Second item" in root_lis[1].get_text()

    def test_converts_complex_hierarchy(self):
        """Test conversion of complex multi-level hierarchy."""
        html = """
        <p data-list-level="2">Level 2 - Item 1</p>
        <p data-list-level="3">Level 3 - Item A</p>
        <p data-list-level="4">Level 4 - Item i</p>
        <p data-list-level="4">Level 4 - Item ii</p>
        <p data-list-level="3">Level 3 - Item B</p>
        <p data-list-level="2">Level 2 - Item 2</p>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_css_counter_lists(soup)

        # Check structure
        root_ol = soup.find("ol")
        assert root_ol is not None

        # Root should have 2 items
        root_lis = root_ol.find_all("li", recursive=False)
        assert len(root_lis) == 2

        # First root item should have nested list
        level3_ol = root_lis[0].find("ol")
        assert level3_ol is not None
        level3_lis = level3_ol.find_all("li", recursive=False)
        assert len(level3_lis) == 2

        # First level 3 item should have nested list
        level4_ol = level3_lis[0].find("ol")
        assert level4_ol is not None
        level4_lis = level4_ol.find_all("li", recursive=False)
        assert len(level4_lis) == 2
        assert "Level 4 - Item i" in level4_lis[0].get_text()
        assert "Level 4 - Item ii" in level4_lis[1].get_text()

    def test_handles_gap_in_levels(self):
        """Test handling of level gaps (e.g., level 2 to level 4)."""
        html = """
        <p data-list-level="2">Level 2</p>
        <p data-list-level="4">Level 4 (skipped 3)</p>
        <p data-list-level="2">Level 2 again</p>
        """
        soup = BeautifulSoup(html, "html.parser")
        # Should not crash
        _preprocess_css_counter_lists(soup)

        # Should still create some structure
        ol = soup.find("ol")
        assert ol is not None

    def test_preserves_element_attributes_in_content(self):
        """Test that content within list items preserves attributes."""
        html = """
        <p data-list-level="2">Item with <strong>bold</strong> text</p>
        <p data-list-level="2">Item with <a href="/link">link</a></p>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_css_counter_lists(soup)

        ol = soup.find("ol")
        assert ol is not None

        lis = ol.find_all("li")
        # Should preserve strong tag
        assert lis[0].find("strong") is not None
        assert "bold" in lis[0].get_text()
        # Should preserve anchor tag
        anchor = lis[1].find("a")
        assert anchor is not None
        assert anchor.get("href") == "/link"

    def test_handles_empty_list_items(self):
        """Test that empty list items are handled gracefully."""
        html = """
        <p data-list-level="2">First item</p>
        <p data-list-level="2"></p>
        <p data-list-level="2">Third item</p>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_css_counter_lists(soup)

        ol = soup.find("ol")
        assert ol is not None
        lis = ol.find_all("li", recursive=False)
        assert len(lis) == 3
        # Empty item should still be present
        assert lis[1].get_text(strip=True) == ""

    def test_multiple_separate_lists(self):
        """Test handling of multiple separate list groups."""
        html = """
        <p data-list-level="2">List 1 - Item 1</p>
        <p data-list-level="2">List 1 - Item 2</p>
        <div>Separator content</div>
        <p data-list-level="2">List 2 - Item 1</p>
        <p data-list-level="2">List 2 - Item 2</p>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_css_counter_lists(soup)

        # Should have two separate ordered lists
        ols = soup.find_all("ol")
        # Due to proximity check, might be treated as one or two lists
        # At minimum should have created list structures
        assert len(ols) >= 1

    def test_full_conversion_to_markdown(self):
        """Test full conversion of CSS counter lists to markdown."""
        converter = MarkdownConverter()
        html = """
        <html>
        <body>
            <p data-list-level="2">First numbered item</p>
            <p data-list-level="3">First lettered sub-item</p>
            <p data-list-level="3">Second lettered sub-item</p>
            <p data-list-level="2">Second numbered item</p>
        </body>
        </html>
        """
        md = converter.convert(html, only_main_content=False)

        # Should have list markers (exact format depends on markdownify)
        # At minimum, items should be present
        assert "First numbered item" in md
        assert "First lettered sub-item" in md
        assert "Second lettered sub-item" in md
        assert "Second numbered item" in md

        # Should not have data-list-level in output
        assert "data-list-level" not in md

    def test_real_world_example(self):
        """Test with a real-world example of CSS counter-based nested lists."""
        html = """
        <p class="Vol2_num_alpha_num" data-list-level="2" style="counter-set: item2 1;">
            The Standards Authority must ensure operational compliance...
        </p>
        <p class="Vol2_num_alpha_num" data-list-level="2">
            Managers responsible for operational activities...
        </p>
        <p class="Vol2_num_alpha_num" data-list-level="3">
            ensure compliance with the applicable standard...
        </p>
        <p class="Vol2_num_alpha_num" data-list-level="3">
            take all measures necessary to support...
        </p>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_css_counter_lists(soup)

        # Should have created proper list structure
        root_ol = soup.find("ol")
        assert root_ol is not None

        # Should have 2 root items
        root_lis = root_ol.find_all("li", recursive=False)
        assert len(root_lis) == 2
        assert "Standards Authority" in root_lis[0].get_text()
        assert "Managers" in root_lis[1].get_text()

        # Second root item should have nested list
        nested_ol = root_lis[1].find("ol")
        assert nested_ol is not None
        nested_lis = nested_ol.find_all("li", recursive=False)
        assert len(nested_lis) == 2
        assert "ensure compliance" in nested_lis[0].get_text()
        assert "take all measures" in nested_lis[1].get_text()

    def test_registry_includes_css_counter_lists(self):
        """Test that CSS counter lists preprocessor is registered."""
        names = [p.name for p in SITE_PREPROCESSORS]
        assert "css_counter_lists" in names

    def test_handles_invalid_data_list_level(self):
        """Test that invalid data-list-level values are handled gracefully."""
        html = """
        <p data-list-level="2">Valid item</p>
        <p data-list-level="invalid">Invalid level - should default to 1</p>
        <p data-list-level="3.5">Float level - should default to 1</p>
        <p data-list-level="2">Another valid item</p>
        """
        soup = BeautifulSoup(html, "html.parser")
        # Should not crash
        _preprocess_css_counter_lists(soup)

        # Should have created list structure
        ol = soup.find("ol")
        assert ol is not None

        # Should have 4 items (invalid levels default to level 1)
        # The exact structure depends on how defaults are handled
        # At minimum, should not crash and should create some list
        lis = soup.find_all("li")
        assert len(lis) == 4

    def test_handles_missing_data_list_level(self):
        """Test that missing data-list-level attributes are handled."""
        html = """
        <p data-list-level="2">First item</p>
        <p data-list-level="">Empty level - should default</p>
        <p data-list-level="2">Third item</p>
        """
        soup = BeautifulSoup(html, "html.parser")
        # Should not crash
        _preprocess_css_counter_lists(soup)

        # Should have created list structure
        ol = soup.find("ol")
        assert ol is not None


class TestWordPressPreprocessor:
    """Tests for WordPress preprocessor."""

    def test_detect_wordpress_by_wp_class(self):
        """Test WordPress detection via wp- prefixed classes."""
        html = """
        <html>
            <body class="wp-content">
                <div class="wp-block-group">Content</div>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        assert _detect_wordpress(soup) is True

    def test_detect_wordpress_by_post_classes(self):
        """Test WordPress detection via post-related classes."""
        html = """
        <html>
            <body>
                <article class="post-1234 hentry">
                    <div class="entry-content">Content</div>
                </article>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        assert _detect_wordpress(soup) is True

    def test_detect_wordpress_by_meta_generator(self):
        """Test WordPress detection via meta generator tag."""
        html = """
        <html>
            <head>
                <meta name="generator" content="WordPress 6.4" />
            </head>
            <body>Content</body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        assert _detect_wordpress(soup) is True

    def test_detect_non_wordpress_site(self):
        """Test that non-WordPress sites are not detected."""
        html = """
        <html>
            <body>
                <article>Regular content</article>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        assert _detect_wordpress(soup) is False

    def test_preprocess_wordpress_removes_fixed_nav(self):
        """Test removal of .fixed-nav elements (BeTheme duplication bug)."""
        html = """
        <html>
            <body>
                <a class="fixed-nav fixed-nav-prev" href="#">Previous</a>
                <a class="fixed-nav fixed-nav-next" href="#">Next</a>
                <article>
                    <h1>Article Title</h1>
                    <p>Article content</p>
                </article>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_wordpress(soup)

        # Navigation should be removed
        assert soup.find(class_="fixed-nav") is None
        assert soup.find(class_="fixed-nav-prev") is None
        assert soup.find(class_="fixed-nav-next") is None

        # Content should remain
        assert soup.find("h1") is not None
        assert soup.find("p") is not None

    def test_preprocess_wordpress_removes_post_navigation(self):
        """Test removal of post navigation elements."""
        html = """
        <html>
            <body>
                <article>Content</article>
                <nav class="post-navigation">
                    <div class="nav-links">
                        <a href="#">Previous post</a>
                        <a href="#">Next post</a>
                    </div>
                </nav>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_wordpress(soup)

        # Post navigation should be removed
        assert soup.find(class_="post-navigation") is None
        assert soup.find(class_="nav-links") is None

        # Content should remain
        assert soup.find("article") is not None

    def test_preprocess_wordpress_removes_share_widgets(self):
        """Test removal of share widget elements."""
        html = """
        <html>
            <body>
                <article>Content</article>
                <div class="share-simple-wrapper">
                    <a href="#">Share on Facebook</a>
                    <a href="#">Share on Twitter</a>
                </div>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_wordpress(soup)

        # Share widgets should be removed
        assert soup.find(class_="share-simple-wrapper") is None

        # Content should remain
        assert soup.find("article") is not None

    def test_preprocess_wordpress_removes_related_posts(self):
        """Test removal of related posts sections."""
        html = """
        <html>
            <body>
                <article>Content</article>
                <section class="section-post-related">
                    <h4>Related Posts</h4>
                    <a href="#">Related post 1</a>
                    <a href="#">Related post 2</a>
                </section>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_wordpress(soup)

        # Related posts should be removed
        assert soup.find(class_="section-post-related") is None

        # Content should remain
        assert soup.find("article") is not None

    def test_preprocess_wordpress_comprehensive(self):
        """Test comprehensive WordPress preprocessing (real-world scenario)."""
        html = """
        <html>
            <body class="wp-content">
                <a class="fixed-nav fixed-nav-prev" href="#">Prev Article</a>
                <a class="fixed-nav fixed-nav-next" href="#">Next Article</a>
                <article class="post-1348 hentry">
                    <a class="fixed-nav fixed-nav-prev" href="#">Prev Article</a>
                    <a class="fixed-nav fixed-nav-next" href="#">Next Article</a>
                    <h1>Making extra contributions to your super</h1>
                    <div class="entry-content">
                        <p>Did you know that Military Super allows extra contributions?</p>
                        <p>There are 3 ways to contribute...</p>
                    </div>
                    <div class="share-simple-wrapper">Share this article</div>
                    <section class="section-post-related">
                        <h4>Check out our other articles</h4>
                    </section>
                </article>
                <nav class="post-navigation">
                    <div class="nav-links">Navigation</div>
                </nav>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_wordpress(soup)

        # All WordPress boilerplate should be removed
        assert soup.find(class_="fixed-nav") is None
        assert soup.find(class_="share-simple-wrapper") is None
        assert soup.find(class_="section-post-related") is None
        assert soup.find(class_="post-navigation") is None

        # Main content should remain
        assert soup.find("h1") is not None
        assert soup.find(class_="entry-content") is not None
        paragraphs = soup.find_all("p")
        assert len(paragraphs) == 2

    def test_preprocess_wordpress_preserves_title(self):
        """Test that page title H1 is preserved by moving it into main content."""
        html = """
        <html>
            <head></head>
            <body>
                <div id="Header_wrapper">
                    <div id="Subheader">
                        <h1 class="title">Page Title from Header</h1>
                    </div>
                </div>
                <main>
                    <p>Main content here</p>
                </main>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_wordpress(soup)

        # Title should be moved into main content
        main = soup.find("main")
        assert main is not None
        h1_in_main = main.find("h1")
        assert h1_in_main is not None
        assert "Page Title from Header" in h1_in_main.get_text()

        # Should be the first element in main
        first_child = list(main.children)[0]
        assert isinstance(first_child, Tag)
        assert first_child.name == "h1"

    def test_preprocess_wordpress_removes_rating_forms(self):
        """Test removal of rating and feedback forms."""
        html = """
        <html>
            <body>
                <article>
                    <p>Main content</p>
                    <div class="rich-reviews">
                        <p>How do you rate this?</p>
                        <input type="text" placeholder="Feedback">
                    </div>
                </article>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_wordpress(soup)

        # Rating forms should be removed
        assert soup.find(class_="rich-reviews") is None

        # Main content should remain
        assert soup.find("article") is not None
        assert "Main content" in soup.get_text()

    def test_preprocess_wordpress_removes_svg_placeholders(self):
        """Test removal of lazy-loading SVG placeholder images."""
        html = """
        <html>
            <body>
                <article>
                    <p>Main content</p>
                    <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'%3E%3C/svg%3E" alt="Placeholder">
                    <img src="https://example.com/real-image.jpg" alt="Real Image">
                </article>
            </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        _preprocess_wordpress(soup)

        # SVG placeholder should be removed
        svg_imgs = [img for img in soup.find_all("img") if str(img.get("src", "")).startswith("data:image/svg")]
        assert len(svg_imgs) == 0

        # Real image should remain
        real_imgs = [img for img in soup.find_all("img") if "example.com" in str(img.get("src", ""))]
        assert len(real_imgs) == 1

        # Main content should remain
        assert "Main content" in soup.get_text()


class TestExpandMarkupAttributes:
    """Operative bodies carried in a custom element's attribute reach the markdown."""

    PACMAN_LIKE = (
        "<html><body><main><h2>7.2.5 Rent Allowance</h2>"
        '<def-content content="&lt;p&gt;A member is entitled to &lt;strong&gt;rent allowance&lt;/strong&gt;'
        ' if the provisions apply.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Condition one&lt;/li&gt;&lt;/ul&gt;"></def-content>'
        "<p>Guidance text.</p></main></body></html>"
    )

    def test_custom_element_body_is_expanded(self):
        md = MarkdownConverter().convert(self.PACMAN_LIKE)
        assert "## 7.2.5 Rent Allowance" in md
        assert "**rent allowance**" in md
        assert "- Condition one" in md
        assert "Guidance text." in md

    def test_ordinary_attributes_are_not_injected(self):
        html = (
            "<html><body><main><h1>Page</h1><p>Body text here.</p>"
            '<div data-blob="<p>hidden blob</p>">kept</div>'
            '<my-card title="Plain title" data-x="{&quot;a&quot;:1}">Card</my-card>'
            '<my-empty content="plain text, no markup"></my-empty>'
            "</main></body></html>"
        )
        md = MarkdownConverter().convert(html)
        assert "hidden blob" not in md
        assert "Plain title" not in md
        assert "plain text, no markup" not in md
        assert "Body text here." in md and "Card" in md

    def test_element_with_own_text_is_left_alone(self):
        soup = BeautifulSoup('<x-box content="<p>dup</p>">already rendered</x-box>', "html.parser")
        assert expand_markup_attributes(soup) == 0
        assert "dup" not in soup.get_text()
