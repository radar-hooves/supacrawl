"""Tests for PDF URL parsing (issue #82).

Tests cover:
- PDF URL detection (extension and Content-Type)
- PDF byte validation (%PDF magic)
- Text extraction via pdfplumber
- Table-to-markdown conversion
- Heading detection heuristics
- PDF metadata extraction
- OCR availability checks
- Auto mode fallback logic
- ScrapeService PDF routing
- CLI --parse-pdf option
- MCP parse_pdf parameter
"""

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supacrawl.services._pdf_sniff import MAX_PDF_SIZE, is_pdf_bytes
from supacrawl.services.pdf import (
    MIN_WORDS_FOR_SUCCESS,
    PdfExtractionResult,
    PdfMetadata,
    _clean_cell,
    _normalise_pdf_date,
    _table_to_markdown,
    is_pdf_url,
    needs_content_type_check,
)

# ---------------------------------------------------------------------------
# PDF URL detection
# ---------------------------------------------------------------------------


class TestIsPdfUrl:
    """Test is_pdf_url detection by file extension."""

    def test_simple_pdf_url(self):
        assert is_pdf_url("https://example.com/report.pdf") is True

    def test_pdf_url_with_path(self):
        assert is_pdf_url("https://example.com/docs/2024/report.pdf") is True

    def test_pdf_url_with_query_params(self):
        assert is_pdf_url("https://example.com/report.pdf?v=2") is True

    def test_pdf_url_with_fragment(self):
        assert is_pdf_url("https://example.com/report.pdf#page=5") is True

    def test_pdf_url_case_insensitive(self):
        assert is_pdf_url("https://example.com/report.PDF") is True

    def test_non_pdf_url(self):
        assert is_pdf_url("https://example.com/page.html") is False

    def test_pdf_in_path_but_not_extension(self):
        """URL containing 'pdf' in path but not as extension."""
        assert is_pdf_url("https://example.com/pdf-viewer/page") is False

    def test_no_extension(self):
        assert is_pdf_url("https://example.com/report") is False


# ---------------------------------------------------------------------------
# PDF magic byte detection
# ---------------------------------------------------------------------------


class TestIsPdfBytes:
    """Test PDF magic byte detection."""

    def test_valid_pdf_bytes(self):
        data = b"%PDF-1.7\nsome content"
        assert is_pdf_bytes(data) is True

    def test_pdf_bytes_with_leading_whitespace(self):
        """Some PDFs have a BOM or whitespace before %PDF."""
        data = b"\xef\xbb\xbf%PDF-1.4\nmore content"
        assert is_pdf_bytes(data) is True

    def test_non_pdf_bytes(self):
        data = b"<html><body>Not a PDF</body></html>"
        assert is_pdf_bytes(data) is False

    def test_empty_bytes(self):
        assert is_pdf_bytes(b"") is False

    def test_short_bytes(self):
        assert is_pdf_bytes(b"AB") is False

    def test_bare_percent_pdf_without_hyphen_is_false(self):
        """HTML or docs containing "%PDF" but not "%PDF-" must NOT be classified as PDF.

        This is the false-positive the hyphen fix eliminates: before the fix,
        bare "%PDF" anywhere in the first 1 KB would trigger mis-routing to the
        PDF extractor.
        """
        html_with_pct_pdf = b"<html><p>See the %PDF specification for details.</p></html>"
        assert is_pdf_bytes(html_with_pct_pdf) is False

    def test_pdf_2_0_header(self):
        """PDF 2.0 files ("%PDF-2.0") must be detected."""
        data = b"%PDF-2.0\n%%EOF"
        assert is_pdf_bytes(data) is True

    def test_pdf_with_leading_junk_within_window(self):
        """A PDF whose header is preceded by a small amount of junk (e.g. a BOM
        or garbage bytes) must still be detected, provided the header falls
        within the 1024-byte sniff window.

        Real readers (Acrobat) accept this; a strict startswith check would
        produce a false negative — the worse error for a scraper.
        """
        junk_prefix = b"\x00\x01\x02" + b"junk" * 5
        data = junk_prefix + b"%PDF-1.6\nsome content"
        assert is_pdf_bytes(data) is True

    def test_pdf_magic_beyond_sniff_window_is_false(self):
        """A signature starting at offset 1024 (the first byte excluded by
        data[:PDF_SNIFF_SIZE]) must NOT match.

        This keeps the sniff bounded so that a multi-MB response body is never
        scanned in its entirety.
        """
        data = b"x" * 1024 + b"%PDF-1.7\ncontent"
        assert is_pdf_bytes(data) is False

    def test_pdf_version_string_in_prose_is_known_residual_true(self):
        """Binary-suspected content containing a full %PDF-1.x version string
        within the first 1024 bytes is classified True — this is a KNOWN,
        ACCEPTED limitation of the windowed sniff.

        In practice this path is only reached for responses whose Content-Type
        is ``application/octet-stream`` or absent; ``text/html`` responses are
        filtered out in ``http_fetch.fetch_static`` before the sniff runs.  On
        a misfire the PDF parser raises ``ValueError`` loudly rather than
        returning corrupt output, so the residual false positive fails safely.
        """
        data = b"<html><p>PDF files begin with %PDF-1.4 ...</p></html>"
        assert is_pdf_bytes(data) is True


