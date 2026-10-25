# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The HTTP server for Anki's web views.

The server itself is in rsbridge (mediasrv.rs). It serves bundled files, add-on
web exports, collection media and legacy page HTML without the GIL, and passes
other requests (mainly the /_anki/ API below) to _PythonHandler.
"""

from __future__ import annotations

import asyncio
import enum
import inspect
import logging
import os
import re
import secrets
import threading
import traceback
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, cast

import stringcase

import aqt
import aqt.operations
from anki import frontend_pb2, generic_pb2, hooks
from anki.collection import (
    NestedOpChanges,
    OpChanges,
    OpChangesOnly,
    Progress,
    SearchNode,
)
from anki.decks import UpdateDeckConfigs, UpdateDeckConfigsMode
from anki.scheduler.v3 import SchedulingStatesWithContext, SetSchedulingStatesRequest
from anki.utils import from_json_bytes, to_json_bytes
from aqt.changenotetype import ChangeNotetypeDialog
from aqt.deckoptions import DeckOptionsDialog
from aqt.operations import on_op_finished
from aqt.operations.deck import update_deck_configs as update_deck_configs_op
from aqt.progress import ProgressUpdate
from aqt.qt import *
from aqt.utils import (
    aqt_data_path,
    askUser,
    openLink,
    show_info,
    show_warning,
    tr,
)

if TYPE_CHECKING:
    import aqt.main

logger = logging.getLogger(__name__)


UNTRUSTED_MEDIA_CSP = "; ".join(
    (
        # Disallow everything by default
        "default-src 'none'",
        "script-src 'none'",
        "connect-src 'none'",
        "object-src 'none'",
        "frame-src 'none'",
        "child-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        # Allow same-origin styles, images, fonts and media, so that an SVG or HTML
        # file can use the resources next to it. 'unsafe-inline' is needed for
        # <style> elements and style= attributes inside SVGs.
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self'",
        "font-src 'self'",
        "media-src 'self'",
        # The sandbox blocks scripts, forms, popups and top-level navigation.
        # allow-same-origin is required for fonts (font loads use CORS, and we send
        # no CORS headers) and to avoid stuck :hover styles under site isolation.
        # Never add allow-scripts: with allow-same-origin, that would give media
        # access to the parent page.
        "sandbox allow-same-origin",
    )
)


# Our pages are always shown top-level
TRUSTED_PAGE_CSP = "frame-ancestors 'none'"


def _untrusted_page_content_security_policy(script_src: str) -> str:
    """CSP for pages that show user content, e.g. note fields in the editor."""
    return "; ".join(
        (
            f"script-src {script_src}",
            "form-action 'none'",
            TRUSTED_PAGE_CSP,
        )
    )


def _legacy_editor_content_security_policy(port: int) -> str:
    csp_paths = (
        f"http://127.0.0.1:{port}/_anki/",
        f"http://127.0.0.1:{port}/_addons/",
    )
    return _untrusted_page_content_security_policy(" ".join(csp_paths))


def _untrusted_sveltekit_content_security_policy(
    port: int, script_hash: str | None
) -> str:
    csp_paths = [
        f"http://127.0.0.1:{port}/_anki/",
        f"http://127.0.0.1:{port}/_app/",
        f"http://127.0.0.1:{port}/_addons/",
    ]
    if script_hash:
        csp_paths.append(script_hash)
    return _untrusted_page_content_security_policy(" ".join(csp_paths))


class PageContext(enum.IntEnum):
    UNKNOWN = enum.auto()
    # mediasrv.rs gives this context the editor CSP
    EDITOR = enum.auto()
    REVIEWER = enum.auto()
    PREVIEWER = enum.auto()
    CARD_LAYOUT = enum.auto()
    DECK_OPTIONS = enum.auto()
    # something in /_anki/pages/
    NON_LEGACY_PAGE = enum.auto()
    # Do not use this if you present user content (e.g. content from cards), as it's a
    # security issue.
    ADDON_PAGE = enum.auto()


@dataclass
class LegacyPage:
    html: str
    context: PageContext


class MediaServer:
    """Runs the server in rsbridge; see the module docstring."""

    def __init__(self, mw: aqt.main.AnkiQt) -> None:
        self.mw = mw
        self.is_shutdown = False
        # map of webview ids to pages
        self._legacy_pages: dict[int, LegacyPage] = {}
        self._server: Any = None

    def start(self) -> None:
        from anki._rsbridge import MediaServer as RustMediaServer

        host = os.getenv("ANKI_API_HOST", "127.0.0.1")
        self._server = RustMediaServer(
            host,
            int(os.getenv("ANKI_API_PORT") or 0),
            str(aqt_data_path() / "web"),
            self.mw.pm.addonFolder(),
            _PythonHandler(),
            allow_any_host=host == "0.0.0.0",
        )
        port = self._server.port
        self._server.set_csp(
            TRUSTED_PAGE_CSP,
            UNTRUSTED_MEDIA_CSP,
            _legacy_editor_content_security_policy(port),
            _untrusted_sveltekit_content_security_policy(port, "{hash}"),
        )
        if _flask_app is not None:
            self._server.set_python_routes(True)
        aqt.gui_hooks.collection_did_load.append(self._on_collection_did_load)
        aqt.gui_hooks.profile_did_open.append(self.update_media_file_filter)
        logger.info("Serving on http://%s:%s", host, port)

    def _on_collection_did_load(self, col: Any) -> None:
        self._server.set_media_dir(col.media.dir())
        self.update_media_file_filter()

    def update_media_file_filter(self) -> None:
        """Call after adding a hooks.media_file_filter handler at runtime."""
        if self._server:
            self._server.set_filter_media_paths(bool(hooks.media_file_filter._hooks))

    def set_web_exports(self, addon: str, pattern: str | None) -> None:
        if self._server:
            self._server.set_web_exports(addon, pattern)

    def shutdown(self) -> None:
        self.is_shutdown = True
        if self._server:
            self._server.shutdown()

    def getPort(self) -> int:
        return int(self._server.port)

    def set_page_html(
        self, id: int, html: str, context: PageContext = PageContext.UNKNOWN
    ) -> None:
        self._legacy_pages[id] = LegacyPage(html, context)
        self._server.set_page(id, html, int(context))

    def get_page(self, id: int) -> LegacyPage | None:
        return self._legacy_pages.get(id)

    def get_page_html(self, id: int) -> str | None:
        if page := self.get_page(id):
            return page.html
        else:
            return None

    def get_page_context(self, id: int) -> PageContext | None:
        if page := self.get_page(id):
            return page.context
        else:
            return None

    def clear_page_html(self, id: int) -> None:
        self._legacy_pages.pop(id, None)
        if self._server:
            self._server.clear_page(id)


# Requests and responses of the Python handlers
##########################################################################


class Headers:
    """Case-insensitive request headers."""

    def __init__(self, items: list[tuple[str, str]]) -> None:
        self._items = {name.lower(): value for name, value in items}

    def get(self, name: str, default: Any = None) -> Any:
        return self._items.get(name.lower(), default)

    def __contains__(self, name: str) -> bool:
        return name.lower() in self._items


@dataclass
class Request:
    method: str
    # with a leading /
    path: str
    args: dict[str, str]
    headers: Headers
    data: bytes


@dataclass
class Response:
    data: bytes | str = b""
    status: int = HTTPStatus.OK
    headers: dict[str, str] = field(default_factory=dict)

    def body(self) -> bytes:
        return self.data.encode("utf8") if isinstance(self.data, str) else self.data


class HTTPError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def abort(status: int) -> None:
    raise HTTPError(status)


_current = threading.local()


class _RequestProxy:
    """The request that the current thread handles, like flask.request."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_current.request, name)


