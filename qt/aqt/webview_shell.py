# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Experimental single web view for the main window (opt-in).

Normally the main window has three web views: the top toolbar, the main area
and the bottom bar. Each one has its own renderer process. In this mode, one
ShellWebView shows a page with three same-origin iframes instead.

mw.toolbarWeb, mw.web and mw.bottomWeb are still the usual classes, so their
logic is unchanged, but they are never shown. FramedWebView routes the few
places where they touch their page (loading HTML, running JS, heights,
visibility, focus) to their frame in the shell. Their page() returns a proxy
whose runJavaScript() runs in the frame. Other page-level APIs act on an unused
page.

Pages go into their frame with srcdoc, so no URL is fetched and the pages keep
"frame-ancestors 'none'". The SvelteKit pages that the main area loads by URL
get a one-time token that lets the media server allow framing for that load.

Like any same-origin frames, the frames can script each other, so card JS in
the main area can call the toolbar's and bottom bar's pycmd() handlers.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from aqt.qt import QEvent, QObject, Qt, QUrl, QUrlQuery, QWebEngineScript
from aqt.webview import AnkiWebView, AnkiWebViewKind, _profile_for_kind

if TYPE_CHECKING:
    import aqt.main

ENV_VAR = "ANKI_SINGLE_WEBVIEW"
PROFILE_KEY = "singleWebview"

FRAMES = ("top", "main", "bottom")
CMD_PREFIX = "__shell:"
FRAMING_QUERY_KEY = "ankiShellFrame"


def enabled(pm: Any) -> bool:
    if value := os.environ.get(ENV_VAR):
        return value != "0"
    return bool(pm.meta.get(PROFILE_KEY, False))


# JS
##########################################################################

# Runs in every sub-frame of the main-window profile, but only acts in the
# shell's own frames. Defined at document creation, like pycmd() would be.
_FRAME_SCRIPT = """
(function () {
    var frame;
    try {
        frame = window.frameElement;
    } catch (e) {
        return;
    }
    if (!frame || !frame.dataset || !frame.dataset.ankiShellFrame) {
        return;
    }
    var name = frame.dataset.ankiShellFrame;
    var shell = window.parent.__ankiShell;
    window.pycmd = window.bridgeCommand = function (cmd, cb) {
        shell.send(name, cmd, cb);
        return false;
    };
    document.addEventListener("DOMContentLoaded", function () {
        window.pycmd("domDone");
    });
})();
"""

_SHELL_STYLE = """
html, body { margin: 0; height: 100%; overflow: hidden; }
#anki-shell { display: flex; flex-direction: column; height: 100%; }
#anki-shell > iframe { display: block; width: 100%; border: 0; flex: none; height: 0; }
#anki-shell > iframe#main { flex: 1 1 auto; min-height: 0; }
#anki-shell > iframe.animate { transition: height var(--transition, 180ms) ease-in-out; }
"""

_SHELL_SCRIPT = """
globalThis.__ankiShell = (function () {
    const frame = (name) => document.getElementById(name);
    // pycmd() is defined once the web channel has connected
    const queue = [];
    const send = (name, cmd, cb) => {
        const msg = %(prefix)s + JSON.stringify([name, cmd]);
        if (typeof pycmd === "function") {
            pycmd(msg, cb);
        } else {
            if (!queue.length) {
                const flush = () => {
                    if (typeof pycmd !== "function") {
                        return setTimeout(flush, 5);
                    }
                    for (const [m, c] of queue.splice(0)) {
                        pycmd(m, c);
                    }
                };
                setTimeout(flush, 5);
            }
            queue.push([msg, cb]);
        }
    };
    for (const name of %(frames)s) {
        const el = frame(name);
        el.addEventListener("load", () => send(name, "__loaded"));
        el.addEventListener("mouseenter", () => send(name, "__enter"));
        el.addEventListener("mouseleave", () => send(name, "__leave"));
    }
    new ResizeObserver(() =>
        send("main", "__size:" + frame("main").clientHeight)
    ).observe(frame("main"));
    return {
        send,
        setHtml(name, html) {
            const el = frame(name);
            el.removeAttribute("src");
            el.srcdoc = html;
        },
        loadUrl(name, url) {
            const el = frame(name);
            el.removeAttribute("srcdoc");
            el.src = url;
        },
        eval(name, js) {
            return frame(name).contentWindow.eval(js);
        },
        setHeight(name, height, animate) {
            const el = frame(name);
            el.classList.toggle("animate", animate);
            el.style.height = height + "px";
        },
        setStyle(name, prop, value) {
            frame(name).style[prop] = value;
        },
        focus(name) {
            frame(name).contentWindow.focus();
        },
    };
})();
"""


