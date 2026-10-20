"""vw_organize: layers, classes, stories, saved views, worksheet cells,
resource import, sheet viewports (script-backed, see scripts_lib/viewport.py)."""

from __future__ import annotations

import re

from .. import coerce as C
from .. import scripts_lib
from ..errors import VWError, validation
from ..host import doc_brief, get_host

KINDS = ["layer", "class", "story", "saved_view", "worksheet", "resource", "viewport"]
ACTIONS = ["create", "set", "delete", "write", "import", "update"]
KIND_SYN = {"layers": "layer", "design_layer": "layer", "sheet": "layer", "sheet_layer": "layer", "classes": "class",
            "stories": "story", "level": "story", "view": "saved_view", "saved_views": "saved_view",
            "worksheets": "worksheet", "cell": "worksheet", "resources": "resource", "symbol": "resource",
            "viewports": "viewport", "vp": "viewport", "drawing": "viewport", "sheet_viewport": "viewport"}
ACTION_SYN = {"add": "create", "new": "create", "make": "create", "update": "set", "edit": "set", "modify": "set",
              "rename": "set", "show": "set", "hide": "set", "activate": "set", "remove": "delete", "del": "delete",
              "put": "write", "set_cell": "write", "load": "import", "regenerate": "update", "refresh": "update",
              "redraw": "update"}


def _cell(ref: str) -> tuple[int, int]:
    m = re.match(r"^\s*([A-Za-z]{1,3})(\d{1,6})\s*$", ref or "")
    if not m:
        raise validation("cell must look like 'B3'", "cell", "B3")
    col = 0
    for ch in m.group(1).upper():
        col = col * 26 + ord(ch) - 64
    return int(m.group(2)), col


