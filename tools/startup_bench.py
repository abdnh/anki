# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""
Benchmark Anki's startup time.

Starts Anki repeatedly with a throwaway base folder, loads
tools/startup_probe.py into it, and reports when each startup phase
finished, relative to process creation. Startup ends when the deck list has
been painted and the event loop is idle; Anki is then closed and the time to
exit is recorded too.

Examples:

    just bench-startup                       # dev build, 10 runs
    just bench-startup --runs 20 --label baseline
    just bench-startup --exe "C:/Program Files/Anki/Anki.exe"
    just bench-startup --exe out/installer/.../Anki.exe --inject
    just bench-startup --collection ~/big.anki2 --copy-addons <base>/addons21
    just bench-startup compare out/startup-bench/a/results.json out/startup-bench/b/results.json

Phases are only comparable between runs on the same machine. Warm runs
(the default) measure CPU work; use --drop-caches (needs admin/root) for
cold-cache disk reads.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import pickle
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
PROBE = Path(__file__).resolve().parent / "startup_probe.py"
PROBE_MODULE = "_anki_startup_probe"

# Shown in the summary in this order; others follow sorted by median time.
KEY_MARKS = (
    "python_ready",
    "aqt_imported",
    "addon_loaded",
    "main_window_did_init",
    "dom_done:main webview",
    "collection_did_load",
    "window_shown",
    "window_first_paint",
    "profile_did_open",
    "deck_browser_did_render",
    "deck_browser_painted",
    "event_loop_idle",
)
PRIMARY_MARK = "deck_browser_painted"
CHROMIUM_TRACE_CATEGORIES = (
    "startup,toplevel,navigation,loading,browser,content,gpu,renderer_host,"
    "blink,v8,ipc,mojom,disabled-by-default-devtools.timeline"
)


# Launching
##########################################################################


def dev_python() -> Path:
    if sys.platform == "win32":
        return REPO / "out" / "pyenv" / "Scripts" / "python.exe"
    return REPO / "out" / "pyenv" / "bin" / "python"


def resolve_exe(exe: str) -> Path:
    path = Path(exe).expanduser().resolve()
    if path.suffix == ".app":
        path = path / "Contents" / "MacOS" / "Anki"
    if not path.exists():
        raise SystemExit(f"Executable not found: {path}")
    return path


def find_app_packages(exe: Path) -> Path:
    for root in (exe.parent, exe.parent.parent / "Resources", exe.parent.parent):
        candidate = root / "app_packages"
        if candidate.is_dir():
            return candidate
    raise SystemExit(f"Unable to find app_packages near {exe}")


class Injection:
    """Puts the probe in a packaged build's app_packages, and removes it later."""

    def __init__(self, exe: Path) -> None:
        self.dir = find_app_packages(exe)
        self.files = [self.dir / f"{PROBE_MODULE}.py", self.dir / f"{PROBE_MODULE}.pth"]

    def __enter__(self) -> Injection:
        try:
            shutil.copyfile(PROBE, self.files[0])
            self.files[1].write_text(
                f"import {PROBE_MODULE}; {PROBE_MODULE}.install_early()\n",
                encoding="utf8",
            )
        except PermissionError as exc:
            self.__exit__()
            raise SystemExit(
                f"Unable to write to {self.dir} ({exc}). Use a build in a folder you"
                " can write to, run as admin, or leave out --inject."
            ) from exc
        return self

    def __exit__(self, *_args: Any) -> None:
        for file in self.files:
            file.unlink(missing_ok=True)


def write_probe_addon(base: Path) -> None:
    addon = base / "addons21" / "zz_startup_probe"
    addon.mkdir(parents=True, exist_ok=True)
    (addon / "__init__.py").write_text(
        f"""\
import importlib.util
import sys

if {PROBE_MODULE!r} in sys.modules:
    _probe = sys.modules[{PROBE_MODULE!r}]
else:
    _spec = importlib.util.spec_from_file_location({PROBE_MODULE!r}, {str(PROBE)!r})
    _probe = importlib.util.module_from_spec(_spec)
    sys.modules[{PROBE_MODULE!r}] = _probe
    _spec.loader.exec_module(_probe)
_probe.install_from_addon()
""",
        encoding="utf8",
    )


