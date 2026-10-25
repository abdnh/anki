# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from aqt import webengine_profile
from aqt.qt import QWebEngineProfile, sip


def test_new_profile_is_a_python_owned_off_the_record_profile(qapp) -> None:
    profile = webengine_profile.new_profile()

    assert isinstance(profile, QWebEngineProfile)
    assert sip.ispyowned(profile)
    assert profile.isOffTheRecord()


def test_take_profile_returns_early_profile_only_once(qapp) -> None:
    webengine_profile.create_early_profile()
    early = webengine_profile._early_profile

    first = webengine_profile.take_profile()
    second = webengine_profile.take_profile()

    assert first is early
    assert second is not early