request = cast(Request, _RequestProxy())


def _text_response(code: HTTPStatus, text: str) -> Response:
    """Return an error message.

    Response is returned as text/plain, so no escaping of untrusted
    input is required."""
    return Response(text, code, {"Content-Type": "text/plain; charset=utf-8"})


DynamicRequest = Callable[[], Response]


@dataclass
class NotFound:
    message: str


class UnsafePathException(Exception):
    def __init__(self, path: str):
        super().__init__(f"Invalid path: {path}")


def ensure_safe_path(base_dir: str | Path, path: str | Path) -> str:
    base_dir = os.path.realpath(base_dir)
    path = os.path.normpath(path)
    fullpath = os.path.abspath(os.path.join(base_dir, path))

    # protect against directory traversal: https://security.openstack.org/guidelines/dg_using-file-paths.html
    if not fullpath.startswith(base_dir + os.sep):
        raise UnsafePathException(path)
    return fullpath


_LOCALHOST_HOSTS = ("127.0.0.1", "localhost", "[::1]")

_ALLOWED_ORIGIN_PREFIXES = tuple(
    f"{scheme}{host}" for scheme in ("http://", "https://") for host in _LOCALHOST_HOSTS
)


def is_localhost_origin(origin: str) -> bool:
    for prefix in _ALLOWED_ORIGIN_PREFIXES:
        if (
            origin == prefix
            or origin.startswith(prefix + ":")
            or origin.startswith(prefix + "/")
        ):
            return True
    return False


