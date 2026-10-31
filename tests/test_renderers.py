"""A renderer that outlives its page and spins is reaped; an idle one, or one with a page open, is not (#170)."""

from __future__ import annotations

import logging
import subprocess
import sys

import pytest

from supacrawl.services import renderers

pytestmark = [pytest.mark.unit, pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")]

# A child of this process whose command line carries the renderer flag, as Chromium's do.
SPIN = [sys.executable, "-c", "while True: pass", "--type=renderer"]
IDLE = [sys.executable, "-c", "import time; time.sleep(60)", "--type=renderer"]


@pytest.fixture
def children():
    procs: list[subprocess.Popen[bytes]] = []
    yield procs
    for proc in procs:
        proc.kill()
        proc.wait(timeout=5)


async def test_a_spinning_renderer_is_reaped_and_an_idle_one_is_left(children, caplog):
    spin, idle = subprocess.Popen(SPIN), subprocess.Popen(IDLE)
    children += [spin, idle]
    assert {spin.pid, idle.pid} <= set(renderers.renderers())

    with caplog.at_level(logging.WARNING, logger="supacrawl.services.renderers"):
        killed = await renderers.reap_spinning_renderers(url="https://example.com/", budget_s=0.2, window_s=0.5)

    assert killed == [spin.pid]
    assert spin.wait(timeout=5) == -9
    assert idle.poll() is None
    record = next(r for r in caplog.records if "Reaped" in r.getMessage())
    assert (record.url, record.budget_s, record.pid) == ("https://example.com/", 0.2, spin.pid)


async def test_nothing_is_reaped_while_a_page_is_open(children):
    spin = subprocess.Popen(SPIN)
    children.append(spin)
    renderers.page_opened()
    try:
        assert await renderers.reap_spinning_renderers(url="u", budget_s=0.1, window_s=0.3) == []
    finally:
        renderers.page_closed()
    assert spin.poll() is None


def test_the_budget_is_read_from_the_environment(monkeypatch):
    monkeypatch.delenv("SUPACRAWL_RENDERER_BUDGET", raising=False)
    assert renderers.renderer_budget_s() == 15.0
    monkeypatch.setenv("SUPACRAWL_RENDERER_BUDGET", "0")
    assert renderers.renderer_budget_s() == 0.0
    monkeypatch.setenv("SUPACRAWL_RENDERER_BUDGET", "nope")
    assert renderers.renderer_budget_s() == 15.0