def _frame_script() -> QWebEngineScript:
    script = QWebEngineScript()
    script.setName("anki-shell-frame")
    script.setSourceCode(_FRAME_SCRIPT)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setRunsOnSubFrames(True)
    return script


def _frame_html(html: str) -> str:
    """Link clicks in our pages must reach acceptNavigationRequest() as
    top-level navigations, so that they open externally as before."""
    return html.replace("<head>", '<head><base target="_top">', 1)


# The shell
##########################################################################


class ShellWebView(AnkiWebView):
    def __init__(self, mw: aqt.main.AnkiQt) -> None:
        super().__init__(mw, kind=AnkiWebViewKind.SHELL)
        self.mw = mw
        self.requiresCol = False
        self.setFocusPolicy(Qt.FocusPolicy.WheelFocus)
        self.setMinimumWidth(400)
        self.setAcceptDrops(True)
        self.views: dict[str, FramedWebView] = {}
        scripts = _profile_for_kind(AnkiWebViewKind.SHELL).scripts()
        assert scripts is not None
        if not scripts.find("anki-shell-frame"):
            scripts.insert(_frame_script())
        body = "<div id=anki-shell>%s</div><script>%s</script>" % (
            "".join(
                f'<iframe id={name} data-anki-shell-frame="{name}"></iframe>'
                for name in FRAMES
            ),
            _SHELL_SCRIPT
            % dict(prefix=json.dumps(CMD_PREFIX), frames=json.dumps(list(FRAMES))),
        )
        self.stdHtml(
            body,
            css=[],
            js=[],
            head=f"<style>{_SHELL_STYLE}</style>",
            default_css=False,
            context=self,
        )

    def attach(self, name: str, view: FramedWebView) -> None:
        self.views[name] = view

    def _onBridgeCmd(self, cmd: str) -> Any:
        if not cmd.startswith(CMD_PREFIX):
            return super()._onBridgeCmd(cmd)
        name, frame_cmd = json.loads(cmd[len(CMD_PREFIX) :])
        if view := self.views.get(name):
            return view._on_frame_cmd(frame_cmd)
        return None

    def call(self, method: str, *args: Any, cb: Callable | None = None) -> None:
        js_args = ", ".join(json.dumps(arg) for arg in args)
        self.evalWithCallback(f"__ankiShell.{method}({js_args})", cb)

    # Drag & drop goes to the main area, as before
    def dragEnterEvent(self, event: Any) -> None:
        if view := self.views.get("main"):
            view.dragEnterEvent(event)

    def dropEvent(self, event: Any) -> None:
        if view := self.views.get("main"):
            view.dropEvent(event)


# The views in the frames
##########################################################################


class _FramePage(QObject):
    """What FramedWebView.page() returns. runJavaScript() runs in the frame;
    everything else uses the view's own (unused) page."""

    def __init__(self, view: FramedWebView, page: Any) -> None:
        super().__init__(view)
        self._view = view
        self._page = page

    def runJavaScript(self, js: str, *args: Any) -> None:
        cb = next((arg for arg in args if callable(arg)), None)
        self._view._run_in_frame(js, cb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._page, name)


