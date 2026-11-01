"""Per-call strictness, hop metadata and typed refusals on the guarded fetchers.

Consumer: radar-hooves/library#297.
"""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Iterator
from unittest.mock import patch

import httpx
import pytest

from supacrawl.exceptions import ValidationError
from supacrawl.services.url_guard import (
    BlockedAddressError,
    RedirectLimitError,
    UnresolvableHostError,
    guarded_hop,
    guarded_request,
    guarded_stream,
)

PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPACRAWL_BLOCK_PRIVATE_NETWORKS", raising=False)


@pytest.fixture
def server() -> Iterator[tuple[int, list[str]]]:
    """A loopback HTTP server: /a -> /b -> 200, /loop -> /loop, /secret records a hit."""
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits.append(self.path)
            if self.path == "/a":
                self.send_response(302)
                self.send_header("Location", "/b")
                self.send_header("Content-Length", "0")
            elif self.path == "/loop":
                self.send_response(302)
                self.send_header("Location", "/loop")
                self.send_header("Content-Length", "0")
            else:
                self.send_response(200)
                self.send_header("Content-Length", "2")
            self.end_headers()
            if self.path not in ("/a", "/loop"):
                self.wfile.write(b"ok")

        def log_message(self, *args: object) -> None:
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], hits
    srv.shutdown()


def _resolver(mapping: dict[str, str]):
    def fake(host: str, port: int, **kwargs: object):
        if host not in mapping:
            raise socket.gaierror(-2, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port))]

    return patch("supacrawl.services.url_guard.socket.getaddrinfo", side_effect=fake)


async def test_env_default_stays_lenient_and_strict_is_per_call(server) -> None:
    port, hits = server
    async with httpx.AsyncClient() as client:
        resp = await guarded_request(client, "GET", f"http://127.0.0.1:{port}/b")
        assert resp.status_code == 200
        with pytest.raises(BlockedAddressError):
            await guarded_request(client, "GET", f"http://127.0.0.1:{port}/b", strict=True)
        with pytest.raises(BlockedAddressError):
            await guarded_hop(client, "GET", f"http://127.0.0.1:{port}/b", strict=True)
        with pytest.raises(BlockedAddressError):
            async with guarded_stream(client, "GET", f"http://127.0.0.1:{port}/b", strict=True):
                pass
    assert hits == ["/b"]


async def test_strict_refuses_redirect_to_private_with_env_unset(server) -> None:
    """A public first hop that redirects to the loopback listener never reaches it."""
    port, hits = server

    def public(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": f"http://127.0.0.1:{port}/secret"})

    with _resolver({"public.test": PUBLIC_IP}):
        async with httpx.AsyncClient(transport=httpx.MockTransport(public)) as client:
            with pytest.raises(BlockedAddressError, match="127.0.0.1"):
                await guarded_request(client, "GET", "http://public.test/x", strict=True)
    assert hits == []


async def test_strict_false_overrides_env(server, monkeypatch: pytest.MonkeyPatch) -> None:
    port, _ = server
    monkeypatch.setenv("SUPACRAWL_BLOCK_PRIVATE_NETWORKS", "1")
    async with httpx.AsyncClient() as client:
        with pytest.raises(BlockedAddressError):
            await guarded_request(client, "GET", f"http://127.0.0.1:{port}/b")
        assert (await guarded_request(client, "GET", f"http://127.0.0.1:{port}/b", strict=False)).status_code == 200


async def test_hops_history_and_final_url(server) -> None:
    port, _ = server
    async with httpx.AsyncClient() as client:
        async with guarded_stream(client, "GET", f"http://127.0.0.1:{port}/a") as resp:
            assert resp.extensions["supacrawl_hops"] == [
                f"http://127.0.0.1:{port}/a",
                f"http://127.0.0.1:{port}/b",
            ]
            assert resp.extensions["supacrawl_final_url"] == f"http://127.0.0.1:{port}/b"
            assert [r.status_code for r in resp.history] == [302]
            assert resp.history[0].headers["location"] == "/b"


async def test_redirect_loop_and_depth_are_typed(server) -> None:
    port, _ = server
    async with httpx.AsyncClient() as client:
        with pytest.raises(RedirectLimitError, match="loop"):
            await guarded_request(client, "GET", f"http://127.0.0.1:{port}/loop")

    n = 0

    def chain(request: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        return httpx.Response(302, headers={"Location": f"http://public.test/{n}"})

    with _resolver({"public.test": PUBLIC_IP}):
        async with httpx.AsyncClient(transport=httpx.MockTransport(chain)) as client:
            with pytest.raises(RedirectLimitError, match="Too many"):
                await guarded_request(client, "GET", "http://public.test/0", max_redirects=3)


async def test_unresolvable_host_is_typed_and_all_are_validation_errors() -> None:
    with _resolver({}):
        async with httpx.AsyncClient() as client:
            with pytest.raises(UnresolvableHostError) as exc:
                await guarded_request(client, "GET", "http://nowhere.test/")
    assert isinstance(exc.value, ValidationError)
    assert issubclass(BlockedAddressError, ValidationError)
    assert issubclass(RedirectLimitError, ValidationError)
