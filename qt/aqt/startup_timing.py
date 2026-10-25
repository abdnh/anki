# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Opt-in startup timing. Run Anki with ANKI_STARTUP_TIMING=1, and it prints
when each startup phase finished, in ms since the process started."""

from __future__ import annotations

import os
import sys
import time
from typing import Any

ENABLED = bool(os.environ.get("ANKI_STARTUP_TIMING"))
PAINTED_CMD = "startup-timing:painted"

_marks: list[tuple[str, float]] = []


def mark(name: str) -> None:
    if ENABLED:
        _marks.append((name, time.perf_counter()))


mark("aqt_import_started")


def install_hooks() -> None:
    if not ENABLED:
        return
    from aqt import gui_hooks

    gui_hooks.collection_did_load.append(_on_collection_did_load)
    gui_hooks.profile_did_open.append(_on_profile_did_open)
    gui_hooks.deck_browser_did_render.append(_on_deck_browser_did_render)
    gui_hooks.webview_did_receive_js_message.append(_on_js_message)


def _on_collection_did_load(col: Any) -> None:
    mark("collection_loaded")


def _on_profile_did_open() -> None:
    mark("profile_opened")


def _on_deck_browser_did_render(deck_browser: Any) -> None:
    from aqt import gui_hooks

    gui_hooks.deck_browser_did_render.remove(_on_deck_browser_did_render)
    mark("deck_list_rendered")
    # the second callback runs once the frame with the deck list was drawn
    deck_browser.web.eval(
        "requestAnimationFrame(() => requestAnimationFrame(() =>"
        f" pycmd('{PAINTED_CMD}')));"
    )


def _on_js_message(handled: tuple[bool, Any], message: str, context: Any) -> Any:
    if message != PAINTED_CMD:
        return handled
    from aqt import gui_hooks

    gui_hooks.webview_did_receive_js_message.remove(_on_js_message)
    mark("deck_list_painted")
    print(format_report(_marks, process_start()), flush=True)
    return (True, None)


def format_report(marks: list[tuple[str, float]], start: float | None) -> str:
    if start is None:
        start = marks[0][1]
        origin = "the start of 'import aqt'"
    else:
        origin = "process start"
    lines = [f"Startup timing (ms since {origin}):"]
    lines += [f"{(t - start) * 1000:8.0f}  {name}" for name, t in marks]
    return "\n".join(lines)


def process_start() -> float | None:
    """The process creation time on the time.perf_counter() clock, if known."""
    try:
        if sys.platform == "win32":
            return _process_start_windows()
        if sys.platform.startswith("linux"):
            return _process_start_linux()
    except Exception as exc:
        print(f"startup timing: unable to get process start: {exc}")
    return None


def _process_start_windows() -> float | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
        ctypes.POINTER(wintypes.FILETIME)
    ] * 4
    created, exited, kernel, user, now = (wintypes.FILETIME() for _ in range(5))
    if not kernel32.GetProcessTimes(
        kernel32.GetCurrentProcess(),
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    kernel32.GetSystemTimePreciseAsFileTime(ctypes.byref(now))
    perf_now = time.perf_counter()

    def ticks(ft: wintypes.FILETIME) -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    # FILETIME counts 100 ns intervals
    return perf_now - (ticks(now) - ticks(created)) / 1e7


def _process_start_linux() -> float | None:
    if not sys.platform.startswith("linux"):
        return None
    with open("/proc/self/stat", encoding="ascii") as file:
        # the command name can contain spaces, so skip past its closing bracket
        fields = file.read().rsplit(")", 1)[1].split()
    start_ticks = int(fields[19])
    boot_now = time.clock_gettime(time.CLOCK_BOOTTIME)
    perf_now = time.perf_counter()
    return perf_now - (boot_now - start_ticks / os.sysconf("SC_CLK_TCK"))