def handle(args: dict, ctx, warnings: list) -> dict:
    a = C.lower_keys(args)
    kind = C.choice(a.get("kind") or a.get("type") or a.get("what"), "kind", KINDS, KIND_SYN)
    raw_action = str(a.get("action") or a.get("op") or "").strip().lower()
    synonyms = {k: v for k, v in ACTION_SYN.items() if k != "update"} if kind == "viewport" else ACTION_SYN
    action = C.choice(a.get("action") or a.get("op"), "action", ACTIONS, synonyms)
    if kind != "viewport" and raw_action in {"update", "regenerate", "refresh", "redraw"}:
        action = "set"
    host = get_host()
    target = host.session.target(a.get("document"), write=True)
    inst = target.inst
    name = a.get("name")
    out: dict = {"ok": True, "kind": kind, "action": action}
    if target.document:
        out["document"] = doc_brief(target.document)
    scale = C.scale_for(C.units_name(a.get("units")))

    if kind == "viewport":
        return _viewport(host, target, a, action, scale, out)

    if kind == "layer":
        if not inst.supports("layers"):
            raise host.bridge.missing_action(inst, "layers")
        name = C.text(name, "name", required=True, max_len=255)
        params: dict = {"action": action if action != "write" else "set"}
        if action == "create":
            params.update({"name": name, "kind": C.choice(a.get("layer_kind"), "layer_kind", ["design", "sheet"],
                                                          default="sheet" if str(a.get("kind")).startswith("sheet") else "design")})
            if a.get("title"):
                params["title"] = str(a["title"])
        else:
            params["target"] = name
        if a.get("new_name"):
            params["name"] = str(a["new_name"])
        if a.get("visibility") not in (None, ""):
            params["visibility"] = C.choice(a["visibility"], "visibility", ["show", "gray", "hide"],
                                            {"visible": "show", "grey": "gray", "hidden": "hide", "invisible": "hide"})
        if a.get("active") not in (None, ""):
            params["active"] = C.boolean(a["active"], "active")
        if a.get("scale") not in (None, ""):
            params["scale"] = _scale(a["scale"])
        if a.get("elevation") not in (None, ""):
            params["elevation"] = C.length(a["elevation"], "elevation", scale)
        if a.get("opacity") not in (None, ""):
            params["opacity"] = C.integer(a["opacity"], "opacity")
        if action == "delete":
            params["confirm"] = "delete"
            if a.get("force") not in (None, ""):
                params["force"] = C.boolean(a["force"], "force")
        if action == "import":
            raise validation("kind=layer supports create, set, delete", "action", "create")
        out["result"] = _clean(host.call(target, "layers", params, kind="write"))
        layer_rec = out["result"].get("layer") if isinstance(out["result"], dict) else None
        if isinstance(layer_rec, dict) and not C.boolean(a.get("detail", False), "detail"):
            out["result"]["layer"] = {k: v for k, v in layer_rec.items()
                                      if k in {"name", "kind", "scale", "elevation", "visible", "story"} and v is not None}
        return out

    if kind == "class":
        name = C.text(name, "name", required=True, max_len=255)
        if action == "create":
            out["result"] = _clean(host.call(target, "manage_classes", {"action": "create", "class_name": name}, kind="write"))
            if any(a.get(k) not in (None, "") for k in ("visibility", "fill_color", "pen_color", "line_weight", "active")):
                action = "set"
            else:
                return out
        if action == "delete":
            out["result"] = _clean(host.call(target, "manage_classes", {"action": "delete", "class_name": name,
                                                                         "confirm": "DELETE_CLASS"}, kind="write"))
            return out
        if action == "set":
            if not inst.v2:
                raise VWError("upgrade_required", "Changing class attributes needs the 1.0 bridge (manage_classes set).")
            params = {"action": "set", "name": name}
            if a.get("new_name"):
                params["new_name"] = str(a["new_name"])
            if a.get("visibility") not in (None, ""):
                params["visibility"] = C.choice(a["visibility"], "visibility", ["show", "gray", "hide"],
                                                {"visible": "show", "grey": "gray", "hidden": "hide"})
            for key in ("active", "use_at_creation"):
                if a.get(key) not in (None, ""):
                    params[key] = C.boolean(a[key], key)
            for key in ("fill_color", "pen_color"):
                if a.get(key) not in (None, ""):
                    params[key] = C.color(a[key], key)
            if a.get("line_weight") not in (None, ""):
                params["line_weight"] = C.integer(a["line_weight"], "line_weight")
            out["result"] = _clean(host.call(target, "manage_classes", params, kind="write"))
            return out
        raise validation("kind=class supports create, set, delete", "action", "create")

    if kind == "story":
        name = C.text(name, "name", required=True, max_len=255)
        params = {"action": "create" if action == "create" else "set", "name": name}
        if a.get("elevation") not in (None, ""):
            params["elevation_mm"] = C.length(a["elevation"], "elevation", scale)
            params["elevation"] = params["elevation_mm"]
        if a.get("new_name"):
            params["new_name"] = str(a["new_name"])
        if a.get("title"):
            params["suffix"] = str(a["title"])
        if action not in {"create", "set"}:
            raise validation("kind=story supports create and set", "action", "create")
        if inst.supports("stories"):
            out["result"] = _clean(host.call(target, "stories", params, kind="write"))
        else:
            out["result"] = host.script(target, "stories", params, label="MCP story")
            out["via"] = "script"
        layers = C.str_list(a.get("layers") or a.get("layer"), "layers")
        if layers:  # put design layers on this story (IFC export maps stories from these)
            assigned = host.script(target, "stories", {"action": "assign", "name": params.get("new_name") or name,
                                                       "layers": layers, "level_type": a.get("level_type") or ""},
                                   label="MCP story layers")
            out["layers"] = assigned
            if isinstance(assigned, dict) and assigned.get("failed"):
                warnings.append(f"Not assigned to the story: {assigned['failed']} ({assigned.get('note')}).")
        return out

    if kind == "saved_view":
        name = C.text(name, "name", required=True, max_len=255)
        sv_action = {"create": "create", "delete": "delete", "set": "restore"}.get(action)
        if sv_action is None:
            raise validation("kind=saved_view supports create, set (restore), delete", "action", "create")
        if inst.supports("saved_views"):
            out["result"] = _clean(host.call(target, "saved_views", {"action": sv_action, "name": name}, kind="write"))
        else:
            out["result"] = host.script(target, "saved_views", {"action": sv_action, "name": name}, label="MCP saved view")
            out["via"] = "script"
        return out

    if kind == "worksheet":
        if action not in {"write", "set"}:
            raise validation("kind=worksheet supports action='write' (cell + value)", "action", "write")
        name = C.text(name, "name", required=True)
        row, col = _cell(str(a.get("cell") or ""))
        value = a.get("value")
        if value is None:
            raise validation("value is required (text or a formula like '=AREA')", "value", "=COUNT")
        out["result"] = _clean(host.call(target, "worksheet", {"action": "write", "worksheet_name": name, "row": row,
                                                               "column": col, "formula": str(value)}, kind="write"))
        return out

    # resource import
    if action != "import":
        raise validation("kind=resource supports action='import' (source + name + resource_type)", "action", "import")
    source = C.text(a.get("source"), "source", required=True)
    rtype = str(a.get("resource_type") or "symbols").lower()
    code = scripts_lib.RESOURCE_TYPES.get(rtype)
    if code is None:
        code = C.integer(rtype, "resource_type")
    out["result"] = host.script(target, "resource_import", {"path": source, "resource_type": code,
                                                             "name": C.text(name, "name", required=True)},
                                label="MCP import resource")
    out["via"] = "script"
    return out


