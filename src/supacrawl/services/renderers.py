"""Reap a Chromium renderer that outlives its page and keeps burning a core (#170).

A page is closed in a ``finally``, so the page lifecycle is bounded; the leak is
one level down. A renderer process that survives its closed page and spins is
not reported by Playwright, and nothing else in the process would notice it.
Measured 07/09/2026: one such renderer held a full core for 1h42m with no
scrape in flight.

The reaper runs once a page closes, after a budget, and only while no page is
open anywhere in this process (a renderer that is busy because a page is
loading in it is doing its job). It samples the CPU time of every renderer
descended from this process over a short window and kills those that are
still spinning. Only descendants are ever touched, so another process's
browser on a shared host is never a candidate. Linux only: it reads ``/proc``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from supacrawl.utils import log_with_correlation

LOGGER = logging.getLogger(__name__)

# Seconds a closed page's renderer may keep spinning before it is reaped;
# SUPACRAWL_RENDERER_BUDGET overrides it and 0 disables the reaper.
_DEFAULT_BUDGET_S = 15.0
# The CPU fraction over the sampling window that counts as spinning: a settled
# renderer idles near zero, the leaked one sat at 99%.
_BUSY_FRACTION = 0.5
_WINDOW_S = 2.0
_RENDERER_FLAG = b"--type=renderer"

_open_pages = 0
_pending: asyncio.Task[list[int]] | None = None


def page_opened() -> None:
    """Record a browser page opened anywhere in this process."""
    global _open_pages
    _open_pages += 1


def page_closed() -> None:
    """Record a browser page closed anywhere in this process."""
    global _open_pages
    _open_pages = max(0, _open_pages - 1)


def open_pages() -> int:
    return _open_pages


def renderer_budget_s() -> float:
    raw = os.getenv("SUPACRAWL_RENDERER_BUDGET")
    if raw is None:
        return _DEFAULT_BUDGET_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        LOGGER.debug("Ignoring non-numeric SUPACRAWL_RENDERER_BUDGET=%r", raw)
        return _DEFAULT_BUDGET_S


def _stat_fields(pid: int) -> list[str] | None:
    """The fields of ``/proc/<pid>/stat`` after the command name, or None once the process is gone."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return text[text.rfind(")") + 2 :].split()


def _descendants(root: int) -> list[int]:
    """Every live process descended from ``root``."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fields = _stat_fields(int(entry))
        if fields and len(fields) > 1:
            children.setdefault(int(fields[1]), []).append(int(entry))
    found: list[int] = []
    queue = [root]
    while queue:
        pid = queue.pop()
        for child in children.get(pid, ()):
            found.append(child)
            queue.append(child)
    return found


def _is_renderer(pid: int) -> bool:
    """Whether the process is a Chromium renderer. The command line is read, never logged."""
    try:
        return _RENDERER_FLAG in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def _cpu_seconds(pid: int) -> float | None:
    fields = _stat_fields(pid)
    if fields is None or len(fields) < 13:
        return None
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def renderers() -> list[int]:
    """The Chromium renderer processes descended from this process."""
    if not sys.platform.startswith("linux"):
        return []
    return [pid for pid in _descendants(os.getpid()) if _is_renderer(pid)]


def process_tree_rss_bytes() -> int | None:
    """Resident memory of this process and every descendant (a browser is several processes); None off Linux."""
    if not sys.platform.startswith("linux"):
        return None
    page_size = os.sysconf("SC_PAGE_SIZE")
    total = 0
    for pid in [os.getpid(), *_descendants(os.getpid())]:
        try:
            total += int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * page_size
        except OSError, IndexError, ValueError:
            continue
    return total


async def reap_spinning_renderers(*, url: str, budget_s: float, window_s: float = _WINDOW_S) -> list[int]:
    """After ``budget_s``, kill every renderer of this process still spinning while no page is open.

    Returns the pids killed. Nothing is touched while a page is open anywhere in
    the process, since its renderer is legitimately busy.
    """
    await asyncio.sleep(budget_s)
    if open_pages() > 0:
        return []
    before = {pid: _cpu_seconds(pid) for pid in renderers()}
    if not before:
        return []
    await asyncio.sleep(window_s)
    if open_pages() > 0:
        return []
    killed: list[int] = []
    for pid, cpu_before in before.items():
        cpu_after = _cpu_seconds(pid)
        if cpu_before is None or cpu_after is None:
            continue
        fraction = (cpu_after - cpu_before) / window_s
        if fraction < _BUSY_FRACTION:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
        killed.append(pid)
        log_with_correlation(
            LOGGER,
            logging.WARNING,
            "Reaped a renderer still spinning after its page closed",
            url=url,
            pid=pid,
            budget_s=budget_s,
            cpu_fraction=round(fraction, 2),
        )
    return killed


def _swallow(task: asyncio.Task[list[int]]) -> None:
    if not task.cancelled() and task.exception() is not None:
        LOGGER.debug("Renderer reap failed: %s", task.exception())


def schedule_reap(url: str) -> None:
    """Arm the reaper for the page just closed; a later close re-arms it, so one check runs per quiet spell."""
    global _pending
    budget = renderer_budget_s()
    if budget <= 0 or not sys.platform.startswith("linux"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _pending is not None and not _pending.done():
        _pending.cancel()
    _pending = loop.create_task(reap_spinning_renderers(url=url, budget_s=budget))
    _pending.add_done_callback(_swallow)