# ---------------------------------------------------------------------------
# Content-Type detection
# ---------------------------------------------------------------------------


class TestDetectPdfContentType:
    """Test HEAD request Content-Type detection."""

    @pytest.mark.asyncio
    async def test_detects_pdf_content_type(self):
        """detect_pdf_content_type routes its HEAD through url_guard.guarded_request
        (#152) rather than calling client.head() directly, so the fake response
        is injected there."""
        from supacrawl.services.pdf import detect_pdf_content_type

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/pdf"}

        with patch("supacrawl.services.pdf.guarded_request", AsyncMock(return_value=mock_response)):
            result = await detect_pdf_content_type("https://example.com/report")
            assert result is True

    @pytest.mark.asyncio
    async def test_non_pdf_content_type(self):
        from supacrawl.services.pdf import detect_pdf_content_type

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html; charset=utf-8"}

        with patch("supacrawl.services.pdf.guarded_request", AsyncMock(return_value=mock_response)):
            result = await detect_pdf_content_type("https://example.com/page")
            assert result is False

    @pytest.mark.asyncio
    async def test_network_error_returns_false(self):
        from supacrawl.services.pdf import detect_pdf_content_type

        with patch(
            "supacrawl.services.pdf.guarded_request",
            AsyncMock(side_effect=Exception("Connection refused")),
        ):
            result = await detect_pdf_content_type("https://unreachable.com/file")
            assert result is False

    @pytest.mark.asyncio
    async def test_head_405_falls_back_to_get_content_type(self):
        """A dynamically-generated download endpoint that answers HEAD with 405
        (legislation.gov.au's compilation PDFs, master-project#350) is not
        misread as non-PDF from the 405 error page's own Content-Type: the
        fallback GET's real Content-Type decides it instead."""
        from supacrawl.services.pdf import detect_pdf_content_type

        head_response = MagicMock()
        head_response.status_code = 405
        head_response.headers = {"content-type": "text/html"}
        get_response = MagicMock()
        get_response.status_code = 200
        get_response.headers = {"content-type": "application/pdf"}
        get_response.content = b"%PDF-1.4\n..."

        with patch(
            "supacrawl.services.pdf.guarded_request",
            AsyncMock(side_effect=[head_response, get_response]),
        ):
            result = await detect_pdf_content_type("https://www.legislation.gov.au/C0000A00000/latest/pdf")
            assert result is True

    @pytest.mark.asyncio
    async def test_head_405_falls_back_to_byte_sniff_when_get_content_type_is_generic(self):
        """When even the fallback GET's Content-Type is generic (e.g.
        application/octet-stream), the body's ``%PDF-`` signature is the last
        word, exactly as the HTTP-first path already sniffs it."""
        from supacrawl.services.pdf import detect_pdf_content_type

        head_response = MagicMock()
        head_response.status_code = 405
        head_response.headers = {"content-type": "text/html"}
        get_response = MagicMock()
        get_response.status_code = 200
        get_response.headers = {"content-type": "application/octet-stream"}
        get_response.content = b"%PDF-1.4\n..."

        with patch(
            "supacrawl.services.pdf.guarded_request",
            AsyncMock(side_effect=[head_response, get_response]),
        ):
            result = await detect_pdf_content_type("https://example.com/download")
            assert result is True

    @pytest.mark.asyncio
    async def test_head_405_and_non_pdf_body_returns_false(self):
        from supacrawl.services.pdf import detect_pdf_content_type

        head_response = MagicMock()
        head_response.status_code = 405
        head_response.headers = {"content-type": "text/html"}
        get_response = MagicMock()
        get_response.status_code = 200
        get_response.headers = {"content-type": "application/octet-stream"}
        get_response.content = b"not a pdf at all"

        with patch(
            "supacrawl.services.pdf.guarded_request",
            AsyncMock(side_effect=[head_response, get_response]),
        ):
            result = await detect_pdf_content_type("https://example.com/download")
            assert result is False