def get_sveltekit_route(path: str) -> str | None:
    page_name = path.split("/", maxsplit=1)[0]
    if page_name in [
        "graphs",
        "congrats",
        "card-info",
        "change-notetype",
        "deck-options",
        "import-anki-package",
        "import-csv",
        "import-page",
        "image-occlusion",
        "preferences",
        "editor",
    ]:
        return page_name
    return None


def is_untrusted_sveltekit_route(route: str) -> bool:
    return route in ("editor", "image-occlusion")


def readyz() -> Response:
    """Reports whether the profile's collection is open.

    The HTTP server starts listening before the profile/collection finishes
    loading, so callers that need the collection open - e.g. the e2e test
    harness's webServer readiness check - should poll this instead of
    /favicon.ico, which responds regardless of collection state.
    """
    if aqt.mw.col is None:
        return Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
    return Response(status=HTTPStatus.OK)


def congrats_info() -> bytes:
    if not aqt.mw.col.sched._is_finished():
        aqt.mw.taskman.run_on_main(lambda: aqt.mw.moveToState("overview"))
    return raw_backend_request("congrats_info")()


def get_deck_configs_for_update() -> bytes:
    return aqt.mw.col._backend.get_deck_configs_for_update_raw(request.data)


def _on_update_deck_configs_success(input: UpdateDeckConfigs) -> None:
    is_compute_all = (
        input.mode == UpdateDeckConfigsMode.UPDATE_DECK_CONFIGS_MODE_COMPUTE_ALL_PARAMS
    )
    if not is_compute_all and isinstance(
        window := aqt.mw.app.activeModalWidget(), DeckOptionsDialog
    ):
        window.reject()


def update_deck_configs() -> bytes:
    # the regular change tracking machinery expects to be started on the main
    # thread and uses a callback on success, so we need to run this op on
    # main, and return immediately from the web request

    input = UpdateDeckConfigs()
    input.ParseFromString(request.data)

    def on_progress(progress: Progress, update: ProgressUpdate) -> None:
        if progress.HasField("compute_memory"):
            val = progress.compute_memory
            update.max = val.total_cards
            update.value = val.current_cards
            update.label = val.label
        elif progress.HasField("compute_params"):
            val2 = progress.compute_params
            # prevent an indeterminate progress bar from appearing at the start of each preset
            update.max = max(val2.total, 1)
            update.value = val2.current
            pct = str(int(val2.current / val2.total * 100) if val2.total > 0 else 0)
            label = tr.deck_config_optimizing_preset(
                current_count=val2.current_preset, total_count=val2.total_presets
            )
            if val2.reviews:
                reviews = tr.deck_config_percent_of_reviews(
                    pct=pct, reviews=val2.reviews
                )
            else:
                reviews = tr.qt_misc_processing()

            update.label = label + "\n" + reviews
        else:
            return
        if update.user_wants_abort:
            update.abort = True

    def handle_on_main() -> None:
        update_deck_configs_op(parent=aqt.mw, input=input).success(
            lambda _: _on_update_deck_configs_success(input)
        ).with_backend_progress(on_progress).run_in_background()

    aqt.mw.taskman.run_on_main(handle_on_main)
    return b""


def get_scheduling_states_with_context() -> bytes:
    return SchedulingStatesWithContext(
        states=aqt.mw.reviewer.get_scheduling_states(),
        context=aqt.mw.reviewer.get_scheduling_context(),
    ).SerializeToString()


def set_scheduling_states() -> bytes:
    states = SetSchedulingStatesRequest()
    states.ParseFromString(request.data)
    aqt.mw.reviewer.set_scheduling_states(states)
    return b""


