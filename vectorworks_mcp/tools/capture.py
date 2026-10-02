"""vw_capture: image of the drawing for the model (MCP image + resource link)."""

from __future__ import annotations

from pathlib import Path

from .. import capture as cap
from .. import coerce as C
from ..catalog import RENDER_MODES, STANDARD_VIEWS
from ..errors import VWError
from ..host import doc_brief, get_host
from ..mcp_core import ToolOutput

# name -> (standard_view, projection or None)
STANDARD_VIEW_CODES = {"top_plan": (7, 6), "top": (7, 0), "front": (3, 0), "back": (4, 0), "left": (5, 0),
                       "right": (6, 0), "bottom": (8, 0), "iso": (9, 0), "right_iso": (9, 0), "left_iso": (10, 0),
                       "right_rear_iso": (11, 0), "left_rear_iso": (12, 0)}
VIEW_SYNONYMS = {"plan": "top_plan", "2d": "top_plan", "top/plan": "top_plan", "isometric": "iso", "3d": "iso",
                 "axonometric": "iso", "front_view": "front", "side": "right"}
RENDER_CODES = {"wireframe": 0, "opengl": 11, "shaded": 11, "hidden_line": 6, "dashed_hidden_line": 7,
                "final_shaded": 5, "sketch": 18, "fast_renderworks": 12, "final_renderworks": 14}
RENDER_SYNONYMS = {"wire": "wireframe", "open_gl": "opengl", "hidden": "hidden_line", "render": "fast_renderworks",
                   "renderworks": "final_renderworks", "realistic": "final_renderworks"}
LONG_RENDER = {5, 12, 14, 15, 17, 18}


def view_codes(a: dict) -> dict:
    params: dict = {}
    if a.get("standard_view") not in (None, ""):
        name = C.choice(a["standard_view"], "standard_view", STANDARD_VIEWS, VIEW_SYNONYMS)
        sv, proj = STANDARD_VIEW_CODES[name]
        params["standard_view"] = sv
        params["projection"] = proj
    if a.get("render_mode") not in (None, ""):
        params["render_mode"] = RENDER_CODES[C.choice(a["render_mode"], "render_mode", RENDER_MODES, RENDER_SYNONYMS)]
    return params


def handle(args: dict, ctx, warnings: list):
    a = C.lower_keys(args)
    host = get_host()
    view = C.choice(a.get("view") or a.get("mode") or a.get("what"), "view",
                    ["drawing", "current", "selection", "objects", "layer", "sheet", "saved_view"],
                    {"all": "drawing", "fit": "drawing", "extents": "drawing", "screen": "current", "screenshot": "current",
                     "selected": "selection", "object": "objects", "uuids": "objects", "sheet_layer": "sheet",
                     "savedview": "saved_view"}, default="drawing")
    fmt = C.choice(a.get("format"), "format", ["png", "jpg"], {"jpeg": "jpg"}, default="png")
    target = host.session.target(a.get("document"))
    inst = target.inst
    if not inst.supports("capture_view"):
        raise host.bridge.missing_action(inst, "capture_view")
    path = cap.new_capture_path(fmt)
    params: dict = {"path": str(path), "file_path": str(path), "format": fmt}
    kind = "read"
    if inst.v2:
        params.update({"restore": True, "update_viewports": False, "reset_plugin_objects": False})
        params.update(view_codes(a))
        if a.get("margin_pct") not in (None, ""):
            params["margin_pct"] = C.number(a["margin_pct"], "margin_pct")
        if view == "drawing":
            params["fit"] = "drawing"
        elif view == "selection":
            params["fit"] = "selection"
        elif view == "objects":
            uuids = [u[5:] if u.startswith("uuid:") else u for u in C.str_list(a.get("objects") or a.get("target"), "objects")]
            if not uuids:
                raise VWError("validation_error", "view='objects' needs objects=[<uuid>, ...]", field="objects",
                              example=["1A2B3C4D-..."])
            params.update({"fit": "objects", "object_uuids": uuids})
        elif view in {"layer", "sheet"}:
            name = a.get(view) or a.get("layer") or a.get("sheet") or a.get("name")
            if not name:
                raise VWError("validation_error", f"view='{view}' needs {view}=<name>", field=view, example="Design Layer-1")
            params.update({"layer": str(name), "fit": "layer"})
        elif view == "saved_view":
            name = a.get("saved_view") or a.get("name")
            if not name:
                raise VWError("validation_error", "view='saved_view' needs saved_view=<name>", field="saved_view",
                              example="Plan view")
            params["saved_view"] = str(name)
        if params.get("render_mode") in LONG_RENDER:
            kind = "long"
    else:
        ignored = [k for k in ("standard_view", "render_mode") if a.get(k)]
        if view != "current" or ignored:
            warnings.append("Pre-1.0 bridge: captured the current view as it is (fitting, standard views and render "
                            "modes need the 1.0 bridge, and the old fit would change the user's selection).")
    result = host.call(target, "capture_view", params, kind=kind, ctx=ctx)
    data = result if isinstance(result, dict) else {}
    real_path = Path(str(data.get("path") or data.get("file_path") or path))
    if not real_path.exists():
        raise VWError("native_error", f"Vectorworks reported a capture but {real_path} does not exist.")
    blocks, info = cap.content_blocks(real_path)
    cap.prune()
    structured = {"ok": True, "path": str(real_path), "resource": info.get("resource"),
                  "width": data.get("width") or info.get("width"), "height": data.get("height") or info.get("height"),
                  "restored": data.get("restored", inst.v2 and True)}
    if info.get("model_width"):
        structured["image_scaled_to"] = [info["model_width"], info["model_height"]]
    if info.get("inline_error"):
        warnings.append(info["inline_error"])
    if data.get("view_used"):
        structured["view_used"] = data["view_used"]
    if target.document:
        structured["document"] = doc_brief(target.document)
    return ToolOutput(structured, blocks)