def child_env(args: argparse.Namespace, out_file: Path, run_dir: Path, n: int) -> dict:
    env = os.environ.copy()
    for key in (
        "ANKIDEV",
        "QTWEBENGINE_REMOTE_DEBUGGING",
        "QTWEBENGINE_CHROMIUM_FLAGS",
        "PYTHONPROFILEIMPORTTIME",
        "ANKI_PROFILE_CODE",
    ):
        env.pop(key, None)
    env["ANKI_STARTUP_BENCH_OUT"] = str(out_file)
    env["ANKI_STARTUP_BENCH_SETTLE_MS"] = str(args.settle_ms)
    env["ANKI_STARTUP_BENCH_MODE"] = "exe" if args.exe else "dev"
    # A running Anki would otherwise receive our launch and we would exit.
    env["ANKI_SINGLE_INSTANCE_KEY"] = f"anki-startup-bench-{uuid.uuid4().hex}"
    if args.fast_exit:
        env["ANKI_STARTUP_BENCH_FAST_EXIT"] = "1"
    if args.ankidev:
        env["ANKIDEV"] = "1"
    if args.cprofile:
        env["ANKI_STARTUP_BENCH_CPROFILE"] = str(run_dir / f"run-{n}.prof")
    if args.chromium_trace:
        trace = run_dir / f"chromium-trace-{n}.json"
        # "*" makes startup take 20+ seconds or hang.
        env["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            f"--trace-startup={CHROMIUM_TRACE_CATEGORIES} --trace-startup-format=json"
            f" --trace-startup-file={trace} --trace-startup-duration=5"
        )
    if not args.exe:
        env["PYTHONPYCACHEPREFIX"] = str(REPO / "out" / "pycache")
        # Keeps the log of a run that is killed after a timeout.
        env["PYTHONUNBUFFERED"] = "1"
        if args.importtime:
            env["PYTHONPROFILEIMPORTTIME"] = "1"
    for item in args.env:
        key, _, value = item.partition("=")
        env[key] = value
    return env


def child_command(args: argparse.Namespace, base: Path) -> list[str]:
    anki_args = [
        "-b",
        str(base),
        *(["-l", args.lang] if args.lang else []),
        *args.anki_arg,
    ]
    if args.exe:
        return [str(resolve_exe(args.exe)), *anki_args]
    python = args.python or str(dev_python())
    return [python, str(PROBE), *anki_args]


# Resource sampling
##########################################################################


def sample_resources(pid: int) -> dict[str, Any] | None:
    try:
        import psutil
    except ImportError:
        return None
    try:
        root = psutil.Process(pid)
        procs = [root, *root.children(recursive=True)]
    except psutil.Error:
        return None
    rss = cpu = 0.0
    faults = 0
    count = 0
    for proc in procs:
        try:
            with proc.oneshot():
                mem = proc.memory_info()
                times = proc.cpu_times()
        except psutil.Error:
            continue
        count += 1
        rss += mem.rss
        cpu += times.user + times.system
        faults += getattr(mem, "num_page_faults", 0)
    return {
        "processes": count,
        "rss_mb": rss / 2**20,
        "cpu_s": cpu,
        "page_faults": faults or None,
    }


# Cache dropping
##########################################################################


def drop_caches() -> None:
    if sys.platform == "win32":
        _purge_standby_list()
    elif sys.platform == "darwin":
        subprocess.run(["sudo", "-n", "purge"], check=True)
    else:
        subprocess.run(["sync"], check=True)
        subprocess.run(
            ["sudo", "-n", "sh", "-c", "echo 3 > /proc/sys/vm/drop_caches"],
            check=True,
        )


