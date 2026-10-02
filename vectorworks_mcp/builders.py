"""vw_create / vw_edit compilers: friendly flat objects -> 0.7 normalised
operations -> native ``apply_operations`` wire ops.

Everything is converted to millimetres here; the 0.7 normaliser then runs with
scale 1 so its validation (types, ranges, wall-host rules) still applies.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from . import coerce as C
from . import ops
from .errors import VWError, validation

CREATE_SYNONYMS = {
    "rectangle": "rect", "box": "rect", "square": "rect", "ellipse": "oval", "polyline": "polyline",
    "poly": "polygon", "polygon2d": "polygon", "linear_dimension": "dimension", "dim": "dimension",
    "label": "text", "note": "text", "room_space": "space", "floor": "slab", "symbol_instance": "symbol",
    "plugin": "parametric", "pio": "parametric", "segment": "line", "schematic_room": "room",
}
CREATE_TYPES = ["line", "rect", "oval", "circle", "arc", "polygon", "polyline", "wall", "slab", "roof", "space",
                "text", "dimension", "symbol", "door", "window", "parametric", "room"]

DEFAULTS_MM = {"wall_height": 3000.0, "wall_thickness": 200.0, "slab_thickness": 200.0, "door_width": 900.0,
               "door_height": 2100.0, "window_width": 1200.0, "window_height": 1200.0, "window_sill": 900.0,
               "dimension_offset": 300.0, "roof_overhang": 500.0}

EDIT_OPS = ["set", "move", "rotate", "scale", "mirror", "reshape", "duplicate", "delete", "parametric", "class",
            "layer", "group", "ungroup", "record", "select", "deselect"]
EDIT_SYNONYMS = {"translate": "move", "shift": "move", "transform": "move", "copy": "duplicate", "clone": "duplicate",
                 "remove": "delete", "erase": "delete", "set_properties": "set", "set_property": "set", "properties": "set",
                 "update": "set", "update_parametric": "parametric", "params": "parametric", "set_class": "class",
                 "move_to_layer": "layer", "set_layer": "layer", "flip": "mirror", "resize": "scale", "turn": "rotate",
                 "attach_record": "record", "set_record": "record", "unselect": "deselect", "clear_selection": "deselect"}
PROPERTY_SYNONYMS = {"name": "name", "class": "class", "class_name": "class", "fill_color": "fillColor",
                     "fillcolor": "fillColor", "fill": "fillColor", "pen_color": "penColor", "pencolor": "penColor",
                     "pen": "penColor", "stroke": "penColor", "line_color": "penColor", "fill_pattern": "fillPattern",
                     "fillpattern": "fillPattern", "line_weight": "lineWeight", "lineweight": "lineWeight",
                     "pen_weight": "lineWeight", "opacity": "opacity"}
SCRIPT_EDIT_OPS = {"mirror", "layer", "group", "ungroup", "record"}


def _get(src: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in src and src[key] is not None and src[key] != "":
            return src[key]
    return default


def _pt(src: dict, names: tuple[str, ...], xy: tuple[str, str], field: str, scale: float, required: bool = True) -> Optional[list[float]]:
    value = _get(src, *names)
    if value is not None:
        return C.point(value, field, scale)
    if xy[0] in src and xy[1] in src:
        return [C.length(src[xy[0]], f"{field}.{xy[0]}", scale), C.length(src[xy[1]], f"{field}.{xy[1]}", scale)]
    if required:
        raise validation(f"{field} is required as [x, y]", field, [0, 0])
    return None


def _len(src: dict, keys: tuple[str, ...], field: str, scale: float, default_mm: Optional[float] = None,
         required: bool = False, positive: bool = False) -> Optional[float]:
    value = _get(src, *keys)
    if value is None:
        if required:
            raise validation(f"{field} is required", field, "3000mm")
        return default_mm
    result = C.length(value, field, scale)
    if positive and result <= 0:
        raise validation(f"{field} must be > 0", field, "3000mm")
    return result


def _common(src: dict, out: dict) -> dict:
    name = _get(src, "name")
    if name is not None:
        out["name"] = C.text(name, "name", max_len=255)
    cls = _get(src, "class", "class_name", "klass")
    if cls is not None:
        out["class_name"] = C.text(cls, "class", max_len=255)
    return out


class CreatePlan:
    """Result of compiling vw_create objects."""

    def __init__(self) -> None:
        self.primary: list[dict] = []      # 0.7 normalised create ops (atomic first transaction)
        self.hosted: list[dict] = []       # doors/windows that reference a local wall id
        self.local_walls: dict[str, dict] = {}
        self.needs_descriptor: set[str] = set()
        self.warnings: list[str] = []
        self.ids: list[str] = []


def compile_create(objects: list[dict], scale: float, *, descriptors: dict[str, str],
                   schemas: dict[str, dict]) -> CreatePlan:
    plan = CreatePlan()
    seen_ids: set[str] = set()
    for index, raw in enumerate(objects):
        field = f"objects[{index}]"
        src = C.lower_keys(raw)
        kind = C.choice(_get(src, "type", "object_type", "kind", "op"), f"{field}.type", CREATE_TYPES, CREATE_SYNONYMS)
        local_id = _get(src, "id", "operation_id", "ref")
        if local_id is not None:
            local_id = str(local_id).lstrip("$")
            if not ops._IDEMPOTENCY_KEY_RE.fullmatch(local_id):
                raise validation(f"{field}.id must be letters/digits/._:- (got {local_id!r})", f"{field}.id", "w1")
            if local_id in seen_ids:
                raise validation(f"{field}.id {local_id!r} is used twice", f"{field}.id", f"{local_id}b")
            seen_ids.add(local_id)
        else:
            local_id = f"o{index + 1}"
        if kind == "room":
            for sub in _room_objects(src, field, scale, local_id):
                _append(plan, sub, sub.pop("_id"))
            continue
        params = _create_params(kind, src, field, scale, plan, descriptors, schemas)
        if kind == "wall":
            plan.local_walls[local_id] = params
        _append(plan, params, local_id)
    return plan


def _append(plan: CreatePlan, params: dict, local_id: str) -> None:
    item = {"type": "create", "operation_id": local_id, "params": params}
    wall_ref = params.get("wall_uuid", "")
    if params.get("object_type") in {"door", "window"} and isinstance(wall_ref, str) and wall_ref.startswith("$"):
        plan.hosted.append(item)
    else:
        plan.primary.append(item)
    plan.ids.append(local_id)


def _create_params(kind: str, src: dict, field: str, scale: float, plan: CreatePlan,
                   descriptors: dict[str, str], schemas: dict[str, dict]) -> dict:
    p: dict[str, Any] = {}
    if kind == "line":
        a = _pt(src, ("start", "from", "p1", "a"), ("x1", "y1"), f"{field}.start", scale)
        b = _pt(src, ("end", "to", "p2", "b"), ("x2", "y2"), f"{field}.end", scale)
        p = {"object_type": "line", "x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]}
    elif kind in {"rect", "oval"}:
        a = _pt(src, ("start", "corner", "p1", "from", "min"), ("x1", "y1"), f"{field}.start", scale, required=False)
        b = _pt(src, ("end", "opposite", "p2", "to", "max"), ("x2", "y2"), f"{field}.end", scale, required=False)
        if a is None or b is None:
            w = _len(src, ("width", "w", "size_x"), f"{field}.width", scale, required=True, positive=True)
            h = _len(src, ("height", "depth", "h", "size_y"), f"{field}.height", scale, required=True, positive=True)
            center = _pt(src, ("center", "centre"), ("cx", "cy"), f"{field}.center", scale, required=False)
            origin = a or _pt(src, ("origin", "at", "position", "location", "bottom_left"), ("x", "y"), f"{field}.origin", scale,
                              required=center is None)
            if center is not None and origin is None:
                origin = [center[0] - w / 2, center[1] - h / 2]
            a, b = origin, [origin[0] + w, origin[1] + h]
        p = {"object_type": kind, "x1": min(a[0], b[0]), "y1": min(a[1], b[1]), "x2": max(a[0], b[0]), "y2": max(a[1], b[1])}
    elif kind in {"circle", "arc"}:
        c = _pt(src, ("center", "centre", "at", "origin"), ("x", "y"), f"{field}.center", scale)
        r = _len(src, ("radius", "r"), f"{field}.radius", scale)
        if r is None:
            d = _len(src, ("diameter", "d"), f"{field}.radius", scale, required=True, positive=True)
            r = d / 2
        p = {"object_type": kind, "x1": c[0], "y1": c[1], "radius": r}
        if kind == "arc":
            p["start_angle"] = C.number(_get(src, "start_angle", "start", "from_angle", default=0), f"{field}.start_angle")
            p["sweep_angle"] = C.number(_get(src, "sweep_angle", "sweep", "angle", default=90), f"{field}.sweep_angle")
    elif kind in {"polygon", "polyline"}:
        closed_default = kind == "polygon"
        pts = C.points(_get(src, "points", "vertices", "pts"), f"{field}.points", scale, minimum=2)
        closed = C.boolean(_get(src, "closed", default=closed_default), f"{field}.closed")
        p = {"object_type": "polygon", "points": pts, "closed": closed}
    elif kind == "wall":
        a = _pt(src, ("start", "from", "p1"), ("x1", "y1"), f"{field}.start", scale) if not {"start_x", "start_y"} <= set(src) else \
            [C.length(src["start_x"], f"{field}.start_x", scale), C.length(src["start_y"], f"{field}.start_y", scale)]
        b = _pt(src, ("end", "to", "p2"), ("x2", "y2"), f"{field}.end", scale) if not {"end_x", "end_y"} <= set(src) else \
            [C.length(src["end_x"], f"{field}.end_x", scale), C.length(src["end_y"], f"{field}.end_y", scale)]
        p = {"object_type": "wall", "start_x": a[0], "start_y": a[1], "end_x": b[0], "end_y": b[1],
             "height": _len(src, ("height", "wall_height"), f"{field}.height", scale, DEFAULTS_MM["wall_height"], positive=True),
             "thickness": _len(src, ("thickness", "width", "wall_thickness"), f"{field}.thickness", scale, DEFAULTS_MM["wall_thickness"], positive=True)}
        style = _get(src, "style", "style_name", "wall_style")
        if style:
            p["style_name"] = str(style)
    elif kind in {"slab", "roof", "space"}:
        pts = C.points(_get(src, "points", "boundary", "outline", "vertices"), f"{field}.points", scale, minimum=3)
        p = {"object_type": kind, "points": pts, "closed": True}
        if kind == "slab":
            p["thickness"] = _len(src, ("thickness",), f"{field}.thickness", scale, DEFAULTS_MM["slab_thickness"], positive=True)
            p["elevation"] = _len(src, ("elevation", "z"), f"{field}.elevation", scale, 0.0)
            style = _get(src, "style", "style_name")
            if style:
                p["style_name"] = str(style)
        elif kind == "roof":
            p["thickness"] = _len(src, ("thickness",), f"{field}.thickness", scale, 200.0, positive=True)
            p["slope"] = C.number(_get(src, "slope", "pitch", default=30), f"{field}.slope")
            p["overhang"] = _len(src, ("overhang", "eave"), f"{field}.overhang", scale, DEFAULTS_MM["roof_overhang"])
            p["elevation"] = _len(src, ("elevation", "bearing_height", "z"), f"{field}.elevation", scale, 0.0)
            for key in ("bearing_inset", "vertical_miter"):
                if key in src:
                    p[key] = _len(src, (key,), f"{field}.{key}", scale, 0.0)
            for key in ("miter_type",):
                if key in src:
                    p[key] = C.integer(src[key], f"{field}.{key}")
            if "generate_gable_walls" in src:
                p["generate_gable_walls"] = C.boolean(src["generate_gable_walls"], f"{field}.generate_gable_walls")
        else:
            p["height"] = _len(src, ("height", "room_height"), f"{field}.height", scale, 3000.0, positive=True)
            room_id = _get(src, "room_id", "number", "room_number")
            if room_id is not None:
                p["room_id"] = str(room_id)
    elif kind == "text":
        at = _pt(src, ("at", "position", "origin", "location", "point"), ("x", "y"), f"{field}.at", scale)
        content = C.text(_get(src, "text", "content", "value", "string"), f"{field}.text", required=True)
        p = {"object_type": "text", "text": content, "x1": at[0], "y1": at[1],
             "width": _len(src, ("width", "box_width"), f"{field}.width", scale, 0.0),
             "rotation": C.number(_get(src, "rotation", "angle", default=0), f"{field}.rotation"),
             "text_size": C.number(_get(src, "size", "text_size", "font_size", default=0), f"{field}.size")}
        if "wrap" in src:
            p["wrap"] = C.boolean(src["wrap"], f"{field}.wrap")
        if "fixed_size" in src:
            p["fixed_size"] = C.boolean(src["fixed_size"], f"{field}.fixed_size")
    elif kind == "dimension":
        a = _pt(src, ("start", "from", "p1"), ("x1", "y1"), f"{field}.start", scale)
        b = _pt(src, ("end", "to", "p2"), ("x2", "y2"), f"{field}.end", scale)
        p = {"object_type": "linear_dimension", "start_x": a[0], "start_y": a[1], "end_x": b[0], "end_y": b[1],
             "offset": _len(src, ("offset", "dimension_offset"), f"{field}.offset", scale, DEFAULTS_MM["dimension_offset"])}
        for key in ("text_offset",):
            if key in src:
                p[key] = _len(src, (key,), f"{field}.{key}", scale, 0.0)
        if "dimension_type" in src:
            p["dimension_type"] = C.integer(src["dimension_type"], f"{field}.dimension_type")
    elif kind == "symbol":
        at = _pt(src, ("at", "position", "origin", "location"), ("x", "y"), f"{field}.at", scale, required=False) or [0.0, 0.0]
        definition = _get(src, "symbol", "definition", "definition_name", "symbol_name", "resource")
        if definition is None and "name" in src and not any(k in src for k in ("symbol", "definition")):
            definition = src["name"]
        p = {"object_type": "symbol", "definition_name": C.text(definition, f"{field}.symbol", required=True),
             "x1": at[0], "y1": at[1], "rotation": C.number(_get(src, "rotation", "angle", default=0), f"{field}.rotation")}
    elif kind in {"door", "window", "parametric"}:
        p = _parametric_params(kind, src, field, scale, plan, descriptors, schemas)
    if kind != "symbol" or "symbol" in src or "definition" in src:
        _common(src, p)
    else:
        cls = _get(src, "class", "class_name")
        if cls:
            p["class_name"] = str(cls)
    try:
        return ops._normalise_create_primitive(p, label=field, scale_to_mm=1.0)
    except ValueError as exc:
        raise validation(str(exc), field, _example(kind)) from None


def _parametric_params(kind: str, src: dict, field: str, scale: float, plan: CreatePlan,
                       descriptors: dict[str, str], schemas: dict[str, dict]) -> dict:
    plugin = "Door" if kind == "door" else "Window" if kind == "window" else C.text(
        _get(src, "plugin", "plugin_name", "object", "pio"), f"{field}.plugin", required=True)
    p: dict[str, Any] = {"object_type": kind, "plugin_name": plugin}
    fingerprint = _get(src, "descriptor_fingerprint", "fingerprint")
    if fingerprint is None:
        fingerprint = descriptors.get(plugin)
        if fingerprint is None:
            plan.needs_descriptor.add(plugin)
            fingerprint = "pending"
    p["descriptor_fingerprint"] = str(fingerprint)
    wall_ref = _get(src, "wall", "wall_uuid", "host", "in_wall")
    at = _pt(src, ("at", "position", "location", "point", "center"), ("x", "y"), f"{field}.at", scale, required=False)
    if kind in {"door", "window"}:
        if wall_ref is None:
            raise validation(f"{field}.wall is required (the wall's uuid, or \"$<id>\" of a wall created in this call)",
                             f"{field}.wall", "$w1")
        wall_ref = str(wall_ref)
        if wall_ref.startswith("uuid:"):
            wall_ref = wall_ref[5:]
        if not wall_ref.startswith("$") and len(wall_ref) < 8:
            wall_ref = "$" + wall_ref  # "w1" -> "$w1" when it names a local wall
        if at is None:
            offset = _len(src, ("offset", "distance", "along", "position_along"), f"{field}.offset", scale)
            local = plan.local_walls.get(wall_ref.lstrip("$")) if wall_ref.startswith("$") else None
            if offset is None or local is None:
                raise validation(f"{field}.at [x, y] (a point on the wall) is required; 'offset' along the wall works only "
                                 "for walls created in the same call", f"{field}.at", [1500, 0])
            sx, sy, ex, ey = local["start_x"], local["start_y"], local["end_x"], local["end_y"]
            length = math.hypot(ex - sx, ey - sy)
            if not 0 <= offset <= length:
                raise validation(f"{field}.offset {offset:.0f} mm is outside the wall (length {length:.0f} mm)", f"{field}.offset", length / 2)
            at = [sx + (ex - sx) * offset / length, sy + (ey - sy) * offset / length]
        p["wall_uuid"] = wall_ref
        p["require_wall_host"] = True
        p["width"] = _len(src, ("width", "w"), f"{field}.width", scale, DEFAULTS_MM[f"{kind}_width"], positive=True)
        p["height"] = _len(src, ("height", "h"), f"{field}.height", scale, DEFAULTS_MM[f"{kind}_height"], positive=True)
        if kind == "window":
            p["sill_height"] = _len(src, ("sill", "sill_height", "elevation"), f"{field}.sill", scale, DEFAULTS_MM["window_sill"])
    else:
        if wall_ref is not None:
            p["wall_uuid"] = str(wall_ref)
            p["require_wall_host"] = True
    if at is None:
        at = [0.0, 0.0]
    p["x"] = at[0]
    p["y"] = at[1]
    p["rotation"] = C.number(_get(src, "rotation", "angle", default=0), f"{field}.rotation")
    params = _get(src, "parameters", "params", "values", "fields")
    if params is not None:
        p["parameters"] = parametric_parameters(C.parse_json_maybe(params), f"{field}.parameters", scale, schemas.get(plugin))
    return p


def parametric_parameters(params: Any, field: str, scale: float, schema: Optional[dict]) -> list[dict]:
    """{id: value} or [{id,type,value}] -> 0.7 typed parameter list (types inferred from the schema)."""
    styles: dict[str, int] = {}
    for entry in (schema or {}).get("parameters") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            styles[entry["id"]] = entry.get("field_style") if isinstance(entry.get("field_style"), int) else 0
    out: list[dict] = []
    items = params.items() if isinstance(params, dict) else None
    if items is None:
        if not isinstance(params, list):
            raise validation(f"{field} must be an object {{\"ParamId\": value}}", field, {"OpenAngle": 45})
        for entry in params:
            if not isinstance(entry, dict) or "id" not in entry:
                raise validation(f"{field} entries need id and value", field, [{"id": "OpenAngle", "value": 45}])
            if "type" in entry:
                out.append({"id": str(entry["id"]), "type": str(entry["type"]).lower(), "value": entry.get("value")})
            else:
                out.append(_typed(str(entry["id"]), entry.get("value"), styles, scale, field))
        return out
    for key, value in items:
        out.append(_typed(str(key), value, styles, scale, field))
    return out


def _typed(pid: str, value: Any, styles: dict[str, int], scale: float, field: str) -> dict:
    style = styles.get(pid)
    if style == 2 or isinstance(value, bool):
        return {"id": pid, "type": "boolean", "value": C.boolean(value, f"{field}.{pid}")}
    if style == 1:
        return {"id": pid, "type": "integer", "value": C.integer(value, f"{field}.{pid}")}
    if style in {3, 7, 9, 10, 13}:
        if style == 7 and isinstance(value, str) and any(ch.isalpha() or ch in "'\"" for ch in value):
            return {"id": pid, "type": "real", "value": C.length(value, f"{field}.{pid}", scale)}
        return {"id": pid, "type": "real", "value": C.number(value, f"{field}.{pid}")}
    if style is None and C.is_number(value):
        return {"id": pid, "type": "integer" if isinstance(value, int) else "real", "value": value}
    return {"id": pid, "type": "string", "value": str(value)}


def _room_objects(src: dict, field: str, scale: float, base_id: str) -> list[dict]:
    """Composite 'room': four walls + optional label + optional dimensions (all real objects)."""
    origin = _pt(src, ("origin", "at", "corner", "position"), ("x", "y"), f"{field}.origin", scale)
    width = _len(src, ("width", "w", "size_x"), f"{field}.width", scale, required=True, positive=True)
    depth = _len(src, ("depth", "height_2d", "length", "d", "size_y"), f"{field}.depth", scale, required=True, positive=True)
    thickness = _len(src, ("wall_thickness", "thickness"), f"{field}.wall_thickness", scale, DEFAULTS_MM["wall_thickness"], positive=True)
    height = _len(src, ("wall_height", "height"), f"{field}.wall_height", scale, DEFAULTS_MM["wall_height"], positive=True)
    name = str(_get(src, "name", "label", default="Room"))
    cls = _get(src, "class", "class_name")
    x, y = origin
    corners = [(x, y), (x + width, y), (x + width, y + depth), (x, y + depth)]
    out: list[dict] = []
    for i, side in enumerate(("south", "east", "north", "west")):
        a, b = corners[i], corners[(i + 1) % 4]
        wall = {"object_type": "wall", "start_x": a[0], "start_y": a[1], "end_x": b[0], "end_y": b[1], "height": height,
                "thickness": thickness, "name": f"{name} {side} wall"}
        if cls:
            wall["class_name"] = str(cls)
        style = _get(src, "wall_style", "style")
        if style:
            wall["style_name"] = str(style)
        wall = ops._normalise_create_primitive(wall, label=field, scale_to_mm=1.0)
        wall["_id"] = f"{base_id}_{side}"
        out.append(wall)
    if C.boolean(_get(src, "label_room", "show_label", default=True), f"{field}.label_room"):
        label = ops._normalise_create_primitive({"object_type": "text", "text": name, "x": x + width / 2, "y": y + depth / 2,
                                                 "width": width * 0.8, "name": name}, label=field, scale_to_mm=1.0)
        label["_id"] = f"{base_id}_label"
        out.append(label)
    if C.boolean(_get(src, "dimensions", "dimension_room", default=False), f"{field}.dimensions"):
        for suffix, end in (("width_dim", (x + width, y)), ("depth_dim", (x, y + depth))):
            dim = ops._normalise_create_primitive({"object_type": "linear_dimension", "start_x": x, "start_y": y,
                                                   "end_x": end[0], "end_y": end[1], "offset": -DEFAULTS_MM["dimension_offset"]},
                                                  label=field, scale_to_mm=1.0)
            dim["_id"] = f"{base_id}_{suffix}"
            out.append(dim)
    if C.boolean(_get(src, "space", "create_space", default=False), f"{field}.space"):
        space = ops._normalise_create_primitive({"object_type": "space", "points": [list(c) for c in corners], "name": name},
                                                label=field, scale_to_mm=1.0)
        space["_id"] = f"{base_id}_space"
        out.append(space)
    return out


def _example(kind: str) -> dict:
    examples = {
        "line": {"type": "line", "start": [0, 0], "end": [1000, 0]},
        "rect": {"type": "rect", "origin": [0, 0], "width": 1000, "height": 500},
        "oval": {"type": "oval", "center": [0, 0], "width": 1000, "height": 500},
        "circle": {"type": "circle", "center": [0, 0], "radius": 500},
        "arc": {"type": "arc", "center": [0, 0], "radius": 500, "start_angle": 0, "sweep_angle": 90},
        "polygon": {"type": "polygon", "points": [[0, 0], [1000, 0], [1000, 1000]]},
        "polyline": {"type": "polyline", "points": [[0, 0], [1000, 0], [1000, 1000]]},
        "wall": {"type": "wall", "id": "w1", "start": [0, 0], "end": [5000, 0], "thickness": 200, "height": 2700},
        "slab": {"type": "slab", "points": [[0, 0], [5000, 0], [5000, 4000], [0, 4000]], "thickness": 250},
        "roof": {"type": "roof", "points": [[0, 0], [5000, 0], [5000, 4000], [0, 4000]], "slope": 35},
        "space": {"type": "space", "points": [[0, 0], [5000, 0], [5000, 4000], [0, 4000]], "name": "Living"},
        "text": {"type": "text", "at": [0, 0], "text": "Living room"},
        "dimension": {"type": "dimension", "start": [0, 0], "end": [5000, 0], "offset": 500},
        "symbol": {"type": "symbol", "symbol": "Chair", "at": [1000, 1000]},
        "door": {"type": "door", "wall": "$w1", "at": [1500, 0], "width": 900},
        "window": {"type": "window", "wall": "$w1", "offset": 3000, "width": 1200, "sill": 900},
        "parametric": {"type": "parametric", "plugin": "Space", "at": [0, 0], "parameters": {"ParamId": "value"}},
        "room": {"type": "room", "origin": [0, 0], "width": 4000, "depth": 3000, "name": "Bedroom"},
    }
    return examples.get(kind, examples["rect"])


# ----------------------------------------------------------------------------
# vw_edit
# ----------------------------------------------------------------------------

def norm_ref(value: Any, field: str) -> str:
    """Object reference -> native ref string (uuid:/name:/handle:/$id)."""
    if isinstance(value, dict):
        for key in ("uuid", "name", "handle"):
            if value.get(key):
                return f"{key}:{value[key]}"
        raise validation(f"{field} must be a uuid, name or $id", field, "uuid:1234-...")
    ref = str(value or "").strip()
    if not ref:
        raise validation(f"{field} is required", field, "uuid:1234-...")
    if ref.startswith(("uuid:", "name:", "handle:", "$")):
        return ref
    low = ref.lower()
    for prefix in ("uuid=", "name=", "handle="):
        if low.startswith(prefix):
            return prefix[:-1] + ":" + ref[len(prefix):]
    if len(ref) >= 32 and all(ch in "0123456789abcdefABCDEF-{}" for ch in ref):
        return "uuid:" + ref.strip("{}")
    return "name:" + ref


class EditPlan:
    def __init__(self) -> None:
        self.ops: list[dict] = []          # 0.7 normalised ops (native path)
        self.script_ops: list[dict] = []   # every change in script form (used when any op needs a script)
        self.needs_script = False
        self.needs_selection = False
        self.selection_ops: list[dict] = []  # select/deselect (native 'selection' action)
        self.warnings: list[str] = []


def _targets(src: dict, field: str) -> list[str]:
    value = _get(src, "targets", "target", "objects", "object", "ref", "refs", "uuid", "uuids", "name")
    value = C.parse_json_maybe(value)
    if value is None:
        raise validation(f"{field}.target is required (uuid, name, \"$id\" or \"selection\")", f"{field}.target", "uuid:...")
    if not isinstance(value, list):
        value = [value]
    return [v if isinstance(v, str) and v.lower() == "selection" else norm_ref(v, f"{field}.target") for v in value]


def compile_edit(changes: list[dict], scale: float, *, schemas: dict[str, dict], descriptors: dict[str, str]) -> EditPlan:
    plan = EditPlan()
    for index, raw in enumerate(changes):
        field = f"changes[{index}]"
        src = C.lower_keys(raw)
        op = C.choice(_get(src, "op", "type", "action", "operation"), f"{field}.op", EDIT_OPS, EDIT_SYNONYMS)
        if op in {"select", "deselect"}:
            plan.selection_ops.append({"op": op, "criteria": _get(src, "criteria"),
                                       "targets": _targets(src, field) if op == "select" and not _get(src, "criteria") else []})
            continue
        targets = _targets(src, field)
        if any(t == "selection" for t in targets):
            plan.needs_selection = True
        script_item: dict[str, Any] = {"op": op, "targets": targets}
        native: list[dict] = []
        if op == "set" or op == "class":
            props_src = _get(src, "properties", "props", "set", "values")
            props_src = C.parse_json_maybe(props_src) if props_src is not None else {
                k: v for k, v in src.items() if k in PROPERTY_SYNONYMS}
            if op == "class":
                props_src = {"class": _get(src, "class", "class_name", "value", "to")}
            if not isinstance(props_src, dict) or not props_src:
                raise validation(f"{field}.properties is required", f"{field}.properties", {"fill_color": "#FF0000"})
            props: dict[str, str] = {}
            for key, value in props_src.items():
                canonical = PROPERTY_SYNONYMS.get(str(key).lower().replace(" ", "_"))
                if canonical is None:
                    raise validation(f"{field}.properties.{key} is not editable; use name, class, fill_color, pen_color, "
                                     "fill_pattern, line_weight, opacity (or op='parametric' / 'record')",
                                     f"{field}.properties", {"fill_color": "#FF0000"})
                if canonical in {"fillColor", "penColor"}:
                    value = C.color(value, f"{field}.properties.{key}")
                elif canonical in {"fillPattern", "lineWeight", "opacity"}:
                    value = str(C.integer(value, f"{field}.properties.{key}"))
                props[canonical] = str(value)
            script_item["properties"] = props
            native = [{"type": "set_properties", "params": {"edits": [{"ref": t, "properties": props} for t in targets]}}]
        elif op == "move":
            delta = _get(src, "by", "delta", "offset", "vector")
            if delta is not None:
                d = C.point(delta, f"{field}.by", scale)
            else:
                d = [_len(src, ("dx", "x"), f"{field}.dx", scale, 0.0), _len(src, ("dy", "y"), f"{field}.dy", scale, 0.0)]
            if d == [0.0, 0.0]:
                raise validation(f"{field} needs by=[dx, dy]", f"{field}.by", [1000, 0])
            script_item["by"] = d
            native = [{"type": "transform", "params": {"target": t, "dx": d[0], "dy": d[1]}} for t in targets]
        elif op == "rotate":
            angle = C.number(_get(src, "angle", "degrees", "rotation", "by"), f"{field}.angle")
            center = _pt(src, ("center", "pivot", "about", "origin"), ("pivot_x", "pivot_y"), f"{field}.center", scale, required=False)
            script_item.update({"angle": angle, "center": center})
            for t in targets:
                params: dict[str, Any] = {"target": t, "rotation_deg": angle}
                if center:
                    params.update({"pivot_x": center[0], "pivot_y": center[1]})
                native.append({"type": "transform", "params": params})
        elif op == "scale":
            factor = _get(src, "factor", "scale", "by")
            sx = C.number(_get(src, "sx", "scale_x", default=factor if factor is not None else 1), f"{field}.sx")
            sy = C.number(_get(src, "sy", "scale_y", default=factor if factor is not None else 1), f"{field}.sy")
            if sx <= 0 or sy <= 0:
                raise validation(f"{field} scale factors must be > 0 (use op='mirror' to flip)", f"{field}.factor", 2)
            center = _pt(src, ("center", "pivot", "about", "origin"), ("pivot_x", "pivot_y"), f"{field}.center", scale, required=False)
            script_item.update({"sx": sx, "sy": sy, "center": center})
            for t in targets:
                params = {"target": t, "scale_x": sx, "scale_y": sy}
                if center:
                    params.update({"pivot_x": center[0], "pivot_y": center[1]})
                native.append({"type": "transform", "params": params})
        elif op == "mirror":
            a = _pt(src, ("axis_start", "from", "p1", "start"), ("x1", "y1"), f"{field}.axis_start", scale, required=False)
            b = _pt(src, ("axis_end", "to", "p2", "end"), ("x2", "y2"), f"{field}.axis_end", scale, required=False)
            axis = str(_get(src, "axis", default="")).lower()
            if a is None or b is None:
                center = _pt(src, ("center", "about", "origin"), ("cx", "cy"), f"{field}.center", scale, required=False) or [0.0, 0.0]
                if axis in {"x", "horizontal"}:
                    a, b = center, [center[0] + 1000, center[1]]
                elif axis in {"y", "vertical"}:
                    a, b = center, [center[0], center[1] + 1000]
                else:
                    raise validation(f"{field} needs axis 'x'/'y' (+center) or axis_start/axis_end points", f"{field}.axis", "y")
            script_item.update({"axis_start": a, "axis_end": b, "copy": C.boolean(_get(src, "copy", default=False), f"{field}.copy")})
        elif op == "reshape":
            a = _pt(src, ("start", "from", "p1"), ("start_x", "start_y"), f"{field}.start", scale)
            b = _pt(src, ("end", "to", "p2"), ("end_x", "end_y"), f"{field}.end", scale)
            script_item.update({"start": a, "end": b})
            native = [{"type": "reshape", "params": {"target": t, "start_x": a[0], "start_y": a[1], "end_x": b[0], "end_y": b[1]}}
                      for t in targets]
        elif op == "duplicate":
            delta = _get(src, "by", "delta", "offset")
            d = C.point(delta, f"{field}.by", scale) if delta is not None else [
                _len(src, ("dx",), f"{field}.dx", scale, 0.0), _len(src, ("dy",), f"{field}.dy", scale, 0.0)]
            count = C.integer(_get(src, "count", "copies", default=1), f"{field}.count")
            if not 1 <= count <= 100:
                raise validation(f"{field}.count must be 1..100", f"{field}.count", 1)
            base_id = str(_get(src, "id", "as", default=f"dup{index + 1}"))
            script_item.update({"by": d, "count": count})
            n = 0
            for t in targets:
                for k in range(1, count + 1):
                    n += 1
                    native.append({"type": "duplicate", "operation_id": f"{base_id}_{n}" if len(targets) * count > 1 else base_id,
                                   "params": {"target": t, "dx": d[0] * k, "dy": d[1] * k}})
        elif op == "delete":
            native = [{"type": "delete", "params": {"target": t}} for t in targets]
            if any(t.startswith("$") for t in targets):
                raise validation(f"{field} delete needs existing objects (uuid/name), not $ids", f"{field}.target", "uuid:...")
        elif op == "parametric":
            params_src = _get(src, "parameters", "params", "values", "set")
            plugin = _get(src, "plugin", "plugin_name")
            if params_src is None or plugin is None:
                raise validation(f"{field} needs plugin (e.g. 'Door') and parameters {{id: value}}", f"{field}.parameters",
                                 {"op": "parametric", "target": "uuid:...", "plugin": "Door", "parameters": {"OpenAngle": 45}})
            plugin = str(plugin)
            parameters = parametric_parameters(C.parse_json_maybe(params_src), f"{field}.parameters", scale, schemas.get(plugin))
            fingerprint = _get(src, "descriptor_fingerprint") or descriptors.get(plugin) or "pending"
            script_item.update({"plugin": plugin, "parameters": {p["id"]: p["value"] for p in parameters}})
            native = [{"type": "update_parametric", "params": {"target": t, "plugin_name": plugin,
                                                               "descriptor_fingerprint": fingerprint, "parameters": parameters}}
                      for t in targets]
        elif op == "layer":
            script_item["layer"] = C.text(_get(src, "layer", "to", "value"), f"{field}.layer", required=True)
        elif op == "group":
            script_item["name"] = _get(src, "name", "group_name")
        elif op == "ungroup":
            pass
        elif op == "record":
            script_item["record"] = C.text(_get(src, "record", "format", "record_name"), f"{field}.record", required=True)
            fields = C.parse_json_maybe(_get(src, "fields", "values", "set", default={}))
            if not isinstance(fields, dict):
                raise validation(f"{field}.fields must be an object", f"{field}.fields", {"Cost": "120"})
            script_item["fields"] = {str(k): "" if v is None else str(v) for k, v in fields.items()}
            script_item["detach"] = C.boolean(_get(src, "detach", "remove", default=False), f"{field}.detach")
        plan.script_ops.append(script_item)
        if op in SCRIPT_EDIT_OPS:
            plan.needs_script = True
        else:
            plan.ops.extend(native)
    return plan


def expand_selection(plan: EditPlan, selection_refs: list[str]) -> None:
    """Replace the 'selection' target with the selected objects' uuids."""
    def fix(targets: list[str]) -> list[str]:
        out: list[str] = []
        for t in targets:
            out.extend(selection_refs if t == "selection" else [t])
        return out
    for item in plan.script_ops:
        item["targets"] = fix(item["targets"])
    new_ops = []
    for op in plan.ops:
        params = op["params"]
        if params.get("target") == "selection":
            for ref in selection_refs:
                new_ops.append({**op, "params": {**params, "target": ref}})
        elif "edits" in params:
            edits = []
            for e in params["edits"]:
                if e["ref"] == "selection":
                    edits.extend({**e, "ref": r} for r in selection_refs)
                else:
                    edits.append(e)
            new_ops.append({**op, "params": {"edits": edits}})
        else:
            new_ops.append(op)
    plan.ops = new_ops


def normalise_edit_ops(native_ops: list[dict]) -> list[dict]:
    """Run the 0.7 normaliser on already-mm ops (validation + canonical form)."""
    if not native_ops:
        return []
    try:
        return ops._normalise_execute_operations(native_ops, coordinate_units="mm")
    except ValueError as exc:
        raise VWError("validation_error", str(exc), field="changes") from None