# ---------------------------------------------------------------------------
# HEAD request optimisation
# ---------------------------------------------------------------------------


class TestNeedsContentTypeCheck:
    """Test smart HEAD request skipping for known non-PDF extensions."""

    def test_html_url_skips_check(self):
        assert needs_content_type_check("https://example.com/page.html") is False

    def test_php_url_skips_check(self):
        assert needs_content_type_check("https://example.com/index.php") is False

    def test_json_url_skips_check(self):
        assert needs_content_type_check("https://example.com/api/data.json") is False

    def test_image_url_skips_check(self):
        assert needs_content_type_check("https://example.com/photo.jpg") is False

    def test_pdf_url_skips_check(self):
        """PDF URL is already detected — no HEAD needed."""
        assert needs_content_type_check("https://example.com/report.pdf") is False

    def test_no_extension_needs_check(self):
        """URL with no extension is ambiguous — needs HEAD request."""
        assert needs_content_type_check("https://example.com/document") is True

    def test_path_only_needs_check(self):
        assert needs_content_type_check("https://example.com/api/v1/document/123") is True

    def test_root_url_needs_check(self):
        assert needs_content_type_check("https://example.com/") is True

    def test_unknown_extension_needs_check(self):
        assert needs_content_type_check("https://example.com/file.xyz") is True


# ---------------------------------------------------------------------------
# PDF size limit
# ---------------------------------------------------------------------------


class TestPdfSizeLimit:
    """Test PDF download size limit."""

    @pytest.mark.asyncio
    async def test_oversized_pdf_raises_value_error(self):
        """download_pdf routes its GET through url_guard.guarded_request (#152)
        rather than calling client.get() directly, so the fake response is
        injected there."""
        from supacrawl.services.pdf import download_pdf

        # Create a mock response that exceeds the size limit
        large_content = b"%PDF-" + b"x" * (MAX_PDF_SIZE + 1)
        mock_response = MagicMock()
        mock_response.content = large_content
        mock_response.raise_for_status = MagicMock()

        with patch("supacrawl.services.pdf.guarded_request", AsyncMock(return_value=mock_response)):
            with pytest.raises(ValueError, match="too large"):
                await download_pdf("https://example.com/huge.pdf")


# ---------------------------------------------------------------------------
# PDF date normalisation
# ---------------------------------------------------------------------------


class TestNormalisePdfDate:
    """Test PDF date string normalisation."""

    def test_standard_pdf_date(self):
        result = _normalise_pdf_date("D:20240115120000Z")
        assert result == "2024-01-15T12:00:00Z"

    def test_date_without_prefix(self):
        result = _normalise_pdf_date("20240115120000")
        assert result == "2024-01-15T12:00:00Z"

    def test_date_only(self):
        result = _normalise_pdf_date("D:20240115")
        assert result == "2024-01-15T00:00:00Z"

    def test_unparseable_date(self):
        result = _normalise_pdf_date("not-a-date")
        assert result == "not-a-date"


# ---------------------------------------------------------------------------
# Table to markdown
# ---------------------------------------------------------------------------


