"""Untrusted-caller posture: strict mode pins browser traffic and blocks page scripts."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from supacrawl.services import browser as browser_mod
from supacrawl.services.browser import BrowserManager, _fulfil_pinned
from supacrawl.services.url_guard import block_page_scripts, guarded_hop


def _addr(ip: str):
    return lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]


def _request(url: str) -> MagicMock:
    req = MagicMock()
    req.url = url
    req.method = "GET"
    req.headers = {"Host": "x", "Accept": "*/*"}
    req.post_data_buffer = None
    return req


def test_scripts_flag(monkeypatch):
    assert not block_page_scripts()
    monkeypatch.setenv("SUPACRAWL_BLOCK_PAGE_SCRIPTS", "1")
    assert block_page_scripts()
    assert BrowserManager()._build_context_options(device=None, extra_headers=None)["java_script_enabled"] is False


@pytest.mark.asyncio
async def test_hop_connects_to_pinned_address_and_keeps_redirect(monkeypatch):
    monkeypatch.setenv("SUPACRAWL_BLOCK_PRIVATE_NETWORKS", "1")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers["host"]))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/"})

    with patch("socket.getaddrinfo", _addr("93.184.216.34")):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resp = await guarded_hop(client, "GET", "http://example.com/a")
    assert resp.status_code == 302
    assert seen == [("93.184.216.34", "example.com")]


@pytest.mark.asyncio
@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "100.64.0.1", "169.254.169.254", "192.168.1.1"])
async def test_rebound_name_is_aborted_not_connected(monkeypatch, ip):
    """A name that now answers privately is refused and the browser never connects."""
    monkeypatch.setenv("SUPACRAWL_BLOCK_PRIVATE_NETWORKS", "1")
    route = MagicMock(abort=AsyncMock(), fulfill=AsyncMock(), continue_=AsyncMock())
    with patch("socket.getaddrinfo", _addr(ip)):
        await _fulfil_pinned(route, _request("http://rebind.example/"))
    route.abort.assert_awaited_once()
    route.fulfill.assert_not_awaited()
    route.continue_.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_route_fulfils_public_page(monkeypatch):
    monkeypatch.setenv("SUPACRAWL_BLOCK_PRIVATE_NETWORKS", "1")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"hi", headers={"content-type": "text/html"}))
    real = httpx.AsyncClient
    monkeypatch.setattr(browser_mod.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    route = MagicMock(abort=AsyncMock(), fulfill=AsyncMock())
    with patch("socket.getaddrinfo", _addr("93.184.216.34")):
        await _fulfil_pinned(route, _request("http://example.com/"))
    route.fulfill.assert_awaited_once()
    assert route.fulfill.await_args.kwargs["status"] == 200
    assert route.fulfill.await_args.kwargs["body"] == b"hi"
