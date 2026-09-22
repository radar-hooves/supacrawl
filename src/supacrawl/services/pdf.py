"""PDF parsing service for text extraction and OCR fallback.

Provides three parsing modes:
- fast: Pure text extraction only (pdfplumber)
- auto: Text extraction first, OCR fallback if result is empty/short
- ocr: Force full OCR processing (requires supacrawl[pdf-ocr] extra)

PDF URLs are auto-detected by file extension (.pdf) or Content-Type header.
When detected, the PDF is downloaded directly via httpx (bypassing the browser)
and processed through the appropriate extraction pipeline.
"""

import io
import logging
import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

import httpx

from supacrawl.services._pdf_sniff import MAX_PDF_SIZE, is_pdf_bytes
from supacrawl.services.url_guard import guarded_request

LOGGER = logging.getLogger(__name__)

# Minimum word count to consider text extraction successful (for auto mode fallback)
MIN_WORDS_FOR_SUCCESS = 20

# Horizontal gap (in points) below which pdfplumber treats adjacent glyphs as one
# word. pdfplumber defaults to 3, which is too wide for LaTeX/academic PDFs whose
# inter-word spaces are narrower than 3pt — at the default they extract as
# "Thedominantsequencetransduction...". A tolerance of 2 restores inter-word
# spacing on those PDFs without over-splitting digitally-generated ones (e.g.
# government PDFs), which is what the RAG use case needs.
PDF_TEXT_X_TOLERANCE = 2

# Type alias for parse modes
type ParsePdfMode = Literal["fast", "auto", "ocr"]


@dataclass
class PdfMetadata:
    """Metadata extracted from PDF document properties."""

    title: str | None = None
    author: str | None = None
    page_count: int = 0
    creation_date: str | None = None


@dataclass
class PdfExtractionResult:
    """Result of PDF text extraction."""

    markdown: str
    metadata: PdfMetadata = field(default_factory=PdfMetadata)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


# Common web page extensions that are definitely NOT PDFs.
# When a URL has one of these extensions, skip the HEAD request.
_NON_PDF_EXTENSIONS = frozenset(
    {
        ".html",
        ".htm",
        ".php",
        ".asp",
        ".aspx",
        ".jsp",
        ".cgi",
        ".shtml",
        ".xhtml",
        ".cfm",
        ".pl",
        ".py",
        ".rb",
        ".js",
        ".css",
        ".xml",
        ".json",
        ".rss",
        ".atom",
        ".svg",
        ".txt",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".bmp",
        ".mp3",
        ".mp4",
        ".webm",
        ".ogg",
        ".wav",
        ".avi",
        ".mov",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".rar",
        ".7z",
    }
)


def is_pdf_url(url: str) -> bool:
    """Check if URL path ends with .pdf extension.

    Only inspects the URL path — does not make any network requests.
    Query parameters and fragments are ignored.
    """
    path = urlparse(url).path.lower()
    return path.endswith(".pdf")


def _has_non_pdf_extension(url: str) -> bool:
    """Check if URL has a known non-PDF file extension.

    Used to skip the HEAD request for URLs that are clearly not PDFs.
    """
    path = urlparse(url).path.lower()
    # Check if path ends with a known non-PDF extension
    for ext in _NON_PDF_EXTENSIONS:
        if path.endswith(ext):
            return True
    return False


def needs_content_type_check(url: str) -> bool:
    """Determine if a URL needs a HEAD request to check for PDF Content-Type.

    Returns True only for ambiguous URLs (no extension or unknown extension).
    URLs with known non-PDF extensions skip the check entirely.
    """
    if is_pdf_url(url):
        return False  # Already detected as PDF
    if _has_non_pdf_extension(url):
        return False  # Known non-PDF extension
    return True  # Ambiguous — needs HEAD request


async def detect_pdf_content_type(url: str, timeout: float = 10.0) -> bool:
    """Send a HEAD request and check for ``application/pdf`` Content-Type.

    Falls back to a GET when HEAD is not itself a success response: a
    dynamically-generated download endpoint commonly answers HEAD with 405
    Method Not Allowed (e.g. legislation.gov.au's compilation PDFs, served as
    a Content-Disposition attachment) or another non-2xx status, and that
    response's body is an error page, not the resource — its Content-Type is
    not evidence either way. The fallback checks the GET's own Content-Type
    and, failing that, sniffs the body for the ``%PDF-`` signature the same
    way the HTTP-first path already does for an ambiguous content type.

    Returns False on any network error rather than raising.
    """
    try:
        # follow_redirects=False: redirects are followed by hand inside
        # guarded_request so every hop is validated and pinned (#152).
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; supacrawl)"},
        ) as client:
            response = await guarded_request(client, "HEAD", url)
            if response.status_code < 300:
                content_type = response.headers.get("content-type", "")
                return "application/pdf" in content_type.lower()

            fallback = await guarded_request(client, "GET", url)
            content_type = fallback.headers.get("content-type", "")
            if "application/pdf" in content_type.lower():
                return True
            return is_pdf_bytes(fallback.content)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


