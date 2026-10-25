# Forms are imported on first use, to make startup faster.

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

_FORMS = (
    "about",
    "addcards",
    "addfield",
    "addmodel",
    "addonconf",
    "addons",
    "browser",
    "browserdisp",
    "browseropts",
    "changemodel",
    "clayout_top",
    "customstudy",
    "dconf",
    "debug",
    "editcurrent",
    "edithtml",
    "emptycards",
    "exporting",
    "fields",
    "filtered_deck",
    "finddupes",
    "findreplace",
    "forget",
    "getaddons",
    "main",
    "modelopts",
    "models",
    "preferences",
    "preview",
    "profiles",
    "progress",
    "reposition",
    "setgroup",
    "setlang",
    "stats",
    "studydeck",
    "synclog",
    "taglimit",
    "template",
    "widgets",
)

if TYPE_CHECKING:
    # ruff: noqa: F401
    from . import (
        about,
        addcards,
        addfield,
        addmodel,
        addonconf,
        addons,
        browser,
        browserdisp,
        browseropts,
        changemodel,
        clayout_top,
        customstudy,
        dconf,
        debug,
        editcurrent,
        edithtml,
        emptycards,
        exporting,
        fields,
        filtered_deck,
        finddupes,
        findreplace,
        forget,
        getaddons,
        main,
        modelopts,
        models,
        preferences,
        preview,
        profiles,
        progress,
        reposition,
        setgroup,
        setlang,
        stats,
        studydeck,
        synclog,
        taglimit,
        template,
        widgets,
    )


def __getattr__(name: str) -> Any:
    if name in _FORMS:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