def import_done() -> bytes:
    def update_window_modality() -> None:
        if window := aqt.mw.app.activeModalWidget():
            from aqt.import_export.import_dialog import ImportDialog

            if isinstance(window, ImportDialog):
                window.hide()
                window.setWindowModality(Qt.WindowModality.NonModal)
                window.show()

    aqt.mw.taskman.run_on_main(update_window_modality)
    return b""


def import_dialog_require_close() -> bytes:
    def handle_on_main() -> None:
        if window := aqt.mw.app.activeModalWidget():
            from aqt.import_export.import_dialog import ImportDialog

            if isinstance(window, ImportDialog):
                window.reject()

    aqt.mw.taskman.run_on_main(handle_on_main)

    return b""


def search_in_browser() -> bytes:
    node = SearchNode()
    node.ParseFromString(request.data)

    def handle_on_main() -> None:
        aqt.dialogs.open("Browser", aqt.mw, search=(node,))

    aqt.mw.taskman.run_on_main(handle_on_main)

    return b""


def change_notetype() -> bytes:
    data = request.data

    def handle_on_main() -> None:
        window = aqt.mw.app.activeModalWidget()
        if isinstance(window, ChangeNotetypeDialog):
            window.save(data)

    aqt.mw.taskman.run_on_main(handle_on_main)
    return b""


def deck_options_require_close() -> bytes:
    def handle_on_main() -> None:
        window = aqt.mw.app.activeModalWidget()
        if isinstance(window, DeckOptionsDialog):
            window.require_close()

    # on certain linux systems, askUser's QMessageBox.question unsets the active window
    # so we wait for the next event loop before querying the next current active window
    aqt.mw.taskman.run_on_main(lambda: QTimer.singleShot(0, handle_on_main))
    return b""


def deck_options_ready() -> bytes:
    def handle_on_main() -> None:
        window = aqt.mw.app.activeModalWidget()
        if isinstance(window, DeckOptionsDialog):
            window.set_ready()

    aqt.mw.taskman.run_on_main(handle_on_main)
    return b""


def get_setting_json(getter: Callable[[str], Any]) -> bytes:
    req = generic_pb2.String()
    req.ParseFromString(request.data)
    value = getter(req.val)
    output = generic_pb2.Json(json=to_json_bytes(value)).SerializeToString()
    return output


def set_setting_json(setter: Callable[[str, Any], Any]) -> bytes:
    req = frontend_pb2.SetSettingJsonRequest()
    req.ParseFromString(request.data)
    setter(req.key, from_json_bytes(req.value_json))
    return b""


def get_profile_config_json() -> bytes:
    assert aqt.mw.pm.profile is not None
    return get_setting_json(aqt.mw.pm.profile.get)


def set_profile_config_json() -> bytes:
    assert aqt.mw.pm.profile is not None
    return set_setting_json(aqt.mw.pm.profile.__setitem__)


def get_meta_json() -> bytes:
    return get_setting_json(aqt.mw.pm.meta.get)


def set_meta_json() -> bytes:
    return set_setting_json(aqt.mw.pm.meta.__setitem__)


def get_config_json() -> bytes:
    try:
        return get_setting_json(aqt.mw.col.conf.get_immutable)
    except KeyError:
        return generic_pb2.Json(json=b"null").SerializeToString()


def set_config_json() -> bytes:
    return set_setting_json(aqt.mw.col.set_config)


def convert_pasted_image() -> bytes:
    req = frontend_pb2.ConvertPastedImageRequest()
    req.ParseFromString(request.data)
    image = QImage.fromData(req.data)
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.ReadWrite)
    if req.ext == "png":
        quality = 50
    else:
        quality = 80
    image.save(buffer, req.ext, quality)
    buffer.reset()
    data = bytes(cast(bytes, buffer.readAll()))
    return frontend_pb2.ConvertPastedImageResponse(data=data).SerializeToString()


AsyncRequestReturnType = TypeVar("AsyncRequestReturnType")


class AsyncRequestHandler(Generic[AsyncRequestReturnType]):
    def __init__(self, callback: Callable[[AsyncRequestHandler], None]) -> None:
        self.callback = callback
        self.loop = asyncio.get_running_loop()
        self.future = self.loop.create_future()

    def run(self) -> None:
        aqt.mw.taskman.run_on_main(lambda: self.callback(self))

    def set_result(self, result: AsyncRequestReturnType) -> None:
        self.loop.call_soon_threadsafe(self.future.set_result, result)

    async def get_result(self) -> AsyncRequestReturnType:
        return await self.future