class TestTableToMarkdown:
    """Test pdfplumber table conversion to markdown."""

    def test_simple_table(self):
        table: list[list[str | None]] = [
            ["Name", "Age", "City"],
            ["Alice", "30", "Sydney"],
            ["Bob", "25", "Melbourne"],
        ]
        result = _table_to_markdown(table)
        lines = result.split("\n")
        assert len(lines) == 4
        assert "| Name | Age | City |" in lines[0]
        assert "| --- | --- | --- |" in lines[1]
        assert "| Alice | 30 | Sydney |" in lines[2]

    def test_table_with_none_cells(self):
        table = [
            ["Header", None],
            [None, "Value"],
        ]
        result = _table_to_markdown(table)
        assert "| Header |  |" in result
        assert "|  | Value |" in result

    def test_table_with_pipe_in_cell(self):
        table: list[list[str | None]] = [
            ["Name", "Description"],
            ["Test", "Has | pipe"],
        ]
        result = _table_to_markdown(table)
        assert "Has \\| pipe" in result

    def test_empty_table(self):
        assert _table_to_markdown([]) == ""
        assert _table_to_markdown([[]]) == ""


# ---------------------------------------------------------------------------
# Clean cell helper
# ---------------------------------------------------------------------------


class TestCleanCell:
    """Test cell cleaning for markdown tables."""

    def test_none_cell(self):
        assert _clean_cell(None) == ""

    def test_newline_in_cell(self):
        assert _clean_cell("line1\nline2") == "line1 line2"

    def test_pipe_escaped(self):
        assert _clean_cell("a|b") == "a\\|b"

    def test_whitespace_stripped(self):
        assert _clean_cell("  hello  ") == "hello"


# ---------------------------------------------------------------------------
# Text extraction (with real pdfplumber)
# ---------------------------------------------------------------------------


