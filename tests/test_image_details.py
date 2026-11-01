"""Image sizing: candidates from the HTML, dimensions from the files themselves."""

import io

import httpx
import pytest
from PIL import Image

from supacrawl.services import image_details
from supacrawl.services.image_details import collect_candidates, measure_images


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height)).save(buffer, "PNG")
    return buffer.getvalue()


HTML = """
<html><head><meta property="og:image" content="/big.png"></head><body>
<img src="/thumb/1920px-photo.png" width="800">
<img srcset="/small.png 300w, /mid.png 900w">
<img src="/static/images/footer/poweredby.png">
<img src="/logo.svg">
<a href="/orig/photo.png">Original file</a>
<a href="/about">About</a>
</body></html>
"""


def test_collect_candidates_finds_links_hints_and_skips_svg():
    found = collect_candidates(HTML, "https://example.com/page")
    assert found["https://example.com/orig/photo.png"] is None
    assert found["https://example.com/big.png"] is None
    assert found["https://example.com/mid.png"] == 900
    assert found["https://example.com/thumb/1920px-photo.png"] == 1920
    assert not any(url.endswith((".svg", "/about")) for url in found)


@pytest.mark.asyncio
async def test_measure_images_sizes_from_file_sorts_and_flags_icons(monkeypatch):
    sizes = {
        "/orig/photo.png": (6000, 4000),
        "/big.png": (1200, 800),
        "/thumb/1920px-photo.png": (1920, 1280),
        "/small.png": (64, 64),
        "/mid.png": (900, 600),
        "/static/images/footer/poweredby.png": (88, 31),
    }

    class FakeStream:
        def __init__(self, url):
            self.status_code = 206
            self.headers = {"content-range": "bytes 0-65535/4000000"}
            self._body = _png(*sizes[httpx.URL(url).path])

        async def aiter_bytes(self):
            yield self._body

    class Ctx:
        def __init__(self, client, method, url):
            self.stream = FakeStream(url)

        async def __aenter__(self):
            return self.stream

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(image_details, "guarded_stream", Ctx)

    details = await measure_images(HTML, "https://example.com/page")

    assert details[0].url == "https://example.com/orig/photo.png"
    assert (details[0].width, details[0].height, details[0].bytes) == (6000, 4000, 4000000)
    assert [d.icon for d in details] == [False] * 4 + [True] * 2
    assert details[-1].width in (64, 88)


@pytest.mark.asyncio
async def test_unreadable_image_keeps_its_hint(monkeypatch):
    class Boom:
        def __init__(self, *a):
            pass

        async def __aenter__(self):
            raise OSError("down")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(image_details, "guarded_stream", Boom)
    details = await measure_images('<img srcset="/a.png 900w">', "https://example.com/")
    assert (details[0].width, details[0].height) == (900, None)
