# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Creates QtWebEngine profiles. This module imports no other aqt modules, so
that startup can create the first profile before it imports the rest of aqt."""

from __future__ import annotations

from typing import cast

from aqt.qt import QWebEngineProfile, sip

_early_profile: QWebEngineProfile | None = None


def new_profile() -> QWebEngineProfile:
    """PyQt holds the GIL while QWebEngineProfile() runs, which takes hundreds of
    milliseconds the first time. The helper in rsbridge releases it, so that
    other threads can run meanwhile."""
    try:
        from anki._rsbridge import new_webengine_profile

        address = new_webengine_profile()
    except Exception as exc:
        print(f"unable to create a web profile with the GIL released: {exc}")
        return QWebEngineProfile()
    profile = cast(
        QWebEngineProfile,
        sip.wrapinstance(address, cast(sip.wrappertype, QWebEngineProfile)),
    )
    # owned by Python, as when it is created with QWebEngineProfile()
    sip.transferback(cast(sip.wrapper, profile))
    return profile


def create_early_profile() -> None:
    """Create a profile for aqt.webview to use first. QtWebEngine continues to
    initialize in the background after the first profile is created."""
    global _early_profile
    if _early_profile is None:
        _early_profile = new_profile()


def take_profile() -> QWebEngineProfile:
    """The early profile if there is one, else a new profile."""
    global _early_profile
    profile, _early_profile = _early_profile, None
    return profile or new_profile()