async def download_pdf(url: str, timeout: float = 120.0, max_size: int = MAX_PDF_SIZE) -> bytes:
    """Download PDF content from *url* via httpx.

    Uses a generous timeout since PDFs can be large. Enforces a size limit
    to prevent memory exhaustion.

    Raises:
        httpx.HTTPStatusError: If the server returns a non-2xx status.
        httpx.TimeoutException: If the request times out.
        ValidationError: If the URL or a redirect hop fails the SSRF guard (#152).
        ValueError: If the PDF exceeds *max_size* bytes.
    """
    # follow_redirects=False: redirects are followed by hand inside
    # guarded_request so every hop is validated and pinned (#152).
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": "Mozilla/5.0 (compatible; supacrawl)"},
    ) as client:
        response = await guarded_request(client, "GET", url)
        response.raise_for_status()

        if len(response.content) > max_size:
            size_mb = len(response.content) / (1024 * 1024)
            limit_mb = max_size / (1024 * 1024)
            raise ValueError(f"PDF is too large ({size_mb:.1f} MB, limit is {limit_mb:.0f} MB). URL: {url}")

        return response.content


# ---------------------------------------------------------------------------
# Availability checks
# ---------------------------------------------------------------------------


def _is_pdfplumber_available() -> bool:
    try:
        import pdfplumber  # noqa: F401

        return True
    except ImportError:
        return False


def _is_ocr_available() -> bool:
    try:
        import pdf2image  # noqa: F401
        import pytesseract  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Text extraction (pdfplumber)
# ---------------------------------------------------------------------------


def _extract_pdf_metadata(pdf: "pdfplumber.PDF") -> PdfMetadata:  # type: ignore[name-defined]  # noqa: F821
    """Extract metadata from an open pdfplumber PDF object."""
    meta = pdf.metadata or {}

    title = meta.get("Title") or meta.get("title")
    author = meta.get("Author") or meta.get("author")
    creation_date = meta.get("CreationDate") or meta.get("creation_date")

    # Clean up pdfplumber date format (D:20240101120000Z -> 2024-01-01T12:00:00Z)
    if creation_date and isinstance(creation_date, str):
        creation_date = _normalise_pdf_date(creation_date)

    return PdfMetadata(
        title=title if isinstance(title, str) else None,
        author=author if isinstance(author, str) else None,
        page_count=len(pdf.pages),
        creation_date=creation_date,
    )


def _normalise_pdf_date(raw: str) -> str | None:
    """Normalise a PDF date string like ``D:20240101120000+00'00'`` to ISO 8601."""
    # Strip leading D: prefix
    cleaned = re.sub(r"^D:", "", raw.strip())
    # Try to parse the common format: YYYYMMDDHHmmSS
    match = re.match(r"(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?", cleaned)
    if not match:
        return raw  # Return as-is if we can't parse
    year, month, day = match.group(1), match.group(2), match.group(3)
    hour = match.group(4) or "00"
    minute = match.group(5) or "00"
    second = match.group(6) or "00"
    return f"{year}-{month}-{day}T{hour}:{minute}:{second}Z"


def _table_to_markdown(table: list[list[str | None]]) -> str:
    """Convert a pdfplumber table (2D list) to a markdown table.

    Handles None cells and attempts to create a clean markdown table
    with proper alignment separators.
    """
    if not table or not table[0]:
        return ""

    # Sanitise cells: replace None with empty string, strip whitespace
    rows = [[_clean_cell(cell) for cell in row] for row in table]

    # Use first row as header
    header = rows[0]
    col_count = len(header)

    # Ensure all rows have the same column count
    rows = [row + [""] * (col_count - len(row)) if len(row) < col_count else row[:col_count] for row in rows]

    lines = []
    # Header row
    lines.append("| " + " | ".join(rows[0]) + " |")
    # Separator
    lines.append("| " + " | ".join("---" for _ in range(col_count)) + " |")
    # Data rows
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)