def _scale(value) -> float:
    """100 | "1:100" | "1/100" -> 100."""
    text = str(value).strip().replace(" ", "")
    match = re.fullmatch(r"1[:/](\d+(?:\.\d+)?)", text)
    number = float(match.group(1)) if match else C.number(value, "scale", example="1:100")
    if number <= 0:
        raise validation("scale must be > 0 (100 or '1:100')", "scale", "1:100")
    return number


_VIS = {"show": 0, "visible": 0, "on": 0, "hide": -1, "hidden": -1, "off": -1, "invisible": -1, "gray": 2,
        "grey": 2, "grayed": 2}
_ANNOTATION_TYPES = ["text", "line", "rect", "polygon", "dimension"]
_ANNOTATION_SYN = {"label": "text", "note": "text", "rectangle": "rect", "box": "rect", "dim": "dimension",
                   "linear_dimension": "dimension", "polyline": "polygon", "segment": "line"}


def _annotations(raw, scale: float) -> list[dict]:
    from .. import builders as B
    out = []
    raw = C.parse_json_maybe(raw)
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, list):
        # a plain string = a text note at the drawing origin, stacked downwards (a weak-model spelling, live)
        raw = [{"type": "text", "text": s, "at": [0, -1000 * (n + 1)]} if isinstance(s, str) else s
               for n, s in enumerate(raw)]
    for i, item in enumerate(C.obj_list(raw, "annotations", example=[{"type": "text", "at": [0, 0], "text": "A"}], maximum=200)):
        src = C.lower_keys(item)
        field = f"annotations[{i}]"
        kind = C.choice(src.get("type") or src.get("kind"), f"{field}.type", _ANNOTATION_TYPES, _ANNOTATION_SYN)
        row: dict = {"type": kind}
        if kind == "text":
            row["at"] = C.point(src.get("at") or src.get("position") or src.get("origin"), f"{field}.at", scale)
            row["text"] = C.text(src.get("text") or src.get("value"), f"{field}.text", required=True)
            if src.get("size") not in (None, ""):
                src.setdefault("text_size", src["size"])
        elif kind in {"line", "dimension"}:
            row["start"] = C.point(src.get("start") or src.get("from"), f"{field}.start", scale)
            row["end"] = C.point(src.get("end") or src.get("to"), f"{field}.end", scale)
            if kind == "dimension":
                row["offset"] = C.length(src.get("offset", 300), f"{field}.offset", scale)
        elif kind == "rect":
            start = src.get("start") or src.get("corner")
            if start is not None and src.get("end") is not None:
                row["start"] = C.point(start, f"{field}.start", scale)
                row["end"] = C.point(src["end"], f"{field}.end", scale)
            else:
                o = C.point(src.get("origin") or src.get("at") or start, f"{field}.origin", scale)
                w = C.length(src.get("width"), f"{field}.width", scale)
                h = C.length(src.get("height") or src.get("depth"), f"{field}.height", scale)
                row["start"], row["end"] = o, [o[0] + w, o[1] + h]
        else:
            row["points"] = C.points(src.get("points"), f"{field}.points", scale, minimum=2)
        style = B.style_props(src, field, basic=True)
        if style:
            row["style"] = style
        out.append(row)
    return out