class FramedWebView(AnkiWebView):
    """Mixed into the main window's view classes in single web view mode."""

    _shell: ShellWebView
    _frame: str

    def _init_frame(self, shell: ShellWebView, frame: str) -> None:
        self._shell = shell
        self._frame = frame
        self._frame_visible = True
        self._frame_height = 0
        # there is no focus proxy, as the view is never shown
        self._filterSet = True
        # not a top-level window, so it is not reported as left open on close
        self.setParent(shell)
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        super().setVisible(False)
        self._page_proxy = _FramePage(self, super().page())
        shell.attach(frame, self)

    # Page content

    def _setHtml(self, html: str, context: Any) -> None:
        from aqt import mw

        self._domDone = False
        # kept for code that reads it back
        mw.mediaServer.set_page_html(id(self), html, context)
        self._shell.call("setHtml", self._frame, _frame_html(html))

    def load_url(self, url: QUrl) -> None:
        from aqt import mw

        self._domDone = False
        self.allow_drops = False
        # lets the media server allow framing for this one load
        token = secrets.token_urlsafe(16)
        mw.mediaServer.allow_framing_once(token)
        query = QUrlQuery(url)
        query.addQueryItem(FRAMING_QUERY_KEY, token)
        url = QUrl(url)
        url.setQuery(query)
        self._shell.call("loadUrl", self._frame, url.toString())

    def _evalWithCallback(self, js: str, cb: Callable[[Any], Any] | None) -> None:
        self._run_in_frame(js, cb)

    def _run_in_frame(self, js: str, cb: Callable[[Any], Any] | None) -> None:
        def handler(val: Any) -> None:
            if self._shouldIgnoreWebEvent():
                print("ignored late js callback", cb)
                return
            if cb:
                cb(val)

        self._shell.call("eval", self._frame, js, cb=handler)

    def page(self) -> Any:
        # AnkiWebView.__init__() uses the page before _init_frame()
        return getattr(self, "_page_proxy", None) or super().page()

    # Messages from the frame

    def _on_frame_cmd(self, cmd: str) -> Any:
        if cmd == "__loaded":
            # Anki's own handler is connected too
            self.loadFinished.emit(True)
        elif cmd in ("__enter", "__leave"):
            type = QEvent.Type.Enter if cmd == "__enter" else QEvent.Type.Leave
            self.eventFilter(self, QEvent(type))
        elif cmd.startswith("__size:"):
            self._frame_height = int(float(cmd[len("__size:") :]))
        else:
            return self._onBridgeCmd(cmd)
        return None

    # Geometry, visibility and focus

    def setFixedHeight(self, h: int) -> None:
        self._frame_height = h
        self._shell.call("setHeight", self._frame, h, False)

    def animate_height(self, height: int) -> None:
        from aqt import mw

        self.web_height = height
        self._frame_height = height
        self._shell.call("setHeight", self._frame, height, not mw.pm.reduce_motion())

    def height(self) -> int:
        return self._frame_height

    def setVisible(self, visible: bool) -> None:
        # the view itself stays hidden
        self._frame_visible = visible
        self._shell.call(
            "setStyle", self._frame, "display", "block" if visible else "none"
        )

    def isVisible(self) -> bool:
        return self._frame_visible

    def hide_while_preserving_layout(self) -> None:
        self._frame_visible = False
        self._shell.call("setStyle", self._frame, "visibility", "hidden")

    def setFocus(self, *args: Any) -> None:
        self._shell.setFocus()
        self._shell.call("focus", self._frame)

    def zoomFactor(self) -> float:
        return self._shell.zoomFactor()

    def setZoomFactor(self, factor: float) -> None:
        if self._frame == "main":
            self._shell.setZoomFactor(factor)

    def selectedText(self) -> str:
        return self._shell.selectedText()

    def onCopy(self) -> None:
        self._shell.onCopy()

    def onCut(self) -> None:
        self._shell.onCut()

    def onPaste(self) -> None:
        self._shell.onPaste()

    def onSelectAll(self) -> None:
        self._shell.onSelectAll()

    def force_load_hack(self) -> None:
        pass


def framed(cls: type[AnkiWebView]) -> type[Any]:
    """A version of a main-window view class that lives in a shell frame."""
    return type(f"Framed{cls.__name__}", (FramedWebView, cls), {})


def setup_views(
    mw: aqt.main.AnkiQt,
    top_cls: type[AnkiWebView],
    main_cls: type[AnkiWebView],
    bottom_cls: type[AnkiWebView],
) -> tuple[ShellWebView, Any, Any, Any]:
    shell = ShellWebView(mw)
    views = []
    for name, cls in zip(FRAMES, (top_cls, main_cls, bottom_cls)):
        view = cast(Any, framed(cls))(mw)
        view._init_frame(shell, name)
        views.append(view)
    top, main, bottom = views
    return shell, top, main, bottom