def _clean_cell(cell: str | None) -> str:
    """Clean a table cell value for markdown output."""
    if cell is None:
        return ""
    # Replace newlines within cells with spaces
    text = str(cell).replace("\n", " ").strip()
    # Escape pipe characters
    text = text.replace("|", "\\|")
    return text


def _detect_headings(page: "pdfplumber.Page") -> dict[int, int]:  # type: ignore[name-defined]  # noqa: F821
    """Detect heading lines based on font size heuristics.

    Examines character-level data to find lines with notably larger font sizes.
    Returns a mapping of line-start y-position → heading level (1-3).
    """
    chars = page.chars
    if not chars:
        return {}

    # Collect font sizes
    sizes: list[float] = []
    for char in chars:
        size = char.get("size", 0)
        if size > 0:
            sizes.append(size)

    if not sizes:
        return {}

    # Find the most common font size (body text)
    from collections import Counter

    size_counts = Counter(round(s, 1) for s in sizes)
    body_size = size_counts.most_common(1)[0][0]

    # Group chars by approximate y-position (top) to identify lines
    line_sizes: dict[int, list[float]] = {}
    for char in chars:
        y_key = round(char.get("top", 0))
        size = char.get("size", 0)
        if size > 0:
            line_sizes.setdefault(y_key, []).append(size)

    headings: dict[int, int] = {}
    for y_pos, char_sizes in line_sizes.items():
        avg_size = sum(char_sizes) / len(char_sizes)
        ratio = avg_size / body_size if body_size > 0 else 1.0

        if ratio >= 1.8:
            headings[y_pos] = 1  # h1
        elif ratio >= 1.4:
            headings[y_pos] = 2  # h2
        elif ratio >= 1.15:
            headings[y_pos] = 3  # h3

    return headings


def _apply_headings_to_text(text: str, page: "pdfplumber.Page") -> str:  # type: ignore[name-defined]  # noqa: F821
    """Apply heading markers to extracted text based on font size analysis.

    This is best-effort: if heading detection fails, the original text is returned.
    """
    headings = _detect_headings(page)
    if not headings:
        return text

    # Build a set of heading text snippets from char data
    # Group chars into lines by y-position
    lines_by_y: dict[int, str] = {}
    for char in page.chars:
        y_key = round(char.get("top", 0))
        lines_by_y.setdefault(y_key, "")
        char_text = char.get("text", "")
        if char_text:
            lines_by_y[y_key] += char_text

    # Map heading y-positions to their text content
    heading_texts: dict[str, int] = {}
    for y_pos, level in headings.items():
        line_text = lines_by_y.get(y_pos, "").strip()
        if line_text and len(line_text) > 1:
            heading_texts[line_text] = level

    if not heading_texts:
        return text

    # Apply heading markers to the extracted text
    lines = text.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped in heading_texts:
            level = heading_texts[stripped]
            prefix = "#" * level
            result.append(f"{prefix} {stripped}")
        else:
            result.append(line)

    return "\n".join(result)


def extract_text(pdf_bytes: bytes) -> PdfExtractionResult:
    """Extract text and tables from a PDF using pdfplumber.

    Returns markdown with:
    - Text content from each page
    - Tables converted to markdown tables
    - Headings detected by font size heuristics
    - PDF metadata (title, author, page count, creation date)

    Raises:
        ImportError: If pdfplumber is not installed.
    """
    import pdfplumber

    pages_markdown: list[str] = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        metadata = _extract_pdf_metadata(pdf)

        for page in pdf.pages:
            page_parts: list[str] = []

            # Extract tables from this page
            tables = page.extract_tables()
            table_bboxes: list[tuple[float, float, float, float]] = []

            if tables:
                # Get table bounding boxes to exclude table regions from text
                for table_obj in page.find_tables():
                    table_bboxes.append(table_obj.bbox)

            # Extract text, optionally cropping out table regions
            if table_bboxes:
                # Simple approach: extract full text, then append tables separately
                text = page.extract_text(x_tolerance=PDF_TEXT_X_TOLERANCE) or ""
                text = _apply_headings_to_text(text, page)
                page_parts.append(text)

                # Append tables as markdown
                for table_data in tables:
                    md_table = _table_to_markdown(table_data)
                    if md_table:
                        page_parts.append(md_table)
            else:
                text = page.extract_text(x_tolerance=PDF_TEXT_X_TOLERANCE) or ""
                text = _apply_headings_to_text(text, page)
                page_parts.append(text)

            combined = "\n\n".join(part for part in page_parts if part.strip())
            if combined.strip():
                pages_markdown.append(combined)

    # Join pages with horizontal rule separators for multi-page documents
    if len(pages_markdown) > 1:
        markdown = "\n\n---\n\n".join(pages_markdown)
    else:
        markdown = pages_markdown[0] if pages_markdown else ""

    # Add document title as h1 if available and not already in content
    if metadata.title and not markdown.startswith(f"# {metadata.title}"):
        markdown = f"# {metadata.title}\n\n{markdown}"

    return PdfExtractionResult(markdown=markdown, metadata=metadata)


