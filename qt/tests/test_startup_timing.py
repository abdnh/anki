# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import sys
import time

import pytest

from aqt.startup_timing import format_report, process_start


def test_format_report_shows_ms_since_process_start() -> None:
    report = format_report([("a", 10.5), ("b", 11.25)], start=10.0)

    assert report.splitlines() == [
        "Startup timing (ms since process start):",
        "     500  a",
        "    1250  b",
    ]


def test_format_report_uses_first_mark_when_process_start_unknown() -> None:
    report = format_report([("a", 3.0), ("b", 3.1)], start=None)

    assert report.splitlines()[0] == (
        "Startup timing (ms since the start of 'import aqt'):"
    )
    assert report.splitlines()[1] == "       0  a"


@pytest.mark.skipif(
    sys.platform != "win32" and not sys.platform.startswith("linux"),
    reason="process start is only known on Windows and Linux",
)
def test_process_start_is_shortly_before_now() -> None:
    start = process_start()

    assert start is not None
    assert 0 < time.perf_counter() - start < 3600
