# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aqt import webview_shell


@pytest.mark.parametrize(
    "env,meta,expected",
    [
        (None, {}, False),
        (None, {webview_shell.PROFILE_KEY: True}, True),
        ("1", {}, True),
        ("0", {webview_shell.PROFILE_KEY: True}, False),
    ],
)
def test_enabled_uses_env_var_before_profile_setting(
    monkeypatch, env: str | None, meta: dict, expected: bool
) -> None:
    if env is None:
        monkeypatch.delenv(webview_shell.ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(webview_shell.ENV_VAR, env)

    assert webview_shell.enabled(SimpleNamespace(meta=meta)) is expected


def test_frame_html_makes_links_open_top_level() -> None:
    html = webview_shell._frame_html("<html><head><title>t</title></head></html>")

    assert html.startswith('<html><head><base target="_top"><title>')
