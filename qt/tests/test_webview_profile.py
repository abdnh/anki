# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from aqt.qt import QWebEngineProfile, sip
from aqt.webview import _new_profile


def test_new_profile_is_a_python_owned_off_the_record_profile(qapp) -> None:
    profile = _new_profile()

    assert isinstance(profile, QWebEngineProfile)
    assert sip.ispyowned(profile)
    assert profile.isOffTheRecord()