def active_window_or_main() -> QWidget:
    return aqt.mw.app.activeWindow() or aqt.mw


async def open_file_picker() -> bytes:
    req = frontend_pb2.openFilePickerRequest()
    req.ParseFromString(request.data)

    def callback(request_handler: AsyncRequestHandler) -> None:
        from aqt.utils import getFile

        def cb(filename: str | None) -> None:
            request_handler.set_result(filename)

        window = active_window_or_main()
        getFile(
            parent=window,
            title=req.title,
            cb=cast(Callable[[Any], None], cb),
            filter=f"{req.filter_description} ({' '.join(f'*.{ext}' for ext in req.extensions)})",
            key=req.key,
        )

    request_handler: AsyncRequestHandler[str | None] = AsyncRequestHandler(callback)
    request_handler.run()
    filename = await request_handler.get_result()

    return generic_pb2.String(val=filename if filename else "").SerializeToString()


def open_media() -> bytes:
    from aqt.editor_legacy import pics
    from aqt.utils import openFolder

    req = generic_pb2.String()
    req.ParseFromString(request.data)
    path = os.path.join(aqt.mw.col.media.dir(), req.val)
    _, ext = os.path.splitext(path)
    if ext[1:] in pics:
        aqt.mw.taskman.run_on_main(lambda: openFolder(path))

    return b""


def show_in_media_folder() -> bytes:
    from aqt.utils import show_in_folder

    req = generic_pb2.String()
    req.ParseFromString(request.data)
    path = os.path.join(aqt.mw.col.media.dir(), req.val)
    aqt.mw.taskman.run_on_main(lambda: show_in_folder(path))

    return b""


async def record_audio() -> bytes:
    def callback(request_handler: AsyncRequestHandler) -> None:
        from aqt.sound import record_audio

        def cb(path: str | None) -> None:
            request_handler.set_result(path)

        window = active_window_or_main()
        record_audio(window, aqt.mw, True, cb)

    request_handler: AsyncRequestHandler[str | None] = AsyncRequestHandler(callback)
    request_handler.run()
    path = await request_handler.get_result()

    return generic_pb2.String(val=path if path else "").SerializeToString()


def play_file() -> bytes:
    from aqt.editor import NewEditor
    from aqt.sound import av_player

    req = generic_pb2.String()
    req.ParseFromString(request.data)
    path = os.path.join(aqt.mw.col.media.dir(), req.val)

    def handle_on_main() -> None:
        window = aqt.dialogs.activeWindow()
        if (
            window is not None
            and hasattr(window, "editor")
            and isinstance(window.editor, NewEditor)
        ):
            av_player.play_file_with_caller(path, window.editor.editorMode)
        else:
            av_player.play_file(path)

    aqt.mw.taskman.run_on_main(handle_on_main)

    return b""


def read_clipboard() -> bytes:
    req = frontend_pb2.ReadClipboardRequest()
    req.ParseFromString(request.data)
    data = {}
    clipboard = aqt.mw.app.clipboard()
    assert clipboard is not None
    mime_data = clipboard.mimeData(QClipboard.Mode.Clipboard)
    assert mime_data is not None
    for type in req.types:
        data[type] = bytes(mime_data.data(type))  # type: ignore

    return frontend_pb2.ReadClipboardResponse(data=data).SerializeToString()


def write_clipboard() -> bytes:
    req = frontend_pb2.WriteClipboardRequest()
    req.ParseFromString(request.data)
    clipboard = aqt.mw.app.clipboard()
    assert clipboard is not None
    mime_data = clipboard.mimeData(QClipboard.Mode.Clipboard)
    assert mime_data is not None
    for type, data in req.data.items():
        mime_data.setData(type, data)
    return b""


def close_add_cards() -> bytes:
    req = generic_pb2.Bool()
    req.ParseFromString(request.data)

    def handle_on_main() -> None:
        from aqt.addcards import NewAddCards

        if window := aqt.dialogs.getInstance(NewAddCards.__name__):
            window._close_if_user_wants_to_discard_changes(req.val)

    aqt.mw.taskman.run_on_main(lambda: QTimer.singleShot(0, handle_on_main))
    return b""