def _purge_standby_list() -> None:
    """Empty the Windows standby list (file cache), like RAMMap does."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [
            ("PrivilegeCount", wintypes.DWORD),
            ("Privileges", LUID_AND_ATTRIBUTES * 1),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    token = wintypes.HANDLE()
    token_adjust_privileges, token_query = 0x20, 0x08
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        token_adjust_privileges | token_query,
        ctypes.byref(token),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    luid = LUID()
    if not advapi32.LookupPrivilegeValueW(
        None, "SeProfileSingleProcessPrivilege", ctypes.byref(luid)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    privileges = TOKEN_PRIVILEGES(
        1, (LUID_AND_ATTRIBUTES * 1)(LUID_AND_ATTRIBUTES(luid, 2))
    )
    advapi32.AdjustTokenPrivileges(
        token, False, ctypes.byref(privileges), 0, None, None
    )
    error = ctypes.get_last_error()
    kernel32.CloseHandle(token)
    if error:
        raise SystemExit(
            "--drop-caches needs an elevated (admin) terminal on Windows"
            f" (AdjustTokenPrivileges error {error})"
        )
    system_memory_list_information = 80
    for command in (3, 4):  # flush modified list, then purge standby list
        value = ctypes.c_int(command)
        status = ntdll.NtSetSystemInformation(
            system_memory_list_information, ctypes.byref(value), ctypes.sizeof(value)
        )
        if status:
            raise SystemExit(
                f"NtSetSystemInformation failed: 0x{status & 0xFFFFFFFF:08x}"
            )


# Running
##########################################################################


def prepare_base(args: argparse.Namespace, out_dir: Path) -> Path:
    base = out_dir / "base"
    if base.exists():
        shutil.rmtree(base)
    if args.copy_base:
        shutil.copytree(Path(args.copy_base).expanduser(), base)
    base.mkdir(parents=True, exist_ok=True)
    seed_prefs(base, args.lang or "en")
    if args.copy_addons:
        shutil.copytree(
            Path(args.copy_addons).expanduser(), base / "addons21", dirs_exist_ok=True
        )
    return base


def seed_prefs(base: Path, lang: str) -> None:
    """Create prefs21.db so that Anki does not show the first-run language picker,
    which it does even when -l is passed."""
    path = base / "prefs21.db"
    if path.exists():
        return
    meta = {
        "ver": 0,
        "updates": False,
        "check_for_updates": False,
        "check_for_addon_updates": False,
        "created": int(time.time()),
        "id": uuid.uuid4().int >> 65,
        "lastMsg": 0,
        "suppressUpdate": False,
        "firstRun": True,
        "defaultLang": lang,
    }
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "create table profiles (name text primary key collate nocase,"
            " data blob not null)"
        )
        db.execute(
            "insert into profiles values ('_global', ?)",
            (pickle.dumps(meta, protocol=4),),
        )


def install_collection(args: argparse.Namespace, base: Path) -> None:
    """Replace the profile's collection after the first run created the profile."""
    if not args.collection:
        return
    profiles = [p.parent for p in base.glob("*/collection.anki2")]
    if len(profiles) != 1:
        raise SystemExit(f"Expected one profile in {base}, found {len(profiles)}")
    target = profiles[0] / "collection.anki2"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)
    shutil.copyfile(Path(args.collection).expanduser(), target)


def run_once(
    args: argparse.Namespace, base: Path, out_dir: Path, n: int
) -> dict[str, Any]:
    out_file = out_dir / f"run-{n}.json"
    log_file = out_dir / "logs" / f"run-{n}.log"
    log_file.parent.mkdir(exist_ok=True)
    out_file.unlink(missing_ok=True)
    env = child_env(args, out_file, out_dir, n)
    cmd = child_command(args, base)
    result: dict[str, Any] = {"run": n, "ok": False}

    if args.drop_caches:
        drop_caches()

    with open(log_file, "w", encoding="utf8") as log:
        t0 = time.perf_counter()
        proc = subprocess.Popen(
            cmd,
            cwd=REPO,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = t0 + args.timeout
        resources = None
        try:
            while proc.poll() is None and time.perf_counter() < deadline:
                if resources is None and out_file.exists():
                    resources = sample_resources(proc.pid)
                time.sleep(0.02)
        except BaseException:
            proc.kill()
            raise
        if proc.poll() is None:
            proc.kill()
            proc.wait()
            result["error"] = f"timed out after {args.timeout}s"
        t_exit = time.perf_counter()

    result["exit_code"] = proc.returncode
    result["resources"] = resources
    if not out_file.exists():
        result.setdefault("error", f"no results (exit code {proc.returncode})")
        return result
    data = json.loads(out_file.read_text(encoding="utf8"))
    marks = {name: (t - t0) * 1000 for name, t in data["marks"].items()}
    marks["process_exit"] = (t_exit - t0) * 1000
    spans = [
        {
            "name": s["name"],
            "start_ms": (s["start"] - t0) * 1000,
            "ms": (s["end"] - s["start"]) * 1000,
        }
        for s in data["spans"]
    ]
    result.update(
        ok=PRIMARY_MARK in marks and "error" not in result,
        marks=marks,
        spans=spans,
        versions=data.get("versions"),
    )
    if "quit_requested" in marks:
        result["exit_ms"] = marks["process_exit"] - marks["quit_requested"]
    if args.importtime and not args.exe:
        result["imports"] = parse_importtime(log_file)
    return result


def parse_importtime(log_file: Path) -> list[dict[str, Any]]:
    rows = []
    prefix = "import time:"
    for line in log_file.read_text(encoding="utf8", errors="replace").splitlines():
        parts = line.removeprefix(prefix).split("|")
        if not line.startswith(prefix) or len(parts) != 3:
            continue
        self_us, cumulative_us, name = parts
        if not self_us.strip().isdigit():
            continue
        rows.append(
            {
                "name": name.rstrip(),
                "self_ms": int(self_us) / 1000,
                "cumulative_ms": int(cumulative_us) / 1000,
            }
        )
    return rows


def run_benchmark(args: argparse.Namespace) -> None:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out or REPO / "out" / "startup-bench" / f"{stamp}-{args.label}")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.exe and not dev_python().exists() and not args.python:
        raise SystemExit("out/pyenv not found; run `just build` first")
    base = prepare_base(args, out_dir)
    if args.exe and not args.inject:
        write_probe_addon(base)

    injection = Injection(resolve_exe(args.exe)) if args.exe and args.inject else None
    results: dict[str, Any] = {
        "label": args.label,
        "date": stamp,
        "command": child_command(args, base),
        "settings": {k: v for k, v in vars(args).items() if k not in ("func",)},
        "warmup": [],
        "runs": [],
    }
    try:
        if injection:
            injection.__enter__()
        for i in range(args.warmup):
            res = run_once(args, base, out_dir, -(i + 1))
            results["warmup"].append(res)
            print(f"warm-up {i + 1}/{args.warmup}: {describe(res)}")
            if i == 0:
                install_collection(args, base)
        if not args.warmup:
            install_collection(args, base)
        for i in range(args.runs):
            res = run_once(args, base, out_dir, i + 1)
            results["runs"].append(res)
            print(f"run {i + 1}/{args.runs}: {describe(res)}")
    finally:
        if injection:
            injection.__exit__()
        out_path = out_dir / "results.json"
        out_path.write_text(json.dumps(results, indent=1), encoding="utf8")

    print()
    print_summary(results)
    print(f"\nResults: {out_path}")


