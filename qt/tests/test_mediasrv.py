# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Tests for the media server (aqt.mediasrv and mediasrv.rs in rsbridge)."""

from __future__ import annotations

import http.client
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from aqt.mediasrv import (
    TRUSTED_PAGE_CSP,
    UNTRUSTED_MEDIA_CSP,
    PageContext,
    UnsafePathException,
    _legacy_editor_content_security_policy,
    _PythonHandler,
    _untrusted_sveltekit_content_security_policy,
    ensure_safe_path,
    is_localhost_origin,
)


class TestEnsureSafePath:
    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        subdir = Path(self.tmpdir) / "sub"
        subdir.mkdir()
        (subdir / "file.txt").write_text("ok")

    def test_valid_subpath(self) -> None:
        result = ensure_safe_path(self.tmpdir, "sub/file.txt")
        assert result == os.path.join(os.path.realpath(self.tmpdir), "sub", "file.txt")

    def test_rejects_parent_traversal(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "../etc/passwd")

    def test_rejects_double_traversal(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "sub/../../etc/passwd")

    def test_rejects_absolute_path_escape(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "/etc/passwd")

    def test_rejects_base_dir_itself(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, ".")

    def test_rejects_empty_path(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "")

    def test_accepts_pathlib_args(self) -> None:
        result = ensure_safe_path(Path(self.tmpdir), Path("sub/file.txt"))
        assert result.endswith(os.path.join("sub", "file.txt"))

    def test_normalizes_redundant_separators(self) -> None:
        result = ensure_safe_path(self.tmpdir, "sub///file.txt")
        assert result == os.path.join(os.path.realpath(self.tmpdir), "sub", "file.txt")

    def test_rejects_traversal_after_normalization(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "sub/../../../etc/passwd")


class TestIsLocalhostOrigin:
    @pytest.mark.parametrize(
        "origin",
        [
            "http://127.0.0.1:40000",
            "http://localhost:40000",
            "http://[::1]:40000",
            "https://127.0.0.1:40000",
            "https://localhost:40000",
            "https://[::1]:40000",
            "http://127.0.0.1",
            "http://localhost",
            "http://[::1]",
            "http://127.0.0.1/",
            "http://localhost/path",
        ],
    )
    def test_allowed_origins(self, origin: str) -> None:
        assert is_localhost_origin(origin) is True

    @pytest.mark.parametrize(
        "origin",
        [
            "http://evil.com",
            "http://127.0.0.1.evil.com",
            "http://localhost.evil.com",
            "http://evil.com:127.0.0.1",
            "http://notlocalhost:40000",
            "https://evil.com",
            "",
        ],
    )
    def test_rejected_origins(self, origin: str) -> None:
        assert is_localhost_origin(origin) is False


def _csp_directives(csp: str) -> dict[str, str]:
    directives = {}
    for part in csp.split(";"):
        name, _, value = part.strip().partition(" ")
        directives[name] = value
    return directives


class TestEditorPageCSP:
    def test_editor_csp_does_not_block_user_embeds(self) -> None:
        csp = _legacy_editor_content_security_policy(port=12345)
        directives = _csp_directives(csp)

        assert directives["script-src"] == (
            "http://127.0.0.1:12345/_anki/ http://127.0.0.1:12345/_addons/"
        )
        assert "object-src" not in directives
        assert "frame-src" not in directives
        assert "child-src" not in directives
        assert "img-src" not in directives

    @pytest.mark.parametrize(
        "csp",
        [
            _legacy_editor_content_security_policy(port=12345),
            _untrusted_sveltekit_content_security_policy(12345, "'sha256-abc='"),
        ],
    )
    def test_editor_csp_blocks_navigation_and_framing(self, csp: str) -> None:
        """Field content must not navigate the editor webview via a form, and
        the editor itself must not be framed by other pages."""
        directives = _csp_directives(csp)
        assert directives["form-action"] == "'none'"
        assert directives["frame-ancestors"] == "'none'"

    def test_sveltekit_editor_csp_allows_render_script_hash(self) -> None:
        csp = _untrusted_sveltekit_content_security_policy(12345, "'sha256-abc='")
        directives = _csp_directives(csp)
        assert directives["script-src"].split() == [
            "http://127.0.0.1:12345/_anki/",
            "http://127.0.0.1:12345/_app/",
            "http://127.0.0.1:12345/_addons/",
            "'sha256-abc='",
        ]

    def test_untrusted_media_csp_allows_only_passive_local_resources(self) -> None:
        """Media embedded via <object>/<iframe> is a document of its own, and has to
        be able to load the stylesheets/images/fonts stored next to it."""
        directives = _csp_directives(UNTRUSTED_MEDIA_CSP)

        assert directives["style-src"] == "'self' 'unsafe-inline'"
        assert directives["img-src"] == "'self'"
        assert directives["font-src"] == "'self'"
        assert directives["media-src"] == "'self'"
        assert directives["sandbox"] == "allow-same-origin"
        assert "frame-ancestors" not in directives
        # ...but only passive resources, and only from our own server, so that
        # cards can neither execute code nor phone home
        for directive in (
            "default-src",
            "script-src",
            "connect-src",
            "object-src",
            "frame-src",
            "child-src",
            "base-uri",
            "form-action",
        ):
            assert directives[directive] == "'none'"
        for name, value in directives.items():
            assert not any(
                remote in value for remote in ("http:", "https:", "data:", "*")
            ), f"{name} must not allow remote sources: {value}"


