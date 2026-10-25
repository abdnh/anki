# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import pytest

import aqt
from aqt import _DialogRegistry, _LazyDialogCreator


def test_dialog_registry_replaces_lazy_creator_when_entry_is_read() -> None:
    registry = _DialogRegistry(
        {"About": [_LazyDialogCreator("aqt.about", "show"), None]}
    )

    creator = registry["About"][0]

    import aqt.about

    assert creator is aqt.about.show
    assert dict.__getitem__(registry, "About")[0] is aqt.about.show


def test_dialog_registry_keeps_creator_set_by_add_on() -> None:
    def custom_creator() -> None:
        pass

    registry = _DialogRegistry(
        {"About": [_LazyDialogCreator("aqt.about", "show"), None]}
    )
    registry["About"][0] = custom_creator

    assert registry["About"][0] is custom_creator


def test_dialog_manager_lists_builtin_dialogs() -> None:
    assert set(aqt.dialogs._dialogs) >= {"AddCards", "Browser", "Preferences"}


def test_aqt_submodule_attribute_is_imported_on_first_use() -> None:
    assert aqt.editcurrent.EditCurrent.__name__ == "EditCurrent"


def test_aqt_unknown_attribute_raises_attribute_error() -> None:
    with pytest.raises(AttributeError):
        aqt.no_such_module  # noqa: B018