def close_edit_current() -> bytes:
    def handle_on_main() -> None:
        from aqt.editcurrent import NewEditCurrent

        if window := aqt.dialogs.getInstance(NewEditCurrent.__name__):
            window.close()

    aqt.mw.taskman.run_on_main(lambda: QTimer.singleShot(0, handle_on_main))
    return b""


def open_link() -> bytes:
    req = generic_pb2.String()
    req.ParseFromString(request.data)
    url = req.val
    aqt.mw.taskman.run_on_main(lambda: openLink(url))
    return b""


async def ask_user() -> bytes:
    req = frontend_pb2.AskUserRequest()
    req.ParseFromString(request.data)

    def callback(request_handler: AsyncRequestHandler) -> None:
        kwargs: dict[str, Any] = dict(text=req.text)
        if req.HasField("help"):
            help_arg: Any
            if req.help.WhichOneof("value") == "help_page":
                help_arg = req.help.help_page
            else:
                help_arg = req.help.help_link
            kwargs["help"] = help_arg
        if req.HasField("title"):
            kwargs["title"] = req.title
        if req.HasField("default_no"):
            kwargs["defaultno"] = req.default_no
        answer = askUser(**kwargs)
        request_handler.set_result(answer)

    request_handler: AsyncRequestHandler[bool] = AsyncRequestHandler(callback)
    request_handler.run()
    answer = await request_handler.get_result()

    return generic_pb2.Bool(val=answer).SerializeToString()


async def show_message_box() -> bytes:
    req = frontend_pb2.ShowMessageBoxRequest()
    req.ParseFromString(request.data)

    def callback(request_handler: AsyncRequestHandler) -> None:
        kwargs: dict[str, Any] = dict(text=req.text)
        if req.type == frontend_pb2.MessageBoxType.INFO:
            icon = QMessageBox.Icon.Information
        elif req.type == frontend_pb2.MessageBoxType.WARNING:
            icon = QMessageBox.Icon.Warning
        elif req.type == frontend_pb2.MessageBoxType.CRITICAL:
            icon = QMessageBox.Icon.Critical
        kwargs["icon"] = icon
        if req.HasField("help"):
            help_arg: Any
            if req.help.WhichOneof("value") == "help_page":
                help_arg = req.help.help_page
            else:
                help_arg = req.help.help_link
            kwargs["help"] = help_arg
        if req.HasField("title"):
            kwargs["title"] = req.title
        if req.HasField("text_format"):
            kwargs["text_format"] = req.text_format
        show_info(**kwargs)
        request_handler.set_result(True)

    request_handler: AsyncRequestHandler[bool] = AsyncRequestHandler(callback)
    request_handler.run()
    answer = await request_handler.get_result()

    return generic_pb2.Bool(val=answer).SerializeToString()


def open_fields_dialog() -> bytes:
    def handle_on_main() -> None:
        from aqt.editor import NewEditor

        window = aqt.dialogs.activeWindow()
        assert window is not None
        if hasattr(window, "editor") and isinstance(window.editor, NewEditor):
            window.editor.onFields()

    aqt.mw.taskman.run_on_main(handle_on_main)
    return b""


def open_cards_dialog() -> bytes:
    def handle_on_main() -> None:
        from aqt.editor import NewEditor

        window = aqt.dialogs.activeWindow()
        assert window is not None
        if hasattr(window, "editor") and isinstance(window.editor, NewEditor):
            window.editor.onCardLayout()

    aqt.mw.taskman.run_on_main(handle_on_main)
    return b""


def save_custom_colours() -> bytes:
    colors = [
        QColorDialog.customColor(i).name(QColor.NameFormat.HexRgb)
        for i in range(QColorDialog.customCount())
    ]
    aqt.mw.col.set_config("customColorPickerPalette", colors)
    return b""


post_handler_list = [
    congrats_info,
    get_deck_configs_for_update,
    update_deck_configs,
    get_scheduling_states_with_context,
    set_scheduling_states,
    change_notetype,
    import_done,
    import_dialog_require_close,
    search_in_browser,
    deck_options_require_close,
    deck_options_ready,
    get_profile_config_json,
    set_profile_config_json,
    get_meta_json,
    set_meta_json,
    get_config_json,
    convert_pasted_image,
    open_file_picker,
    open_media,
    show_in_media_folder,
    record_audio,
    play_file,
    read_clipboard,
    write_clipboard,
    close_add_cards,
    close_edit_current,
    open_link,
    ask_user,
    show_message_box,
    open_fields_dialog,
    open_cards_dialog,
    save_custom_colours,
]