def viewport_record(record: dict) -> dict:
    """Snippet viewport readback -> names (view, render_mode), rounded mm."""
    from .capture import RENDER_CODES, STANDARD_VIEW_CODES
    record = dict(record)
    codes = record.pop("view_code", None)
    record["view"] = next((n for n, c in STANDARD_VIEW_CODES.items() if codes and list(c) == list(codes)), codes)
    if codes:
        record["projection"] = {0: "orthogonal", 1: "perspective", 6: "plan"}.get(codes[1], codes[1])
    rcode = record.pop("render_code", None)
    record["render_mode"] = next((n for n, c in RENDER_CODES.items() if c == rcode), rcode)
    record["center"] = [round(v, 1) for v in record.get("center") or []]
    record["size"] = [round(v, 1) for v in record.get("size") or []]
    # defaults are noise for the model
    return {k: v for k, v in record.items() if v not in ([], {}, None, "", False, 0) or k == "scale"}


def _viewport(host, target, a: dict, action: str, scale: float, out: dict) -> dict:
    """kind=viewport through the vetted `viewport` run_script snippet (one undo step, rolled back on error)."""
    from .. import builders as B
    from .capture import RENDER_CODES, RENDER_SYNONYMS, STANDARD_VIEW_CODES, VIEW_SYNONYMS
    from ..catalog import RENDER_MODES, STANDARD_VIEWS
    if action not in {"create", "set", "update", "delete"}:
        raise validation("kind=viewport supports create, set, update (regenerate), delete", "action", "create")
    if not host.can_script(target.inst):
        raise host.bridge.missing_action(target.inst, "run_script")
    args: dict = {"action": action}
    ref = a.get("target") or (a.get("name") if action != "create" else None)
    if ref not in (None, ""):
        args["target"] = B.norm_ref(ref, "target")
    if a.get("sheet") not in (None, ""):
        args["sheet"] = C.text(a["sheet"], "sheet", max_len=255)
    if action == "create" and not args.get("sheet"):
        raise validation("sheet (sheet layer name; created when missing) is required to create a viewport", "sheet",
                         "A-101")
    if action in {"set", "delete"} and not args.get("target"):
        raise validation(f"target (viewport uuid or name) is required for action={action}", "target", "GF Plan")
    if action == "update" and not (args.get("target") or args.get("sheet")):
        raise validation("target (viewport) or sheet (all viewports on it) is required", "target", "GF Plan")
    if action == "delete" and not C.boolean(a.get("confirm", False), "confirm"):
        raise VWError("confirmation_required", "Deleting a viewport also deletes its annotations. Ask the user, then "
                      "repeat with confirm=true.", field="confirm", example=True)
    if action == "create" and a.get("name"):
        args["name"] = C.text(a["name"], "name", max_len=255)
    if a.get("new_name"):
        args["new_name"] = C.text(a["new_name"], "new_name", max_len=255)
    if a.get("layers") not in (None, ""):
        args["layers"] = {"show": C.str_list(a["layers"], "layers")}
    if a.get("scale") not in (None, ""):
        args["scale"] = _scale(a["scale"])
    view = a.get("view") or a.get("standard_view")
    if view not in (None, ""):
        args["view"] = list(STANDARD_VIEW_CODES[C.choice(view, "view", STANDARD_VIEWS, VIEW_SYNONYMS)])
    if a.get("render_mode") not in (None, ""):
        args["render"] = RENDER_CODES[C.choice(a["render_mode"], "render_mode", RENDER_MODES, RENDER_SYNONYMS)]
    if a.get("at") not in (None, ""):
        args["at"] = C.point(a["at"], "at", 1.0)  # sheet (paper) millimetres
    crop = C.parse_json_maybe(a.get("crop"))
    if crop is False or (isinstance(crop, (str, list)) and (crop == [] or str(crop).strip().lower() in {"none", "clear", "off", "false"})):
        args["crop"] = "clear"
    elif crop not in (None, ""):
        if isinstance(crop, dict):
            c = C.lower_keys(crop)
            o = C.point(c.get("origin") or c.get("at"), "crop.origin", scale)
            crop = [o, [o[0] + C.length(c.get("width"), "crop.width", scale), o[1] + C.length(c.get("height") or c.get("depth"), "crop.height", scale)]]
            args["crop"] = crop
        else:
            pts = C.points(crop, "crop", scale, minimum=2)
            if len(pts) == 2 and (pts[0][0] == pts[1][0] or pts[0][1] == pts[1][1]):
                raise validation("crop rectangle needs two opposite corners [[x1,y1],[x2,y2]]", "crop", [[0, 0], [10000, 8000]])
            args["crop"] = pts
    classes = C.parse_json_maybe(a.get("classes"))
    if classes not in (None, "", {}):
        if not isinstance(classes, dict):
            raise validation("classes must be {class name: show|hide|gray}", "classes", {"A-Furniture": "hide"})
        args["classes"] = {}
        for name, vis in classes.items():
            key = str(vis).strip().lower() if not isinstance(vis, bool) else ("show" if vis else "hide")
            if key not in _VIS:
                raise validation(f"classes.{name} must be show, hide or gray", "classes", {name: "hide"})
            args["classes"][str(name)] = _VIS[key]
    if a.get("title") is not None:
        args["title"] = str(a["title"])
    if a.get("annotations") not in (None, "", []):
        if action not in {"create", "set"}:
            raise validation("annotations are added with action=create or set", "action", "set")
        args["annotations"] = _annotations(a["annotations"], scale)
    if action == "set" and len([k for k in args if k not in {"action", "target", "sheet"}]) == 0:
        raise validation("action=set needs at least one change (layers, scale, view, render_mode, at, crop, classes, "
                         "title, new_name, annotations)", "scale", "1:50")
    result = host.script(target, "viewport", args, label=f"MCP viewport {action}") or {}
    if isinstance(result.get("viewport"), dict):
        result["viewport"] = viewport_record(result["viewport"])
    if result.get("warnings"):
        out["warnings"] = result.pop("warnings")
    out["result"] = result
    out["via"] = "script"
    out["undo"] = "one undo step"
    want = C.boolean(a["preview"], "preview") if a.get("preview") not in (None, "") else None
    if action in {"create", "set"}:
        out["next_step"] = ("Done: the viewport readback above is authoritative. vw_capture(view='sheet', sheet=<sheet>) "
                            "once if you want to look at it.")
        if want or (want is None and action == "create"):
            from . import preview
            vp = result.get("viewport") if isinstance(result.get("viewport"), dict) else {}
            sheet = args.get("sheet") or vp.get("sheet") or vp.get("sheet_layer")
            if sheet:
                out["next_step"] = ("Done: the viewport readback above is authoritative. LOOK at the sheet preview image "
                                    "before reporting success (layers, scale, crop, annotations).")
                return preview.attach(host, a.get("document"), out, out.setdefault("warnings", []), sheet=str(sheet))
    return out


def _clean(value):
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if k != "timing"}
    return value