def describe(res: dict[str, Any]) -> str:
    if not res.get("ok"):
        return f"FAILED: {res.get('error', 'deck list never painted')}"
    text = f"{res['marks'][PRIMARY_MARK]:.0f} ms to deck list"
    if "exit_ms" in res:
        text += f", {res['exit_ms']:.0f} ms to exit"
    return text


# Reporting
##########################################################################


def _stats(values: list[float]) -> dict[str, float]:
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
        "n": len(values),
    }


def collect(results: dict[str, Any]) -> dict[str, dict[str, list[float]]]:
    """Returns {"marks"|"spans"|"other": {name: [ms per run]}}."""
    out: dict[str, dict[str, list[float]]] = {"marks": {}, "spans": {}, "other": {}}
    for run in results["runs"]:
        if not run.get("ok"):
            continue
        for name, ms in run["marks"].items():
            out["marks"].setdefault(name, []).append(ms)
        totals: dict[str, float] = {}
        for span in run["spans"]:
            totals[span["name"]] = totals.get(span["name"], 0.0) + span["ms"]
        for name, ms in totals.items():
            out["spans"].setdefault(name, []).append(ms)
        if "exit_ms" in run:
            out["other"].setdefault("exit (ms)", []).append(run["exit_ms"])
        if res := run.get("resources"):
            for key in ("rss_mb", "cpu_s", "processes"):
                if res.get(key) is not None:
                    out["other"].setdefault(key, []).append(res[key])
    return out


def _ordered_marks(marks: dict[str, list[float]]) -> list[str]:
    known = [m for m in KEY_MARKS if m in marks]
    rest = sorted(
        (m for m in marks if m not in known), key=lambda m: statistics.median(marks[m])
    )
    return known + rest