exposed_backend_list = [
    # CollectionService
    "latest_progress",
    "get_custom_colours",
    # DeckService
    "get_deck_names",
    "get_deck",
    # I18nService
    "i18n_resources",
    # ImportExportService
    "get_csv_metadata",
    "get_import_anki_package_presets",
    "import_csv",
    "import_anki_package",
    "import_json_file",
    "import_json_string",
    # NotesService
    "get_field_names",
    "get_note",
    "new_note",
    "note_fields_check",
    "defaults_for_adding",
    "default_deck_for_notetype",
    "add_note",
    "update_notes",
    "update_notetype",
    # NotetypesService
    "get_notetype",
    "get_notetype_names",
    "get_change_notetype_info",
    "get_cloze_field_ords",
    # StatsService
    "card_stats",
    "get_review_logs",
    "graphs",
    "get_graph_preferences",
    "set_graph_preferences",
    # TagsService
    "complete_tag",
    # ImageOcclusionService
    "get_image_for_occlusion",
    "add_image_occlusion_note",
    "get_image_occlusion_note",
    "update_image_occlusion_note",
    "get_image_occlusion_fields",
    # SchedulerService
    "compute_fsrs_params",
    "compute_optimal_retention",
    "set_wants_abort",
    "evaluate_params_legacy",
    "get_optimal_retention_parameters",
    "simulate_fsrs_review",
    "simulate_fsrs_workload",
    # DeckConfigService
    "get_ignored_before_count",
    "get_retention_workload",
    # CardRenderingService
    "encode_iri_paths",
    "decode_iri_paths",
    "html_to_text_line",
    # ConfigService
    "set_config_json",
    "get_config_bool",
    # MediaService
    "add_media_file",
    "add_media_from_path",
    "add_media_from_url",
    "get_absolute_media_path",
    "extract_media_files",
    # CardsService
    "get_card",
]


def raw_backend_request(endpoint: str) -> Callable[[], bytes]:
    # check for key at startup
    from anki._backend import RustBackend

    assert hasattr(RustBackend, f"{endpoint}_raw")

    def wrapped() -> bytes:
        output = getattr(aqt.mw.col._backend, f"{endpoint}_raw")(request.data)
        op_changes_type = int(request.headers.get("Anki-Op-Changes", "0"))
        if op_changes_type:
            op_message_types = (OpChanges, OpChangesOnly, NestedOpChanges)
            try:
                response = op_message_types[op_changes_type - 1]()
                response.ParseFromString(output)
                changes: Any = response
                for _ in range(op_changes_type - 1):
                    changes = changes.changes
            except IndexError:
                raise ValueError(f"unhandled op changes level: {op_changes_type}")

            def handle_on_main() -> None:
                handler = active_window_or_main()
                on_op_finished(aqt.mw, changes, handler)

            aqt.mw.taskman.run_on_main(handle_on_main)

        return output

    return wrapped


# all methods in here require a collection
post_handlers = {
    stringcase.camelcase(handler.__name__): handler for handler in post_handler_list
} | {
    stringcase.camelcase(handler): raw_backend_request(handler)
    for handler in exposed_backend_list
}


def _extract_collection_post_request(path: str) -> DynamicRequest | NotFound:
    if not aqt.mw.col:
        return NotFound(message=f"collection not open, ignore request for {path}")
    if handler := post_handlers.get(path):
        # convert bytes/None into response
        def wrapped() -> Response:
            try:
                if inspect.iscoroutinefunction(handler):
                    data = asyncio.run(handler())
                else:
                    data = handler()
                if data:
                    response = Response(
                        data, headers={"Content-Type": "application/binary"}
                    )
                else:
                    response = _text_response(HTTPStatus.NO_CONTENT, "")
            except Exception as exc:
                print(traceback.format_exc())
                response = _text_response(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return response

        return wrapped
    else:
        return NotFound(message=f"{path} not found")


def _check_dynamic_request_permissions() -> None:
    if request.method == "GET":
        return

    def warn() -> None:
        show_warning(
            "Unexpected API access. Please report this message on the Anki forums."
        )

    # check content type header to ensure this isn't an opaque request from another origin
    if request.headers.get("Content-type") != "application/binary":
        aqt.mw.taskman.run_on_main(warn)
        abort(HTTPStatus.FORBIDDEN)

    # does page have access to entire API?
    if _have_api_access():
        return

    # whitelisted API endpoints for reviewer/previewer
    if request.path in (
        "/_anki/getSchedulingStatesWithContext",
        "/_anki/setSchedulingStates",
        "/_anki/i18nResources",
        "/_anki/congratsInfo",
    ):
        pass
    else:
        # other legacy pages may contain third-party JS, so we do not
        # allow them to access our API
        aqt.mw.taskman.run_on_main(warn)
        abort(HTTPStatus.FORBIDDEN)


def _handle_dynamic_request(req: DynamicRequest) -> Response:
    _check_dynamic_request_permissions()
    try:
        return req()
    except Exception as e:
        return _text_response(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))


