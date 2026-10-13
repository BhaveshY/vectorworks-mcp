"""Vetted Python snippets executed in-process through native ``run_script``
(contract 1.10). They implement features the native bridge lacks natively.

Every snippet receives ``ARGS`` (decoded JSON) plus the helpers from
``_prelude.py`` and must assign ``result`` (JSON-serialisable). They are
listed in ``vw_read(what='capabilities')`` under ``script_features``.

Status: written against the Vectorworks 2024 Python API (vs.*), linted against
the SDK stubs (scripts/lint-vs-snippets.py) and exercised against the mock
bridge. ``viewport``, ``massing``, ``edit_engine`` (style) and ``resource_list``
were live-tested in Vectorworks 2024 on 2026-10-13.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent

SNIPPETS = {
    "edit_engine": "mirror, move to layer, group, ungroup, record fields, graphic styles (line type, hatch/gradient "
                   "fill, font/size/style/alignment, markers)",
    "viewport": "sheet viewports: create / set / update / delete (layers, scale, view, render, crop, classes, "
                "annotations)",
    "massing": "3D massing: extrudes, Column plug-in",
    "records": "list record formats, read an object's records",
    "saved_views": "list / restore / create / delete saved views (fallback when the bridge lacks saved_views)",
    "stories": "list / create / set stories (fallback when the bridge lacks stories)",
    "import_image": "import a PNG/JPG/BMP/TIF image into the drawing",
    "resource_list": "list hatches, line types, text styles, wall/slab styles, gradients, image fills ...",
    "resource_import": "import a named resource (symbol, hatch, style ...) from another .vwx file",
    "object_detail": "full object detail: bounds, layer, class, records, plug-in name",
}

# BuildResourceList object types = resource node types (SDK Objs.TDType.h; line types 96, text styles 109
# verified live). Overridable via resource_type=<int>.
RESOURCE_TYPES = {
    "symbols": 16, "worksheets": 18, "materials": 19, "viewport_styles": 36, "records": 47, "hatches": 66,
    "line_types": 96, "textures": 97, "roof_styles": 102, "slab_styles": 107, "tiles": 108, "text_styles": 109,
    "image_fills": 119, "gradients": 120, "wall_styles": 127,
}
RESOURCE_SYNONYMS = {"hatch": "hatches", "line_type": "line_types", "linetype": "line_types", "linetypes": "line_types",
                     "dash": "line_types", "dashes": "line_types", "line_style": "line_types",
                     "line_styles": "line_types", "text_style": "text_styles", "textstyle": "text_styles",
                     "gradient": "gradients", "image_fill": "image_fills", "image": "image_fills",
                     "images": "image_fills", "tile": "tiles", "tile_fills": "tiles", "wall_style": "wall_styles",
                     "slab_style": "slab_styles", "roof_style": "roof_styles", "material": "materials",
                     "texture": "textures", "symbol": "symbols", "worksheet": "worksheets", "record": "records",
                     "viewport_style": "viewport_styles", "fill": "hatches", "fills": "hatches"}
# Snippets that need shared helper files (prepended after the prelude).
INCLUDES = {"edit_engine": ["_style"], "massing": ["_style"], "viewport": ["_style"], "resource_list": ["_style"]}


def load(name: str) -> str:
    if name not in SNIPPETS:
        raise KeyError(name)
    parts = ["_prelude"] + INCLUDES.get(name, []) + [name]
    return "\n".join((_DIR / f"{part}.py").read_text(encoding="utf-8") for part in parts)


def build(name: str, args: Any) -> str:
    payload = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    return f"import json as _json\nARGS = _json.loads({payload!r})\n" + load(name)
