# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import contextlib
import pickle
import sqlite3
from pathlib import Path

from tools.startup_bench import collect, parse_importtime, seed_prefs


def test_parse_importtime_reads_rows_and_skips_header_and_other_output(
    tmp_path: Path,
) -> None:
    log = tmp_path / "run.log"
    log.write_text(
        "Starting Anki...\n"
        "import time: self [us] | cumulative | imported package\n"
        "import time:       120 |        120 |   _io\n"
        "import time:      2500 |      40100 | aqt\n"
        "import time: garbage\n",
        encoding="utf8",
    )

    rows = parse_importtime(log)

    assert rows == [
        {"name": "   _io", "self_ms": 0.12, "cumulative_ms": 0.12},
        {"name": " aqt", "self_ms": 2.5, "cumulative_ms": 40.1},
    ]


def test_seed_prefs_creates_global_profile_that_skips_language_picker(
    tmp_path: Path,
) -> None:
    seed_prefs(tmp_path, "ja")

    with contextlib.closing(sqlite3.connect(tmp_path / "prefs21.db")) as db:
        rows = db.execute("select name, data from profiles").fetchall()
    assert [name for name, _ in rows] == ["_global"]
    meta = pickle.loads(rows[0][1])
    assert meta["defaultLang"] == "ja"
    assert meta["updates"] is False


def test_seed_prefs_keeps_existing_prefs(tmp_path: Path) -> None:
    prefs = tmp_path / "prefs21.db"
    prefs.write_bytes(b"existing")

    seed_prefs(tmp_path, "en")

    assert prefs.read_bytes() == b"existing"


def test_collect_sums_repeated_spans_and_ignores_failed_runs() -> None:
    results = {
        "runs": [
            {
                "ok": True,
                "marks": {"deck_browser_painted": 2000.0},
                "spans": [
                    {"name": "http x", "ms": 1.0},
                    {"name": "http x", "ms": 2.0},
                ],
                "exit_ms": 300.0,
                "resources": {"rss_mb": 500.0, "cpu_s": 2.5, "processes": 5},
            },
            {"ok": False, "marks": {"deck_browser_painted": 9999.0}, "spans": []},
        ]
    }

    data = collect(results)

    assert data["marks"] == {"deck_browser_painted": [2000.0]}
    assert data["spans"] == {"http x": [3.0]}
    assert data["other"] == {
        "exit (ms)": [300.0],
        "rss_mb": [500.0],
        "cpu_s": [2.5],
        "processes": [5],
    }