_APIKEY = secrets.token_urlsafe(32)


def _have_api_access() -> bool:
    return (
        request.headers.get("Authorization") == f"Bearer {_APIKEY}"
        or os.environ.get("ANKI_API_HOST") == "0.0.0.0"
    )


class _PythonHandler:
    """Called by mediasrv.rs on its own threads, not the main thread."""

    def handle(
        self,
        method: str,
        path: str,
        query: str,
        headers: list[tuple[str, str]],
        body: bytes,
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        _current.request = Request(
            method=method,
            path=f"/{path}",
            args=dict(urllib.parse.parse_qsl(query)),
            headers=Headers(headers),
            data=body,
        )
        try:
            response = self._dispatch(path)
        except HTTPError as exc:
            status = HTTPStatus(exc.status)
            response = _text_response(status, status.phrase)
        except Exception as exc:
            print(traceback.format_exc())
            response = _text_response(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        finally:
            _current.request = None
        return int(response.status), list(response.headers.items()), response.body()

    def _dispatch(self, path: str) -> Response:
        logger.debug("%s /%s", request.method, path)
        if path == "_anki/readyz":
            return readyz()
        if path.startswith("_anki/") and request.method == "POST":
            req = _extract_collection_post_request(path[len("_anki/") :])
            if isinstance(req, NotFound):
                print(req.message)
                return _text_response(HTTPStatus.NOT_FOUND, f"Invalid path: {path}")
            return _handle_dynamic_request(req)
        if _flask_app is not None:
            return _call_flask_app(path)
        return _not_handled()

    def filter_media_path(self, path: str) -> str:
        return hooks.media_file_filter(path)

    def match_web_export(self, addon: str, sub_path: str) -> bool:
        pattern = aqt.mw.addonManager.getWebExports(addon)
        return bool(pattern and re.fullmatch(pattern, sub_path))


def _not_handled() -> Response:
    # mediasrv.rs serves the path as a media file instead
    return Response(status=HTTPStatus.NOT_FOUND, headers={"X-Anki-Not-Handled": "1"})


# Add-ons can add routes to a Flask app here, as they could when the server used
# Flask. Flask is only imported when an add-on uses it.

_flask_app: Any = None


def _get_flask_app() -> Any:
    global _flask_app
    if _flask_app is None:
        import flask

        _flask_app = flask.Flask(__name__, root_path="/fake")
        mw = getattr(aqt, "mw", None)
        if mw and getattr(mw, "mediaServer", None) and mw.mediaServer._server:
            mw.mediaServer._server.set_python_routes(True)
    return _flask_app


def _call_flask_app(path: str) -> Response:
    from werkzeug.exceptions import MethodNotAllowed
    from werkzeug.exceptions import NotFound as WerkzeugNotFound
    from werkzeug.test import EnvironBuilder

    adapter = _flask_app.url_map.bind("localhost")
    try:
        adapter.match(f"/{path}", method=request.method)
    except (WerkzeugNotFound, MethodNotAllowed):
        return _not_handled()
    environ = EnvironBuilder(
        path=f"/{path}",
        method=request.method,
        query_string=urllib.parse.urlencode(request.args),
        headers=list(request.headers._items.items()),
        data=request.data,
    ).get_environ()
    status_and_headers: list[Any] = []

    def start_response(status: str, headers: list, exc_info: Any = None) -> None:
        status_and_headers[:] = [int(status.split(maxsplit=1)[0]), dict(headers)]

    body = b"".join(_flask_app(environ, start_response))
    return Response(body, status_and_headers[0], status_and_headers[1])


if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        if name == "app":
            return _get_flask_app()
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