# ---------------------------------------------------------------------------
# OCR extraction (pytesseract + pdf2image)
# ---------------------------------------------------------------------------


def extract_ocr(pdf_bytes: bytes) -> PdfExtractionResult:
    """Extract text from a PDF using OCR (pytesseract + pdf2image).

    Converts each page to an image and runs Tesseract OCR on it.
    Best for scanned documents and image-only PDFs.

    Raises:
        ImportError: If pytesseract or pdf2image are not installed.
        RuntimeError: If Tesseract is not installed on the system.
    """
    import pdf2image
    import pytesseract

    pages_text: list[str] = []

    # Convert PDF to images
    images = pdf2image.convert_from_bytes(pdf_bytes)
    page_count = len(images)

    for image in images:
        text = pytesseract.image_to_string(image)
        if text.strip():
            pages_text.append(text.strip())

    if len(pages_text) > 1:
        markdown = "\n\n---\n\n".join(pages_text)
    else:
        markdown = pages_text[0] if pages_text else ""

    # Try to get metadata from text extraction (OCR doesn't provide it)
    metadata = PdfMetadata(page_count=page_count)
    if _is_pdfplumber_available():
        try:
            import pdfplumber

            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                metadata = _extract_pdf_metadata(pdf)
        except Exception:
            pass  # Fall back to basic metadata

    return PdfExtractionResult(markdown=markdown, metadata=metadata)


# ---------------------------------------------------------------------------
# Main parse orchestrator
# ---------------------------------------------------------------------------


async def parse_pdf(
    url: str,
    mode: ParsePdfMode = "auto",
    pdf_bytes: bytes | None = None,
) -> PdfExtractionResult:
    """Parse a PDF document and extract text as markdown.

    Args:
        url: URL of the PDF (used for downloading if pdf_bytes not provided).
        mode: Parsing mode — "fast" (text only), "auto" (text with OCR fallback),
              or "ocr" (force OCR).
        pdf_bytes: Pre-downloaded PDF content. If None, downloads from url.

    Returns:
        PdfExtractionResult with markdown content and metadata.

    Raises:
        ImportError: If required libraries are not installed.
        ValueError: If the downloaded content is not a valid PDF.
    """
    # Download if not provided
    if pdf_bytes is None:
        LOGGER.info(f"Downloading PDF from {url}")
        pdf_bytes = await download_pdf(url)

    # Validate PDF content
    if not is_pdf_bytes(pdf_bytes):
        raise ValueError(f"Content from {url} is not a valid PDF (expected %PDF- header)")

    if mode == "ocr":
        if not _is_ocr_available():
            raise ImportError(
                "OCR mode requires pytesseract and pdf2image. Install with: pip install supacrawl[pdf-ocr]"
            )
        LOGGER.info(f"Extracting text from PDF via OCR: {url}")
        return extract_ocr(pdf_bytes)

    # fast or auto: try text extraction first
    if not _is_pdfplumber_available():
        raise ImportError("PDF text extraction requires pdfplumber. Install with: pip install pdfplumber")

    LOGGER.info(f"Extracting text from PDF: {url}")
    result = extract_text(pdf_bytes)

    # auto mode: check if text extraction was sufficient
    if mode == "auto":
        word_count = len(result.markdown.split())
        if word_count < MIN_WORDS_FOR_SUCCESS:
            if _is_ocr_available():
                LOGGER.info(f"Text extraction yielded only {word_count} words, falling back to OCR: {url}")
                return extract_ocr(pdf_bytes)
            else:
                LOGGER.warning(
                    f"Text extraction yielded only {word_count} words for {url}. "
                    "OCR fallback not available. Install with: pip install supacrawl[pdf-ocr]"
                )

    return result