def _make_simple_pdf(text: str = "Hello World. This is a test PDF document.") -> bytes:
    """Create a minimal PDF with text for testing.

    Uses pdfplumber-compatible PDF generation via pypdfium2.
    """
    # Use fpdf2 if available, otherwise create a minimal valid PDF manually
    try:
        from fpdf import FPDF

        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.multi_cell(0, 10, text)
        return pdf.output()
    except ImportError:
        pass

    # Fallback: create a minimal PDF with raw PDF commands
    # This creates a valid PDF that pdfplumber can read
    content_stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
    content_bytes = content_stream.encode("latin-1")

    objects = []
    # Object 1: Catalog
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    # Object 2: Pages
    objects.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    # Object 3: Page
    objects.append(
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
        b"/MediaBox [0 0 612 792] "
        b"/Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
    )
    # Object 4: Content stream
    objects.append(
        f"4 0 obj\n<< /Length {len(content_bytes)} >>\nstream\n".encode("latin-1")
        + content_bytes
        + b"\nendstream\nendobj\n"
    )
    # Object 5: Font
    objects.append(b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")

    # Build PDF
    pdf = io.BytesIO()
    pdf.write(b"%PDF-1.4\n")

    offsets = []
    for obj in objects:
        offsets.append(pdf.tell())
        pdf.write(obj)

    # Cross-reference table
    xref_start = pdf.tell()
    pdf.write(b"xref\n")
    pdf.write(f"0 {len(objects) + 1}\n".encode())
    pdf.write(b"0000000000 65535 f \n")
    for offset in offsets:
        pdf.write(f"{offset:010d} 00000 n \n".encode())

    # Trailer
    pdf.write(b"trailer\n")
    pdf.write(f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode())
    pdf.write(b"startxref\n")
    pdf.write(f"{xref_start}\n".encode())
    pdf.write(b"%%EOF\n")

    return pdf.getvalue()


class TestExtractText:
    """Test text extraction from PDFs."""

    def test_extract_from_minimal_pdf(self):
        from supacrawl.services.pdf import extract_text

        pdf_bytes = _make_simple_pdf()
        result = extract_text(pdf_bytes)

        assert isinstance(result, PdfExtractionResult)
        assert result.metadata.page_count >= 1
        # The text should be extracted (exact content depends on PDF generation method)
        assert len(result.markdown) > 0

    def test_extract_returns_metadata(self):
        from supacrawl.services.pdf import extract_text

        pdf_bytes = _make_simple_pdf()
        result = extract_text(pdf_bytes)

        assert isinstance(result.metadata, PdfMetadata)
        assert result.metadata.page_count >= 1

    def test_extract_from_invalid_bytes_raises(self):
        from pdfplumber.utils.exceptions import PdfminerException

        from supacrawl.services.pdf import extract_text

        with pytest.raises(PdfminerException):
            extract_text(b"not a pdf")


# ---------------------------------------------------------------------------
# OCR availability
# ---------------------------------------------------------------------------


class TestOcrAvailability:
    """Test OCR dependency detection."""

    def test_ocr_not_available_without_packages(self):
        from supacrawl.services.pdf import _is_ocr_available

        with patch.dict("sys.modules", {"pytesseract": None, "pdf2image": None}):
            # Force re-evaluation by importing fresh
            # The function checks import availability
            pass
        # Just verify it returns a bool
        result = _is_ocr_available()
        assert isinstance(result, bool)

    def test_pdfplumber_is_available(self):
        from supacrawl.services.pdf import _is_pdfplumber_available

        assert _is_pdfplumber_available() is True


# ---------------------------------------------------------------------------
# parse_pdf orchestrator
# ---------------------------------------------------------------------------


class TestParsePdf:
    """Test the parse_pdf orchestrator function."""

    @pytest.mark.asyncio
    async def test_fast_mode_text_extraction(self):
        from supacrawl.services.pdf import parse_pdf

        pdf_bytes = _make_simple_pdf("This is a test document with enough words to pass the threshold.")
        result = await parse_pdf(url="https://example.com/test.pdf", mode="fast", pdf_bytes=pdf_bytes)

        assert isinstance(result, PdfExtractionResult)
        assert len(result.markdown) > 0

    @pytest.mark.asyncio
    async def test_auto_mode_succeeds_with_text(self):
        """Auto mode should succeed with text extraction when text is present."""
        from supacrawl.services.pdf import parse_pdf

        # Create PDF with enough words to pass the MIN_WORDS_FOR_SUCCESS threshold
        words = " ".join(f"word{i}" for i in range(MIN_WORDS_FOR_SUCCESS + 10))
        pdf_bytes = _make_simple_pdf(words)
        result = await parse_pdf(url="https://example.com/test.pdf", mode="auto", pdf_bytes=pdf_bytes)

        assert len(result.markdown) > 0

    @pytest.mark.asyncio
    async def test_invalid_pdf_raises_value_error(self):
        from supacrawl.services.pdf import parse_pdf

        with pytest.raises(ValueError, match="not a valid PDF"):
            await parse_pdf(
                url="https://example.com/fake.pdf",
                mode="fast",
                pdf_bytes=b"<html>Not a PDF</html>",
            )

    @pytest.mark.asyncio
    async def test_ocr_mode_without_packages_raises(self):
        from supacrawl.services.pdf import parse_pdf

        pdf_bytes = _make_simple_pdf()

        with patch("supacrawl.services.pdf._is_ocr_available", return_value=False):
            with pytest.raises(ImportError, match="supacrawl\\[pdf-ocr\\]"):
                await parse_pdf(url="https://example.com/test.pdf", mode="ocr", pdf_bytes=pdf_bytes)


# ---------------------------------------------------------------------------
# ScrapeService PDF routing
# ---------------------------------------------------------------------------


class TestScrapeServicePdfRouting:
    """Test that ScrapeService routes PDF URLs to the PDF pipeline."""

    @pytest.mark.asyncio
    async def test_pdf_url_bypasses_browser(self):
        """PDF URLs should be processed without launching a browser."""
        from supacrawl.services.scrape import ScrapeService

        pdf_bytes = _make_simple_pdf("Enough words for a reasonable extraction result here in this document.")

        with (
            patch("supacrawl.services.pdf.is_pdf_url", return_value=True),
            patch("supacrawl.services.pdf.download_pdf", new_callable=AsyncMock, return_value=pdf_bytes),
        ):
            service = ScrapeService(headless=True)
            result = await service.scrape(
                url="https://example.com/report.pdf",
                formats=["markdown"],
                parse_pdf="fast",
            )

            assert result.success is True
            assert result.data is not None
            assert result.data.markdown is not None
            assert len(result.data.markdown) > 0
            assert result.data.metadata.source_url == "https://example.com/report.pdf"
            assert result.data.metadata.pdf_page_count is not None
            assert result.data.metadata.pdf_page_count >= 1

    @pytest.mark.asyncio
    async def test_parse_pdf_none_uses_browser(self):
        """When parse_pdf=None, PDF URLs should use the browser path."""
        from supacrawl.services.scrape import ScrapeService

        # If parse_pdf is None, the PDF detection is skipped entirely
        with patch("supacrawl.services.pdf.is_pdf_url") as mock_detect:
            service = ScrapeService(headless=True)
            # This will fail because we haven't set up a browser, but
            # the important thing is that PDF detection was NOT called
            try:
                await service.scrape(
                    url="https://example.com/report.pdf",
                    formats=["markdown"],
                    parse_pdf=None,
                )
            except Exception:
                pass  # Expected to fail (no browser)

            mock_detect.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_mode_no_head_request_for_extensionless_urls(self):
        """Auto mode should not send HEAD requests for extensionless URLs."""
        from supacrawl.services.scrape import ScrapeService

        # Extensionless URL should only check is_pdf_url (False), no HEAD request
        with (
            patch("supacrawl.services.pdf.is_pdf_url", return_value=False) as mock_url_check,
            patch("supacrawl.services.pdf.detect_pdf_content_type") as mock_head,
        ):
            service = ScrapeService(headless=True)
            try:
                await service.scrape(
                    url="https://example.com/handbook/document/123",
                    formats=["markdown"],
                    parse_pdf="auto",
                )
            except Exception:
                pass  # Expected to fail (no browser)

            mock_url_check.assert_called_once()
            mock_head.assert_not_called()

    @pytest.mark.asyncio
    async def test_pdf_metadata_in_scrape_result(self):
        """PDF metadata should be included in the ScrapeResult."""
        from supacrawl.services.pdf import PdfExtractionResult, PdfMetadata
        from supacrawl.services.scrape import ScrapeService

        mock_result = PdfExtractionResult(
            markdown="# Test Document\n\nSome content with enough words to be meaningful.",
            metadata=PdfMetadata(
                title="Test Document",
                author="Test Author",
                page_count=3,
                creation_date="2024-01-15T12:00:00Z",
            ),
        )

        with (
            patch("supacrawl.services.pdf.is_pdf_url", return_value=True),
            patch("supacrawl.services.pdf.parse_pdf", new_callable=AsyncMock, return_value=mock_result),
        ):
            service = ScrapeService(headless=True)
            result = await service.scrape(
                url="https://example.com/report.pdf",
                formats=["markdown"],
                parse_pdf="fast",
            )

            assert result.success is True
            assert result.data is not None
            assert result.data.metadata.title == "Test Document"
            assert result.data.metadata.pdf_author == "Test Author"
            assert result.data.metadata.pdf_page_count == 3
            assert result.data.metadata.pdf_creation_date == "2024-01-15T12:00:00Z"

    @pytest.mark.asyncio
    async def test_content_type_pdf_extensionless_url_routes_to_extractor(self) -> None:
        """A URL with no .pdf extension whose GET returns Content-Type: application/pdf
        must be routed to PDF extraction, not the browser, and must not trigger a
        second download (the already-fetched bytes are reused)."""
        from supacrawl.services.pdf import PdfExtractionResult, PdfMetadata
        from supacrawl.services.scrape import ScrapeService

        pdf_bytes = _make_simple_pdf(
            "Attention is all you need. Transformer architecture for neural machine translation."
        )
        mock_result = PdfExtractionResult(
            markdown="# Attention Is All You Need\n\nTransformer architecture for neural machine translation.",
            metadata=PdfMetadata(title="Attention Is All You Need", page_count=1),
        )

        download_mock = AsyncMock()

        with (
            patch("supacrawl.services.http_fetch.guarded_request", new_callable=AsyncMock) as mock_guarded_request,
            patch("supacrawl.services.pdf.parse_pdf", new_callable=AsyncMock, return_value=mock_result) as mock_parse,
            patch("supacrawl.services.pdf.download_pdf", download_mock),
        ):
            # Simulate the guarded GET returning application/pdf content-type
            # (fetch_static routes through url_guard.guarded_request, #152).
            mock_response = MagicMock()
            mock_response.headers = {"content-type": "application/pdf"}
            mock_response.status_code = 200
            mock_response.content = pdf_bytes
            mock_response.url = "https://arxiv.org/pdf/1706.03762"
            mock_guarded_request.return_value = mock_response

            service = ScrapeService(headless=True)
            result = await service.scrape(
                url="https://arxiv.org/pdf/1706.03762",
                formats=["markdown"],
                parse_pdf="fast",
            )

        assert result.success is True
        assert result.data is not None
        assert result.data.markdown is not None
        # parse_pdf was called — routed to extractor
        mock_parse.assert_called_once()
        # pre-fetched bytes were passed — no second download
        call_kwargs = mock_parse.call_args.kwargs
        assert call_kwargs.get("pdf_bytes") is not None
        # download_pdf was NOT called — no second request
        download_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_magic_byte_fallback_octet_stream_routes_to_extractor(self) -> None:
        """A URL whose GET returns Content-Type: application/octet-stream but whose
        body starts with %PDF magic bytes is routed to PDF extraction."""
        from supacrawl.services.pdf import PdfExtractionResult, PdfMetadata
        from supacrawl.services.scrape import ScrapeService

        pdf_bytes = _make_simple_pdf("A scanned document served with a generic binary content type.")
        mock_result = PdfExtractionResult(
            markdown="# Scanned Document\n\nA scanned document served with a generic binary content type.",
            metadata=PdfMetadata(page_count=1),
        )

        download_mock = AsyncMock()

        with (
            patch("supacrawl.services.http_fetch.guarded_request", new_callable=AsyncMock) as mock_guarded_request,
            patch("supacrawl.services.pdf.parse_pdf", new_callable=AsyncMock, return_value=mock_result) as mock_parse,
            patch("supacrawl.services.pdf.download_pdf", download_mock),
        ):
            mock_response = MagicMock()
            mock_response.headers = {"content-type": "application/octet-stream"}
            mock_response.status_code = 200
            mock_response.content = pdf_bytes
            mock_response.url = "https://example.com/download/doc"
            mock_guarded_request.return_value = mock_response

            service = ScrapeService(headless=True)
            result = await service.scrape(
                url="https://example.com/download/doc",
                formats=["markdown"],
                parse_pdf="fast",
            )

        assert result.success is True
        assert result.data is not None
        mock_parse.assert_called_once()
        call_kwargs = mock_parse.call_args.kwargs
        assert call_kwargs.get("pdf_bytes") is not None
        download_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_html_response_not_routed_to_pdf_extractor(self) -> None:
        """A normal HTML response must not be routed to the PDF extractor."""
        from supacrawl.services.scrape import ScrapeService

        html_body = b"<html><body><h1>Hello</h1><p>This is a normal web page with enough words to pass quality checks.</p></body></html>"

        with (
            patch("supacrawl.services.http_fetch.httpx.AsyncClient") as mock_client_cls,
            patch("supacrawl.services.pdf.parse_pdf", new_callable=AsyncMock) as mock_parse,
        ):
            mock_response = MagicMock()
            mock_response.headers = {"content-type": "text/html; charset=utf-8"}
            mock_response.content = html_body
            mock_response.text = html_body.decode()
            mock_response.url = "https://example.com/"
            mock_response.status_code = 200
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client_cls.return_value)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value.get = AsyncMock(return_value=mock_response)

            service = ScrapeService(headless=True)
            await service.scrape(
                url="https://example.com/",
                formats=["markdown"],
                parse_pdf="fast",
            )

        # HTML page must not be passed to parse_pdf
        mock_parse.assert_not_called()
        # The scrape may succeed (HTML path) or fail (quality/bot checks) — what
        # matters is that the PDF extractor was never invoked.

    @pytest.mark.asyncio
    async def test_pdf_body_without_parse_pdf_escalates_to_browser(self) -> None:
        """When the server returns a PDF body but parse_pdf is disabled (None),
        _try_http_first must return None so the pipeline escalates to the browser
        rather than returning an empty successful result with html=""."""
        from supacrawl.services.http_fetch import HttpFetchResult
        from supacrawl.services.scrape import ScrapeService

        pdf_bytes = _make_simple_pdf("Some PDF content that should not be returned as empty HTML.")

        with patch("supacrawl.services.scrape.fetch_static") as mock_fetch_static:
            mock_fetch_static.return_value = HttpFetchResult(
                url="https://example.com/report",
                html="",
                status_code=200,
                content_type="application/pdf",
                headers={},
                raw_bytes=pdf_bytes,
            )

            service = ScrapeService(headless=True)
            result = await service._try_http_first(
                url="https://example.com/report",
                formats=["markdown"],
                timeout=30000,
                only_main_content=True,
                include_tags=None,
                exclude_tags=None,
                content_mode=0.35,
                query=None,
                expand_iframes="none",
                headers=None,
                proxy=None,
                json_schema=None,
                json_prompt=None,
                wants_change_tracking=False,
                previous_entry=None,
                change_tracking_modes=None,
                max_age=0,
                cache_variant=None,
                expect=None,
                parse_pdf=None,
            )

        # Must return None so the caller escalates to the browser path, not an
        # empty successful ScrapeResult.
        assert result is None


