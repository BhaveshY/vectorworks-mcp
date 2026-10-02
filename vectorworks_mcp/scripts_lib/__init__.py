"""Vetted Python snippets executed in-process through native ``run_script``
(contract 1.10). They implement features the native bridge lacks natively.

Every snippet receives ``ARGS`` (decoded JSON) plus the helpers from
``_prelude.py`` and must assign ``result`` (JSON-serialisable). They are
listed in ``vw_read(what='capabilities')`` under ``script_features``.

Status: written against the Vectorworks 2024 Python API (vs.*), syntax-checked
and exercised against the mock bridge; NOT yet live-tested in Vectorworks
because no 1.0 bridge with run_script was running during development.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent

SNIPPETS = {
    "edit_engine": "mirror, move to layer, group, ungroup, attach/set/detach record fields",
    "records": "list record formats, read an object's records",
    "saved_views": "list / restore / create / delete saved views (fallback when the bridge lacks saved_views)",
    "stories": "list / create / set stories (fallback when the bridge lacks stories)",
    "import_image": "import a PNG/JPG/BMP/TIF image into the drawing",
    "resource_list": "list hatches, line types, text styles, wall/slab styles, gradients, image fills ...",
    "resource_import": "import a named resource (symbol, hatch, style ...) from another .vwx file",
    "object_detail": "full object detail: bounds, layer, class, records, plug-in name",
}

# BuildResourceList object types (VectorScript appendix). Overridable via resource_type=<int>.
RESOURCE_TYPES = {
    "symbols": 16, "worksheets": 18, "records": 47, "hatches": 66, "line_types": 97,
    "gradients": 105, "image_fills": 106, "tiles": 112, "text_styles": 125, "wall_styles": 127,
    "slab_styles": 130, "saved_views": 103,
}


def load(name: str) -> str:
    if name not in SNIPPETS:
        raise KeyError(name)
    prelude = (_DIR / "_prelude.py").read_text(encoding="utf-8")
    body = (_DIR / f"{name}.py").read_text(encoding="utf-8")
    return prelude + "\n" + body


def build(name: str, args: Any) -> str:
    payload = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    return f"import json as _json\nARGS = _json.loads({payload!r})\n" + load(name)