# The server in rsbridge
##########################################################################

SVELTEKIT_INDEX = (
    b'<html><head><meta http-equiv="content-security-policy" '
    b"content=\"script-src 'self' 'sha256-abc='\"></head></html>"
)


@dataclass
class Server:
    server: Any
    port: int
    media: Path

    def fetch(
        self,
        path: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            data = response.read()
            headers = {k.lower(): v for k, v in response.getheaders()}
            return response.status, headers, data
        finally:
            conn.close()


def _write(path: Path, data: bytes = b"test") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _start_server(tmp_path: Path, configure_csp: bool = True, **kwargs: Any) -> Server:
    from anki._rsbridge import MediaServer as RustMediaServer

    web = tmp_path / "web"
    _write(web / "js" / "foo.js", b"console.log(1)")
    _write(web / "js" / "vendor" / "jquery.min.js", b"jquery")
    _write(web / "js" / "vendor" / "plot.js", b"plot")
    _write(web / "pages" / "page.html", b"<html></html>")
    _write(web / "sveltekit" / "index.html", SVELTEKIT_INDEX)
    _write(web / "sveltekit" / "_app" / "immutable" / "x.js", b"immutable")
    addons = tmp_path / "addons"
    _write(addons / "myaddon" / "web" / "a.js", b"addon js")
    _write(addons / "myaddon" / "secret.txt", b"secret")
    media = tmp_path / "media"
    _write(media / "card.html", b"<html></html>")
    _write(media / "image.svg", b"<svg></svg>")
    _write(media / "audio.mp3", b"0123456789")
    (media / "folder").mkdir()
    _write(tmp_path / "outside.txt", b"outside")

    server = RustMediaServer(
        "127.0.0.1", 0, str(web), str(addons), _PythonHandler(), **kwargs
    )
    port = server.port
    if configure_csp:
        server.set_csp(
            TRUSTED_PAGE_CSP,
            UNTRUSTED_MEDIA_CSP,
            _legacy_editor_content_security_policy(port),
            _untrusted_sveltekit_content_security_policy(port, "{hash}"),
        )
    server.set_media_dir(str(media))
    return Server(server, port, media)


@pytest.fixture
def mw(monkeypatch) -> mock.Mock:
    import aqt

    mw = mock.Mock()
    monkeypatch.setattr(aqt, "mw", mw, raising=False)
    return mw


@pytest.fixture
def server(tmp_path: Path, mw: mock.Mock) -> Iterator[Server]:
    server = _start_server(tmp_path)
    yield server
    server.server.shutdown()


class TestMediaFiles:
    @pytest.mark.parametrize("name", ["card.html", "image.svg"])
    def test_media_document_is_sandboxed(self, server: Server, name: str) -> None:
        status, headers, _ = server.fetch(f"/{name}")

        assert status == 200
        assert headers["content-security-policy"] == UNTRUSTED_MEDIA_CSP

    def test_media_file_supports_range_requests(self, server: Server) -> None:
        status, headers, data = server.fetch(
            "/audio.mp3", headers={"Range": "bytes=2-4"}
        )

        assert status == 206
        assert data == b"234"
        assert headers["content-type"] == "audio/mpeg"

    @pytest.mark.parametrize(
        "path", ["/../outside.txt", "/..%2Foutside.txt", "/%2e%2e/outside.txt"]
    )
    def test_media_path_cannot_escape_media_folder(
        self, server: Server, path: str
    ) -> None:
        status, _, data = server.fetch(path)

        assert status in (403, 404)
        assert data != b"outside"

    def test_media_folder_listing_is_refused(self, server: Server) -> None:
        status, _, _ = server.fetch("/folder")

        assert status == 403

    def test_media_is_not_served_when_collection_closed(self, server: Server) -> None:
        server.server.set_media_dir(None)

        status, _, _ = server.fetch("/card.html")

        assert status == 404

    def test_media_file_filter_rewrites_path(self, server: Server, monkeypatch) -> None:
        monkeypatch.setattr(
            _PythonHandler, "filter_media_path", lambda self, path: "audio.mp3"
        )
        server.server.set_filter_media_paths(True)

        status, _, data = server.fetch("/missing.mp3")

        assert (status, data) == (200, b"0123456789")


class TestAddonFiles:
    def test_exported_addon_file_is_not_sandboxed(self, server: Server) -> None:
        server.server.set_web_exports("myaddon", r"web/.*\.js")

        status, headers, data = server.fetch("/_addons/myaddon/web/a.js")

        assert (status, data) == (200, b"addon js")
        assert "content-security-policy" not in headers

    def test_unexported_addon_file_is_not_served(self, server: Server) -> None:
        server.server.set_web_exports("myaddon", r"web/.*\.js")

        status, _, data = server.fetch("/_addons/myaddon/secret.txt")

        assert status == 404
        assert data != b"secret"

    def test_python_only_pattern_is_matched_in_python(
        self, server: Server, mw: mock.Mock
    ) -> None:
        pattern = r"(?=web/)web/.*\.js"
        mw.addonManager.getWebExports.return_value = pattern

        assert server.server.set_web_exports("myaddon", pattern) is False
        status, _, data = server.fetch("/_addons/myaddon/web/a.js")

        assert (status, data) == (200, b"addon js")


class TestBundledFiles:
    def test_bundled_asset_has_no_csp(self, server: Server) -> None:
        status, headers, _ = server.fetch("/_anki/js/foo.js")

        assert status == 200
        assert headers["content-type"] == "application/javascript"
        assert "content-security-policy" not in headers

    def test_bundled_html_page_refuses_framing(self, server: Server) -> None:
        status, headers, _ = server.fetch("/_anki/pages/page.html")

        assert status == 200
        assert headers["content-security-policy"] == TRUSTED_PAGE_CSP

    @pytest.mark.parametrize(
        "path,data",
        [("/_anki/js/vendor/jquery.js", b"jquery"), ("/_anki/plot.js", b"plot")],
    )
    def test_legacy_vendor_paths_are_remapped(
        self, server: Server, path: str, data: bytes
    ) -> None:
        status, _, body = server.fetch(path)

        assert (status, body) == (200, data)

    def test_trusted_sveltekit_page_refuses_framing(self, server: Server) -> None:
        status, headers, data = server.fetch("/deck-options/1")

        assert status == 200
        assert headers["content-security-policy"] == TRUSTED_PAGE_CSP
        assert b"content-security-policy" not in data

    @pytest.mark.parametrize("route", ["editor", "image-occlusion"])
    def test_untrusted_sveltekit_page_refuses_framing(
        self, server: Server, route: str
    ) -> None:
        status, headers, _ = server.fetch(f"/{route}/")

        assert status == 200
        directives = _csp_directives(headers["content-security-policy"])
        assert directives["form-action"] == "'none'"
        assert directives["frame-ancestors"] == "'none'"
        assert directives["script-src"].split()[-1] == "'sha256-abc='"

    def test_immutable_sveltekit_files_are_cached(self, server: Server) -> None:
        status, headers, data = server.fetch("/_app/immutable/x.js")

        assert (status, data) == (200, b"immutable")
        assert headers["cache-control"] == "max-age=31536000"

    def test_bundled_path_cannot_escape_web_folder(self, server: Server) -> None:
        status, _, data = server.fetch("/_anki/..%2F..%2Foutside.txt")

        assert status in (403, 404)
        assert data != b"outside"


class TestLegacyPages:
    @pytest.mark.parametrize(
        "context", [PageContext.REVIEWER, PageContext.PREVIEWER, PageContext.UNKNOWN]
    )
    def test_legacy_page_refuses_framing(self, server: Server, context) -> None:
        server.server.set_page(1, "<html>page</html>", int(context))

        status, headers, data = server.fetch("/_anki/legacyPageData?id=1")

        assert (status, data) == (200, b"<html>page</html>")
        assert headers["content-security-policy"] == TRUSTED_PAGE_CSP

    def test_legacy_editor_page_gets_editor_csp(self, server: Server) -> None:
        server.server.set_page(1, "<html></html>", int(PageContext.EDITOR))

        _, headers, _ = server.fetch("/_anki/legacyPageData?id=1")

        assert headers[
            "content-security-policy"
        ] == _legacy_editor_content_security_policy(server.port)

    def test_cleared_page_is_not_found(self, server: Server) -> None:
        server.server.set_page(1, "<html></html>", int(PageContext.REVIEWER))
        server.server.clear_page(1)

        status, _, _ = server.fetch("/_anki/legacyPageData?id=1")

        assert status == 404


class TestRequestChecks:
    def test_nothing_is_served_before_csp_is_configured(
        self, tmp_path: Path, mw: mock.Mock
    ) -> None:
        server = _start_server(tmp_path, configure_csp=False)
        try:
            status, _, _ = server.fetch("/_anki/pages/page.html")
        finally:
            server.server.shutdown()

        assert status == 503

    def test_non_local_host_is_refused(self, server: Server) -> None:
        status, _, _ = server.fetch("/card.html", headers={"Host": "evil.com"})

        assert status == 403

    def test_non_local_origin_is_refused(self, server: Server) -> None:
        status, _, _ = server.fetch("/card.html", headers={"Origin": "http://evil.com"})

        assert status == 403

    def test_any_host_is_allowed_when_configured(
        self, tmp_path: Path, mw: mock.Mock
    ) -> None:
        server = _start_server(tmp_path, allow_any_host=True)
        try:
            status, _, _ = server.fetch("/card.html", headers={"Host": "evil.com"})
        finally:
            server.server.shutdown()

        assert status == 200

    def test_api_request_without_binary_content_type_is_refused(
        self, server: Server
    ) -> None:
        status, _, _ = server.fetch("/_anki/getDeck", method="POST", body=b"")

        assert status == 403

    def test_api_request_from_page_without_api_access_is_refused(
        self, server: Server
    ) -> None:
        status, _, _ = server.fetch(
            "/_anki/getDeck",
            method="POST",
            headers={"Content-Type": "application/binary"},
            body=b"",
        )

        assert status == 403

    def test_api_request_with_key_reaches_handler(
        self, server: Server, monkeypatch
    ) -> None:
        from aqt import mediasrv

        monkeypatch.setitem(mediasrv.post_handlers, "testHandler", lambda: b"ok")

        status, headers, data = server.fetch(
            "/_anki/testHandler",
            method="POST",
            headers={
                "Content-Type": "application/binary",
                "Authorization": f"Bearer {mediasrv._APIKEY}",
            },
            body=b"",
        )

        assert (status, data) == (200, b"ok")
        assert headers["content-type"] == "application/binary"

    def test_readyz_reports_collection_state(
        self, server: Server, mw: mock.Mock
    ) -> None:
        mw.col = None
        assert server.fetch("/_anki/readyz")[0] == 503
        mw.col = object()
        assert server.fetch("/_anki/readyz")[0] == 200


class TestFlaskRoutes:
    def test_addon_flask_route_is_served_and_media_still_works(
        self, server: Server, monkeypatch
    ) -> None:
        from aqt import mediasrv

        monkeypatch.setattr(mediasrv, "_flask_app", None)
        app = mediasrv.app

        @app.route("/myaddon/hello")
        def hello() -> str:
            return "hello"

        server.server.set_python_routes(True)

        assert server.fetch("/myaddon/hello")[2] == b"hello"
        assert server.fetch("/audio.mp3")[2] == b"0123456789"