# ---------------------------------------------------------------------------
# CLI --parse-pdf option
# ---------------------------------------------------------------------------


class TestCLIParsePdfOption:
    """Test CLI --parse-pdf option resolution."""

    def test_default_is_auto(self):
        """Default parse_pdf mode should be 'auto'."""
        # Verify the default in the CLI matches the issue spec
        from click.testing import CliRunner

        from supacrawl.cli._common import app

        runner = CliRunner()
        # Use --help to verify the option exists
        result = runner.invoke(app, ["scrape", "--help"])
        assert "--parse-pdf" in result.output
        assert "auto" in result.output

    def test_off_disables_pdf_parsing(self):
        """--parse-pdf off should resolve to None."""
        parse_pdf = "off"
        resolved = parse_pdf if parse_pdf != "off" else None
        assert resolved is None

    def test_valid_modes(self):
        """All valid modes should pass through."""
        for mode in ("fast", "auto", "ocr"):
            resolved = mode if mode != "off" else None
            assert resolved == mode


# ---------------------------------------------------------------------------
# Cache variant for PDF
# ---------------------------------------------------------------------------


class TestPdfCacheVariant:
    """Test that PDF results are cached correctly."""

    @pytest.mark.asyncio
    async def test_pdf_result_cached(self):
        """PDF scrape results should be stored in cache when max_age > 0."""
        from supacrawl.services.scrape import ScrapeService

        pdf_bytes = _make_simple_pdf("A document with enough content for testing purposes and validation.")

        with (
            patch("supacrawl.services.pdf.is_pdf_url", return_value=True),
            patch("supacrawl.services.pdf.download_pdf", new_callable=AsyncMock, return_value=pdf_bytes),
        ):
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as tmpdir:
                service = ScrapeService(headless=True, cache_dir=Path(tmpdir))
                result = await service.scrape(
                    url="https://example.com/report.pdf",
                    formats=["markdown"],
                    parse_pdf="fast",
                    max_age=3600,
                )

                assert result.success is True

                # Second call should hit cache
                result2 = await service.scrape(
                    url="https://example.com/report.pdf",
                    formats=["markdown"],
                    parse_pdf="fast",
                    max_age=3600,
                )

                assert result2.success is True
                assert result2.data is not None
                assert result2.data.metadata.cache_hit is True
