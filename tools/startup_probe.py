# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""
Startup timing probe that runs inside Anki. Driven by tools/startup_bench.py.

It can be loaded in three ways:

- as the main script of a dev build (all markers)
- from a .pth file in a packaged build's app_packages (all markers)
- as an add-on (only markers after add-on loading)

All times are time.perf_counter() values, which share a system-wide clock
with the parent benchmark process on Windows, macOS and Linux.
"""

from __future__ import annotations

import time

_T_LOADED = time.perf_counter()

import functools
import importlib.abc
import json
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any

ENV_OUT = "ANKI_STARTUP_BENCH_OUT"
ENV_SETTLE_MS = "ANKI_STARTUP_BENCH_SETTLE_MS"
ENV_FAST_EXIT = "ANKI_STARTUP_BENCH_FAST_EXIT"
ENV_CPROFILE = "ANKI_STARTUP_BENCH_CPROFILE"
ENV_MODE = "ANKI_STARTUP_BENCH_MODE"

PAINTED_CMD = "startup-bench:painted"
# Also reports browser-side times of the deck list page, in ms since the epoch.
PAINTED_JS = (
    """
(() => {
  const now = () => performance.timeOrigin + performance.now();
  const times = {eval_run: now()};
  const nav = performance.getEntriesByType("navigation")[0];
  if (nav) {
    times.response_end = performance.timeOrigin + nav.responseEnd;
    times.dom_content_loaded = performance.timeOrigin + nav.domContentLoadedEventEnd;
  }
  requestAnimationFrame(() => {
    times.first_frame = now();
    requestAnimationFrame(() => {
      times.second_frame = now();
      for (const entry of performance.getEntriesByType("paint")) {
        times[entry.name] = performance.timeOrigin + entry.startTime;
      }
      pycmd("%s:" + JSON.stringify(times));
    });
  });
})();
"""
    % PAINTED_CMD
)

# Module imports to time. Extension modules are timed from create_module().
WATCHED_IMPORTS = (
    "aqt",
    "aqt.qt",
    "aqt.main",
    "aqt.webview",
    "aqt.webengine_profile",
    "aqt.mediasrv",
    "aqt.reviewer",
    "aqt.editor",
    "aqt.browser",
    "anki.collection",
    "anki._backend",
    "anki._rsbridge",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtMultimedia",
)

# AnkiQt methods to time (first call only).
MAIN_WINDOW_METHODS = (
    "setupUI",
    "setup_crashlog",
    "setupKeys",
    "setupThreads",
    "setup_webengine",
    "setupMediaServer",
    "setupSpellCheck",
    "setupProgress",
    "setupStyle",
    "setupMainWindow",
    "setupMenus",
    "setupErrorHandler",
    "setupHooks",
    "setupDeckBrowser",
    "setupOverview",
    "setupReviewer",
    "setupAddons",
    "finish_ui_setup",
    "setupProfile",
    "loadProfile",
    "loadCollection",
    "setup_sound",
    "update_undo_actions",
    "apply_collection_options",
    "moveToState",
    "show",
    "maybe_auto_sync_on_open_close",
)

# Functions in aqt.main's namespace to time (first call only).
MAIN_MODULE_FUNCTIONS = ("restoreGeom", "restoreState", "FlagManager")


class Recorder:
    def __init__(self) -> None:
        self.marks: dict[str, float] = {}
        self.spans: list[dict[str, Any]] = []
        self.finished = False

    def mark(self, name: str, t: float | None = None) -> None:
        self.marks.setdefault(name, time.perf_counter() if t is None else t)

    def span(self, name: str, start: float, end: float) -> None:
        self.spans.append({"name": name, "start": start, "end": end})


REC = Recorder()
_profiler: Any = None
_keepalive: list[Any] = []


def enabled() -> bool:
    return bool(os.environ.get(ENV_OUT))


# Import timing
##########################################################################


class ImportWatcher(importlib.abc.MetaPathFinder):
    """Delegates to the other finders, then wraps the loader to time it."""

    def __init__(
        self, names: Sequence[str], on_loaded: Callable[[str, Any], None]
    ) -> None:
        self.names = set(names)
        self.on_loaded = on_loaded

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: Any = None
    ) -> Any:
        if fullname not in self.names:
            return None
        self.names.discard(fullname)
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                if spec.loader is not None:
                    self._wrap_loader(fullname, spec.loader)
                return spec
        return None

    def _wrap_loader(self, fullname: str, loader: Any) -> None:
        state: dict[str, float] = {}
        orig_create = getattr(loader, "create_module", None)
        orig_exec = loader.exec_module

        def create_module(spec: Any) -> Any:
            state["start"] = time.perf_counter()
            return orig_create(spec) if orig_create else None

        def exec_module(module: Any) -> None:
            start = state.get("start", time.perf_counter())
            try:
                orig_exec(module)
            finally:
                REC.span(f"import {fullname}", start, time.perf_counter())
            # The module is not yet an attribute of its parent package here.
            try:
                self.on_loaded(fullname, module)
            except Exception as exc:
                print(f"startup probe: {fullname}: {exc!r}", file=sys.stderr)

        # Instance attributes shadow the loader class's methods.
        loader.create_module = create_module
        loader.exec_module = exec_module


_patched_modules: set[str] = set()


def _on_module_loaded(fullname: str, module: Any) -> None:
    if fullname in _patched_modules:
        return
    _patched_modules.add(fullname)
    if fullname == "aqt.mediasrv":
        _wrap_media_server(module)
    elif fullname == "aqt":
        REC.mark("aqt_imported")
        install_aqt_patches()
        install_hooks()
        # older versions import these with aqt
        for name in ("aqt.main", "aqt.webview"):
            if name in sys.modules:
                _on_module_loaded(name, sys.modules[name])
    elif fullname == "aqt.main":
        install_main_patches(module)
    elif fullname == "aqt.webengine_profile":
        _wrap_profile_creation(module, "new_profile")
    elif fullname == "aqt.webview":
        if "aqt.webengine_profile" not in _patched_modules:
            # older versions
            _wrap_profile_creation(module, "_profile_for_kind")
        _wrap_webview(module.AnkiWebView)


# Patching
##########################################################################


def _wrap_first_call(owner: Any, attr: str, name: str, max_calls: int = 1) -> None:
    orig = getattr(owner, attr, None)
    if orig is None or getattr(orig, "_startup_probe", False):
        return
    calls = 0

    @functools.wraps(orig)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        if calls >= max_calls:
            return orig(*args, **kwargs)
        calls += 1
        start = time.perf_counter()
        try:
            return orig(*args, **kwargs)
        finally:
            REC.span(name, start, time.perf_counter())

    wrapper._startup_probe = True  # type: ignore[attr-defined]
    setattr(owner, attr, wrapper)


def install_aqt_patches() -> None:
    import aqt

    _wrap_first_call(aqt.AnkiApp, "__init__", "QApplication()")
    _wrap_first_call(aqt.ProfileManager, "setupMeta", "ProfileManager.setupMeta")
    _wrap_first_call(aqt, "setupGL", "setupGL", max_calls=2)
    _wrap_first_call(aqt, "setupLangAndBackend", "setupLangAndBackend")
    _wrap_first_call(
        aqt,
        "init_webengine_and_import_modules",
        "init_webengine_and_import_modules",
    )


def install_main_patches(main: Any) -> None:
    _wrap_first_call(main.AnkiQt, "__init__", "AnkiQt()")
    for method in MAIN_WINDOW_METHODS:
        _wrap_first_call(main.AnkiQt, method, f"AnkiQt.{method}")
    for func in MAIN_MODULE_FUNCTIONS:
        _wrap_first_call(main, func, func)

    orig_init = main.AnkiQt.__init__

    @functools.wraps(orig_init)
    def mw_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        # The window is first shown later, from a timer.
        _install_window_filter(self)

    main.AnkiQt.__init__ = mw_init


def _wrap_profile_creation(module: Any, attr: str) -> None:
    owner = module
    if not hasattr(owner, attr):
        # older versions
        owner, attr = module.AnkiWebPage, "_profileForPage"
    orig = getattr(owner, attr, None)
    if orig is None:
        return

    @functools.wraps(orig)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            return orig(*args, **kwargs)
        finally:
            end = time.perf_counter()
            # Cached lookups take microseconds; only creation is of interest.
            if end - start > 0.001:
                REC.span("QWebEngineProfile()", start, end)

    setattr(owner, attr, wrapper)


def _wrap_media_server(mediasrv: Any) -> None:
    """Time each HTTP request that the media server handles in Python. Other
    requests are only visible in the js:* marks."""
    handler = getattr(mediasrv, "_PythonHandler", None)
    if handler is not None:
        orig_handle = handler.handle

        def handle(self: Any, method: str, path: str, *args: Any) -> Any:
            start = time.perf_counter()
            REC.mark("http_first_python_request", start)
            try:
                return orig_handle(self, method, path, *args)
            finally:
                end = time.perf_counter()
                name = path.removeprefix("_anki/")
                REC.span(f"http {name}", start, end)
                REC.mark(f"http_done:{name}", end)

        handler.handle = handle
        return

    # older versions served everything with Flask
    app = mediasrv.app
    orig = app.wsgi_app

    def wsgi_app(environ: dict[str, Any], start_response: Any) -> Any:
        start = time.perf_counter()
        REC.mark("http_first_request", start)
        path = environ.get("PATH_INFO", "").removeprefix("/_anki/")
        try:
            return orig(environ, start_response)
        finally:
            end = time.perf_counter()
            REC.span(f"http {path}", start, end)
            REC.mark(f"http_done:{path}", end)

    app.wsgi_app = wsgi_app  # type: ignore[method-assign]


def _webview_kind(view: Any) -> str:
    kind = getattr(view, "_kind", None)
    return getattr(kind, "value", None) or type(view).__name__


def _wrap_webview(view_class: Any) -> None:
    orig_init = view_class.__init__
    orig_load_finished = getattr(view_class, "_on_load_finished", None)
    orig_bridge = getattr(view_class, "_onBridgeCmd", None)

    @functools.wraps(orig_init)
    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        start = time.perf_counter()
        try:
            orig_init(self, *args, **kwargs)
        finally:
            REC.span(f"AnkiWebView({_webview_kind(self)})", start, time.perf_counter())

    view_class.__init__ = init

    if orig_load_finished is not None:
        # PyQt drops the signal's ok argument for the original one-arg slot,
        # so it must not be forwarded.
        @functools.wraps(orig_load_finished)
        def load_finished(self: Any, *args: Any) -> Any:
            REC.mark(f"load_finished:{_webview_kind(self)}")
            return orig_load_finished(self)

        view_class._on_load_finished = load_finished

    if orig_bridge is not None:

        @functools.wraps(orig_bridge)
        def bridge(self: Any, cmd: str, *args: Any, **kwargs: Any) -> Any:
            if cmd == "domDone":
                REC.mark(f"dom_done:{_webview_kind(self)}")
            return orig_bridge(self, cmd, *args, **kwargs)

        view_class._onBridgeCmd = bridge


def _install_window_filter(mw: Any) -> None:
    from aqt.qt import QEvent, QObject

    class WindowFilter(QObject):
        def eventFilter(self, obj: Any, evt: Any) -> bool:
            if evt.type() == QEvent.Type.Show:
                REC.mark("window_shown")
            elif evt.type() == QEvent.Type.Paint:
                REC.mark("window_first_paint")
            return False

    filt = WindowFilter()
    _keepalive.append(filt)
    mw.installEventFilter(filt)


# Hooks
##########################################################################


def install_hooks() -> None:
    from aqt import gui_hooks

    def on_main_window_did_init() -> None:
        REC.mark("main_window_did_init")

    def on_profile_did_open() -> None:
        REC.mark("profile_did_open")
        _disable_network_checks()

    def on_collection_did_load(col: Any) -> None:
        REC.mark("collection_did_load")

    def on_deck_browser_did_render(deck_browser: Any) -> None:
        if "deck_browser_did_render" in REC.marks:
            return
        REC.mark("deck_browser_did_render")
        # Two frames: the second callback runs once the frame that holds
        # the deck list has been committed.
        deck_browser.web.eval(PAINTED_JS)

    def on_js_message(handled: tuple[bool, Any], message: str, context: Any) -> Any:
        if not message.startswith(PAINTED_CMD):
            return handled
        REC.mark("deck_browser_painted")
        _record_js_times(message[len(PAINTED_CMD) + 1 :])
        from aqt.qt import QTimer

        QTimer.singleShot(0, _on_event_loop_idle)
        return (True, None)

    gui_hooks.main_window_did_init.append(on_main_window_did_init)
    gui_hooks.profile_did_open.append(on_profile_did_open)
    gui_hooks.collection_did_load.append(on_collection_did_load)
    gui_hooks.deck_browser_did_render.append(on_deck_browser_did_render)
    gui_hooks.webview_did_receive_js_message.append(on_js_message)
    _keepalive.extend(
        [
            on_main_window_did_init,
            on_profile_did_open,
            on_collection_did_load,
            on_deck_browser_did_render,
            on_js_message,
        ]
    )


def _record_js_times(payload: str) -> None:
    try:
        times = json.loads(payload)
    except ValueError:
        return
    # epoch ms -> perf_counter seconds
    offset = time.time() - time.perf_counter()
    for name, epoch_ms in times.items():
        if isinstance(epoch_ms, (int, float)):
            REC.mark(f"js:{name}", epoch_ms / 1000 - offset)


def _disable_network_checks() -> None:
    import aqt

    pm = aqt.mw.pm
    for setter in ("set_update_check", "set_check_for_addon_updates"):
        if fn := getattr(pm, setter, None):
            fn(False)


def _on_event_loop_idle() -> None:
    REC.mark("event_loop_idle")
    _stop_profiler()
    write_results()
    from aqt.qt import QTimer

    QTimer.singleShot(int(os.environ.get(ENV_SETTLE_MS, "500")), _quit)


def _quit() -> None:
    import aqt

    REC.mark("quit_requested")
    write_results()
    if os.environ.get(ENV_FAST_EXIT):
        os._exit(0)
    aqt.mw.unloadProfileAndExit()


# Output
##########################################################################


def _versions() -> dict[str, Any]:
    aqt = sys.modules.get("aqt")
    info: dict[str, Any] = {
        "python": sys.version,
        "platform": sys.platform,
        "executable": sys.executable,
        "aqt_file": getattr(aqt, "__file__", None),
    }
    try:
        from anki.buildinfo import version

        info["anki"] = version
    except Exception:
        pass
    try:
        from aqt.qt import PYQT_VERSION_STR, QT_VERSION_STR, qWebEngineChromiumVersion

        info["qt"] = QT_VERSION_STR
        info["pyqt"] = PYQT_VERSION_STR
        info["chromium"] = qWebEngineChromiumVersion()
    except Exception:
        pass
    return info


def write_results() -> None:
    out = os.environ.get(ENV_OUT)
    if not out:
        return
    data = {
        "pid": os.getpid(),
        "mode": os.environ.get(ENV_MODE, "unknown"),
        "marks": REC.marks,
        "spans": REC.spans,
        "versions": _versions(),
    }
    tmp = f"{out}.tmp"
    with open(tmp, "w", encoding="utf8") as file:
        json.dump(data, file, indent=1)
    os.replace(tmp, out)


def _start_profiler() -> None:
    global _profiler
    if os.environ.get(ENV_CPROFILE):
        import cProfile

        _profiler = cProfile.Profile()
        _profiler.enable()


def _stop_profiler() -> None:
    global _profiler
    if _profiler is not None and (path := os.environ.get(ENV_CPROFILE)):
        _profiler.disable()
        _profiler.dump_stats(path)
        _profiler = None


# Entry points
##########################################################################


def install_early() -> None:
    """Call before aqt is imported."""
    if not enabled() or any(isinstance(f, ImportWatcher) for f in sys.meta_path):
        return
    REC.mark("python_ready", _T_LOADED)
    _start_profiler()
    if "aqt" in sys.modules:
        _on_module_loaded("aqt", sys.modules["aqt"])
        return
    sys.meta_path.insert(0, ImportWatcher(WATCHED_IMPORTS, _on_module_loaded))


def install_from_addon() -> None:
    """Call from an add-on, after aqt and the main window exist."""
    if not enabled() or "aqt_imported" in REC.marks:
        return
    import aqt
    import aqt.main

    REC.mark("addon_loaded", _T_LOADED)
    for method in ("finish_ui_setup", "setupProfile", "loadProfile", "loadCollection"):
        _wrap_first_call(aqt.main.AnkiQt, method, f"AnkiQt.{method}")
    _install_window_filter(aqt.mw)
    install_hooks()


def main() -> None:
    """Run a dev build, as tools/run.py does."""
    sys.path.extend(["pylib", "qt", "out/pylib", "out/qt"])
    install_early()
    import aqt

    aqt.run()


if __name__ == "__main__":
    main()
