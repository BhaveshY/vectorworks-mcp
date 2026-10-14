"""Did-you-mean help for names the model got slightly wrong (classes, symbols, layers)."""

from __future__ import annotations

import re
from typing import Iterable

from .builders import did_you_mean
from .errors import VWError

_SYMBOL_MISSING = re.compile(r"symbol definition was not found: (.+)$", re.I | re.M)


def _names(data, *keys: str) -> list[str]:
    items = data
    if isinstance(data, dict):
        items = next((data[k] for k in keys + ("items",) if isinstance(data.get(k), list)), [])
    out = []
    for item in items or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name:
            out.append(name)
    return out


def class_names(host, target) -> list[str]:
    return _names(host.call(target, "manage_classes", {"action": "list"}), "classes")


def warn_new_classes(host, target, wanted: Iterable[str], warnings: list) -> None:
    """Vectorworks creates a class on first use; a typo silently makes a new class. Warn with close names."""
    wanted = sorted({w for w in wanted if w})
    if not wanted:
        return
    try:
        existing = class_names(host, target)
    except VWError:
        return
    have = set(existing)
    for name in wanted:
        if name not in have:
            close = did_you_mean(name, existing)
            warnings.append(f"Class {name!r} did not exist: Vectorworks creates it" +
                            (f" (did you mean {', '.join(repr(c) for c in close)}?)" if close else "") + ".")


def enrich_symbol_error(host, target, exc: VWError) -> None:
    """Native 'unique symbol definition was not found: X' -> not_found with up to 5 close symbol names."""
    match = _SYMBOL_MISSING.search(exc.message or "")
    if not match:
        return
    wanted = match.group(1).strip().strip("'\"")
    try:
        names = _names(host.call(target, "symbol", {"action": "list", "query": ""}), "symbols")
    except VWError:
        names = []
    close = did_you_mean(wanted, names)
    exc.code = "not_found"
    exc.message = (f"No symbol definition named {wanted!r} in this document" +
                   (f"; did you mean {', '.join(repr(c) for c in close)}?" if close else "") +
                   " (vw_read(what='symbols', search=...) lists them; import from a file with "
                   "vw_organize(kind='resource', action='import')).")
    exc.next_step = "Use an existing symbol name and send the call again; nothing was created."