def print_summary(results: dict[str, Any]) -> None:
    data = collect(results)
    ok = sum(1 for r in results["runs"] if r.get("ok"))
    print(f"{results['label']}: {ok}/{len(results['runs'])} successful runs")
    if not ok:
        return
    versions = next(r for r in results["runs"] if r.get("ok")).get("versions") or {}
    print(
        "Anki {anki}, Qt {qt}, PyQt {pyqt}, Python {py}".format(
            anki=versions.get("anki", "?"),
            qt=versions.get("qt", "?"),
            pyqt=versions.get("pyqt", "?"),
            py=(versions.get("python") or "?").split()[0],
        )
    )
    header = f"{'':42} {'median':>8} {'min':>8} {'max':>8} {'stdev':>7}"

    print("\nMilestones (ms since process creation)")
    print(header)
    for name in _ordered_marks(data["marks"]):
        _print_row(name, _stats(data["marks"][name]))

    print("\nPhases (ms, summed per run)")
    print(header)
    spans = sorted(data["spans"], key=lambda n: -statistics.median(data["spans"][n]))
    for name in spans:
        _print_row(name, _stats(data["spans"][name]))

    if data["other"]:
        print("\nOther (at deck list painted)")
        print(header)
        for name, values in data["other"].items():
            _print_row(name, _stats(values))

    imports = [r["imports"] for r in results["runs"] if r.get("imports")]
    if imports:
        print("\nSlowest imports by self time (last run, inflated by -X importtime)")
        for row in sorted(imports[-1], key=lambda r: -r["self_ms"])[:25]:
            print(
                f"  {row['self_ms']:8.1f} ms self {row['cumulative_ms']:9.1f} ms cum  {row['name'].strip()}"
            )


def _print_row(name: str, stats: dict[str, float]) -> None:
    print(
        f"{name[:42]:42} {stats['median']:8.1f} {stats['min']:8.1f}"
        f" {stats['max']:8.1f} {stats['stdev']:7.1f}"
    )


def compare(args: argparse.Namespace) -> None:
    loaded = [json.loads(Path(p).read_text(encoding="utf8")) for p in args.results]
    collected = [collect(r) for r in loaded]
    labels = [r["label"][:12] for r in loaded]
    base = collected[0]
    header = f"{'median ms':38} {labels[0]:>12}"
    for label in labels[1:]:
        header += f" {label:>12} {'delta':>8}"
    print(header)
    for section in ("marks", "spans", "other"):
        names = (
            _ordered_marks(base[section])
            if section == "marks"
            else sorted(base[section])
        )
        print(f"-- {section}")
        for name in names:
            first = statistics.median(base[section][name])
            line = f"{name[:38]:38} {first:12.1f}"
            for other in collected[1:]:
                values = other[section].get(name)
                if not values:
                    line += f" {'-':>12} {'':>8}"
                    continue
                med = statistics.median(values)
                pct = (med - first) / first * 100 if first else 0.0
                line += f" {med:12.1f} {pct:+7.1f}%"
            print(line)


# CLI
##########################################################################


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="startup_bench",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers()
    cmp_parser = sub.add_parser("compare", help="compare results.json files")
    cmp_parser.add_argument("results", nargs="+")
    cmp_parser.set_defaults(func=compare)

    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="untimed runs first; the first one creates the profile",
    )
    parser.add_argument("--label", default="run", help="name for this result set")
    parser.add_argument("--out", help="output folder (default: out/startup-bench/...)")
    parser.add_argument(
        "--exe", help="packaged Anki executable or .app (default: dev build)"
    )
    parser.add_argument(
        "--inject",
        action="store_true",
        help="with --exe, put the probe in app_packages for early milestones",
    )
    parser.add_argument("--python", help="Python to run the dev build with")
    parser.add_argument("--copy-base", help="copy this base folder and use the copy")
    parser.add_argument("--copy-addons", help="copy this addons21 folder into the base")
    parser.add_argument("--collection", help="use a copy of this collection.anki2")
    parser.add_argument(
        "--drop-caches",
        action="store_true",
        help="empty the OS file cache before each run (needs admin/root)",
    )
    parser.add_argument(
        "--lang", default="en", help="passed as -l; empty to use the profile's"
    )
    parser.add_argument("--settle-ms", type=int, default=500)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument(
        "--fast-exit", action="store_true", help="skip clean shutdown (no exit time)"
    )
    parser.add_argument("--ankidev", action="store_true", help="set ANKIDEV=1")
    parser.add_argument(
        "--importtime",
        action="store_true",
        help="dev only: python -X importtime (inflates timings)",
    )
    parser.add_argument(
        "--cprofile", action="store_true", help="cProfile until idle (inflates timings)"
    )
    parser.add_argument(
        "--chromium-trace", action="store_true", help="write Chromium startup traces"
    )
    parser.add_argument(
        "--env", action="append", default=[], metavar="KEY=VALUE", help="extra env var"
    )
    parser.add_argument(
        "--anki-arg", action="append", default=[], help="extra Anki argument"
    )
    parser.set_defaults(func=run_benchmark)
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
