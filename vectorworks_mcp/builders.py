"""vw_create / vw_edit compilers: friendly flat objects -> 0.7 normalised
operations -> native ``apply_operations`` wire ops.

Everything is converted to millimetres here; the 0.7 normaliser then runs with
scale 1 so its validation (types, ranges, wall-host rules) still applies.
"""

from __future__ import annotations

import math
import re
from typing import Any, Optional

from . import coerce as C
from . import ops
from .errors import VWError, validation

CREATE_SYNONYMS = {
    "rectangle": "rect", "box": "rect", "square": "rect", "ellipse": "oval", "polyline": "polyline",
    "poly": "polygon", "polygon2d": "polygon", "linear_dimension": "dimension", "dim": "dimension",
    "label": "text", "note": "text", "room_space": "space", "floor": "slab", "symbol_instance": "symbol",
    "plugin": "parametric", "pio": "parametric", "segment": "line", "schematic_room": "room",
    "extrusion": "extrude", "mass": "extrude", "massing": "extrude", "block": "extrude", "volume": "extrude",
    "pillar": "column", "post": "column",
}
CREATE_TYPES = ["line", "rect", "oval", "circle", "arc", "polygon", "polyline", "wall", "slab", "roof", "space",
                "text", "dimension", "symbol", "door", "window", "parametric", "room", "extrude", "column"]
SCRIPT_CREATE_TYPES = {"extrude", "column"}  # created by the vetted `massing` run_script snippet

DEFAULTS_MM = {"wall_height": 3000.0, "wall_thickness": 200.0, "slab_thickness": 200.0, "door_width": 900.0,
               "door_height": 2100.0, "window_width": 1200.0, "window_height": 1500.0, "window_sill": 900.0,
               "dimension_offset": 300.0, "roof_overhang": 500.0}

# Door/Window parameters that duplicate the dedicated width/height/sill fields (compared lower-case, letters and
# digits only). Weak models send e.g. {"DoorWidth": "900"}; DoorWidth is the read-only leaf width in Vectorworks,
# so setting it fails the native readback. They are mapped onto the dedicated fields instead.
OPENING_PARAM_ALIASES = {
    "width": {"width", "doorwidth", "windowwidth", "overallwidth", "leafwidth", "openingwidth", "w"},
    "height": {"height", "doorheight", "windowheight", "overallheight", "leafheight", "openingheight", "h"},
    "sill": {"sill", "sillheight", "sillelevation", "elevation", "sillheightabovefloor"},
}
# Friendly parameter names -> real parameter ids per plug-in (compared lower-case, letters and digits only). They win
# over the schema's own id/display-name matching (e.g. Window "sill_height" is the sill ELEVATION, not the
# "SillHeight" board parameter). Verified live (VW 2024): Window Elevation + ElevationSetAt "Sill of Window" | "Head
# of Window"; Door SwingDirection In|Out, SwingHingeSide Left|Right, Configuration Swing|Sliding|Pocket|...
FRIENDLY_PARAMS: dict[str, dict[str, str]] = {
    "Door": {"width": "Width", "w": "Width", "doorwidth": "Width", "overallwidth": "Width", "height": "Height",
             "h": "Height", "doorheight": "Height", "overallheight": "Height", "openangle": "OpenAngle",
             "openingangle": "OpenAngle", "angle": "OpenAngle", "swing": "SwingDirection",
             "swingdirection": "SwingDirection", "opens": "SwingDirection", "hinge": "SwingHingeSide",
             "hingeside": "SwingHingeSide", "handing": "SwingHingeSide", "hand": "SwingHingeSide",
             "configuration": "Configuration", "config": "Configuration", "doortype": "Configuration",
             "operation": "SwingOperation", "leaves": "SwingOperation", "leaftype": "LeafType"},
    "Window": {"width": "Width", "w": "Width", "windowwidth": "Width", "overallwidth": "Width", "height": "Height",
               "h": "Height", "windowheight": "Height", "overallheight": "Height", "sill": "Elevation",
               "sillheight": "Elevation", "sillelevation": "Elevation", "elevation": "Elevation",
               "openangle": "OpenAngle", "configuration": "Configuration", "config": "Configuration",
               "windowtype": "Configuration"},
    "Space": {"roomid": "11_Room ID", "id": "11_Room ID", "netheight": "11_Net Height",
              "grossheight": "11_Gross Height", "height": "11_Gross Height"},
    "Column2": {"height": "OA Height", "width": "Shaft Width", "depth": "Shaft Depth", "diameter": "Diameter"},
}
FRIENDLY_PARAMS["Column"] = FRIENDLY_PARAMS["Column2"]
# The friendly names shown in vw_read(what="parametric_schema") (all spellings above work).
FRIENDLY_SHOWN: dict[str, dict[str, str]] = {
    "Door": {"width": "Width", "height": "Height", "open_angle": "OpenAngle", "swing": "SwingDirection",
             "hinge": "SwingHingeSide", "configuration": "Configuration", "operation": "SwingOperation"},
    "Window": {"width": "Width", "height": "Height", "sill_height": "Elevation", "configuration": "Configuration",
               "open_angle": "OpenAngle"},
    "Space": {"name": "(displayed room name)", "room_id": "11_Room ID", "height": "11_Gross Height"},
    "Column2": {"height": "OA Height", "width": "Shaft Width", "depth": "Shaft Depth", "diameter": "Diameter"},
}
# Keys that rename a Space's displayed room name (label) instead of a parameter.
SPACE_NAME_KEYS = {"name", "roomname", "label", "spacename", "title"}

_UUID_RE = re.compile(r"^\{?[0-9A-Fa-f]{8}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{4}-?[0-9A-Fa-f]{12}\}?$")


def _semantic(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def is_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value.strip()))

EDIT_OPS = ["set", "move", "rotate", "scale", "mirror", "reshape", "duplicate", "delete", "parametric", "class",
            "layer", "order", "group", "ungroup", "record", "select", "deselect"]
ORDER_POSITIONS = {"front": "front", "back": "back", "forward": "forward", "backward": "backward", "top": "front",
                   "bottom": "back", "to_front": "front", "to_back": "back", "bring_to_front": "front",
                   "send_to_back": "back", "bring_forward": "forward", "send_backward": "backward", "up": "forward",
                   "down": "backward", "behind": "back", "back_most": "back", "front_most": "front"}
# op synonyms that already say where to move (vw_edit op="send_to_back")
ORDER_OP_POSITIONS = {"send_to_back": "back", "bring_to_front": "front", "send_backward": "backward",
                      "bring_forward": "forward", "to_back": "back", "to_front": "front", "move_to_back": "back",
                      "move_to_front": "front"}
EDIT_SYNONYMS = {"translate": "move", "shift": "move", "transform": "move", "copy": "duplicate", "clone": "duplicate",
                 "remove": "delete", "erase": "delete", "set_properties": "set", "set_property": "set", "properties": "set",
                 "update": "set", "update_parametric": "parametric", "params": "parametric", "set_class": "class",
                 "move_to_layer": "layer", "set_layer": "layer", "flip": "mirror", "resize": "scale", "turn": "rotate",
                 "attach_record": "record", "set_record": "record", "unselect": "deselect", "clear_selection": "deselect",
                 "arrange": "order", "stack": "order", "stacking": "order", "z_order": "order", "send_to_back": "order",
                 "bring_to_front": "order", "send_backward": "order", "bring_forward": "order", "to_back": "order",
                 "to_front": "order", "move_to_back": "order", "move_to_front": "order"}
PROPERTY_SYNONYMS = {"name": "name", "class": "class", "class_name": "class", "fill_color": "fillColor",
                     "fillcolor": "fillColor", "fill": "fillColor", "pen_color": "penColor", "pencolor": "penColor",
                     "pen": "penColor", "stroke": "penColor", "line_color": "penColor", "fill_pattern": "fillPattern",
                     "fillpattern": "fillPattern", "line_weight": "lineWeight", "lineweight": "lineWeight",
                     "pen_weight": "lineWeight", "opacity": "opacity"}
SCRIPT_EDIT_OPS = {"mirror", "layer", "order", "group", "ungroup", "record", "style", "pio", "wall", "text", "space_name"}

# Graphic attributes the native set_properties lacks; applied + read back by the `_style` snippet helper.
STYLE_SYNONYMS = {
    "line_style": "line_style", "linestyle": "line_style", "line_type": "line_style", "linetype": "line_style",
    "dash": "line_style", "dash_style": "line_style", "line_pattern": "line_style",
    "fill": "fill", "fill_style": "fill", "hatch": "fill", "gradient": "fill", "image_fill": "fill", "tile": "fill",
    "font": "font", "font_name": "font", "text_font": "font", "typeface": "font",
    "text_size": "text_size", "font_size": "text_size", "size_pt": "text_size", "point_size": "text_size",
    "text_style": "text_style", "font_style": "text_style", "face": "text_style", "bold": "text_style",
    "italic": "text_style", "underline": "text_style", "text_style_name": "text_style_name",
    "text_style_resource": "text_style_name",
    "align": "align", "alignment": "align", "text_align": "align", "justify": "align", "justification": "align",
    "horizontal_alignment": "align", "valign": "valign", "vertical_align": "valign", "vertical_alignment": "valign",
    "start_marker": "start_marker", "begin_marker": "start_marker", "start_arrow": "start_marker",
    "arrow_start": "start_marker", "end_marker": "end_marker", "end_arrow": "end_marker", "arrow_end": "end_marker",
    "arrowhead": "end_marker", "arrows": "arrows", "arrowheads": "arrows", "markers": "arrows",
}
# Basic attributes, also accepted as style keys where the object is made or finished by a script (vw_create extras,
# massing, viewport annotations).
BASIC_STYLE = {"pen_color": "pen_color", "pen": "pen_color", "stroke": "pen_color", "line_color": "pen_color",
               "fill_color": "fill_color", "line_weight": "line_weight", "pen_weight": "line_weight",
               "opacity": "opacity", "class": "class", "class_name": "class", "name": "name"}
TEXT_FACES = {"plain": 0, "normal": 0, "regular": 0, "none": 0, "bold": 1, "italic": 2, "underline": 4,
              "outline": 8, "shadow": 16}
ALIGN = {"left": 1, "center": 2, "centre": 2, "middle": 2, "right": 3, "justify": 4, "justified": 4}
VALIGN = {"top": 1, "top_baseline": 2, "center": 3, "centre": 3, "middle": 3, "baseline": 4, "bottom_baseline": 4,
          "bottom": 5}
MARKER_PRESETS = ["arrow", "open_arrow", "dot", "circle", "slash", "tick", "cross"]
FILL_KEYS = {"hatch": "hatch", "gradient": "gradient", "image_fill": "image", "image": "image", "tile": "tile"}


def _marker(value: Any, field: str) -> dict:
    value = C.parse_json_maybe(value)
    if isinstance(value, bool) or (C.is_number(value) and value in (0, 1)):
        return {"preset": "arrow", "visible": bool(value)}
    if isinstance(value, str):
        name = value.strip().lower().replace(" ", "_").replace("-", "_")
        if name in {"none", "off", "no", "false", ""}:
            return {"preset": "arrow", "visible": False}
        if name == "true":
            return {"preset": "arrow", "visible": True}
        name = {"arrowhead": "arrow", "filled_arrow": "arrow", "open": "open_arrow", "oblique": "slash",
                "architectural": "slash", "point": "dot", "x": "cross"}.get(name, name)
        if name not in MARKER_PRESETS:
            raise validation(f"{field} must be one of {MARKER_PRESETS} or none", field, "arrow")
        return {"preset": name, "visible": True}
    if isinstance(value, dict):
        v = C.lower_keys(value)
        out = _marker(v.get("style") or v.get("preset") or v.get("type") or "arrow", field)
        if v.get("size") not in (None, ""):
            out["size_in"] = C.number(v["size"], f"{field}.size") / 25.4  # page mm -> page inches
        if v.get("visible") is not None:
            out["visible"] = C.boolean(v["visible"], f"{field}.visible")
        return out
    raise validation(f"{field} must be a marker name like 'arrow', true/false or {{style, size}}", field, "arrow")


_UNIT_RE = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*(mm|cm|m|in|ft|\"|')\s*$", re.I)


def pio_parameters(raw: Any, field: str) -> list[dict]:
    """{field: value} for the parametric-record fallback. Numbers are in `units` (the snippet converts them for
    length fields); strings with a unit ("9m") are converted to mm here; everything else is sent as text."""
    raw = C.parse_json_maybe(raw)
    if isinstance(raw, list):
        raw = {str(e.get("id")): e.get("value") for e in raw if isinstance(e, dict) and "id" in e}
    if not isinstance(raw, dict) or not raw:
        raise validation(f"{field} must be an object {{\"Field\": value}}", field, {"OA Height": 3000})
    out = []
    for key, value in raw.items():
        if isinstance(value, str) and _UNIT_RE.match(value):
            out.append({"id": str(key), "value": C.length(value, f"{field}.{key}", 1.0), "mm": True})
        else:
            out.append({"id": str(key), "value": value})
    return out


def _is_color(value: Any) -> bool:
    try:
        C.color(value, "fill")
        return True
    except VWError:
        return False


def style_props(src: dict, field: str, *, basic: bool = False, skip: tuple = ()) -> dict:
    """Friendly style keys of one object/properties dict -> normalised style for the `_style` snippet helper.

    basic=True also takes pen/fill colour, line weight, opacity, class and name (for script-made objects)."""
    out: dict[str, Any] = {}
    faces: Optional[int] = None
    for key, value in src.items():
        k = str(key).lower().replace(" ", "_")
        if k in skip:
            continue
        canonical = STYLE_SYNONYMS.get(k) or (BASIC_STYLE.get(k) if basic else None)
        if canonical is None or value is None:
            continue
        label = f"{field}.{key}"
        if canonical == "line_style":
            if isinstance(value, bool):
                raise validation(f"{label} must be a line type name or index", label, "Dash 1")
            if C.is_number(value) or (isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip())):
                out["line_style"] = {"kind": "index", "value": int(float(value))}
            elif str(value).strip().lower() in {"solid", "none"}:
                out["line_style"] = {"kind": "index", "value": 2 if str(value).strip().lower() == "solid" else 0}
            else:
                out["line_style"] = {"kind": "name", "name": C.text(value, label, required=True, max_len=255)}
        elif canonical == "fill":
            kind_hint = FILL_KEYS.get(k)
            value = C.parse_json_maybe(value)
            if isinstance(value, dict):
                v = C.lower_keys(value)
                kind_word = str(v.get("type") or v.get("kind") or "").strip().lower()
                if kind_word in {"none", "solid"} and not v.get("name"):
                    v = {}
                    value = kind_word
                elif kind_word and kind_word in FILL_KEYS:
                    # {"type": "hatch", "name": "Brick"} (a common model spelling) = {"hatch": "Brick"}
                    v = {kind_word: v.get("name") or True}
                hit = next(((FILL_KEYS[n], v[n]) for n in FILL_KEYS if v.get(n)), None)
                if hit is None and isinstance(value, str):
                    out["fill"] = {"kind": value}
                    continue
                if hit is None:
                    raise validation(f"{label} must be {{\"hatch\": name}} / gradient / image / tile", label,
                                     {"hatch": "Brick"})
                # {"gradient": true} = any gradient (the script picks the first available one)
                out["fill"] = {"kind": hit[0], "name": "" if hit[1] is True else str(hit[1])}
            elif value is False or str(value).strip().lower() in {"none", "no", "empty", "hollow", "transparent"}:
                out["fill"] = {"kind": "none"}
            elif value is True or str(value).strip().lower() == "solid":
                out["fill"] = {"kind": "solid"}
            else:
                out["fill"] = {"kind": kind_hint or "auto", "name": C.text(value, label, required=True, max_len=255)}
        elif canonical == "text_size":
            raw = str(value).strip().lower().removesuffix("pt").strip() if isinstance(value, str) else value
            size = C.number(raw, label)
            if not 0 < size <= 2000:
                raise validation(f"{label} must be a point size > 0", label, 12)
            out["text_size"] = size
        elif canonical == "text_style":
            if k in {"bold", "italic", "underline"}:
                faces = (faces or 0) | (TEXT_FACES[k] if C.boolean(value, label) else 0)
                continue
            if C.is_number(value):
                faces = (faces or 0) | int(value)
                continue
            words = [w for w in re.split(r"[\s,+|/_-]+", str(value).strip().lower()) if w]
            if words and all(w in TEXT_FACES for w in words):
                faces = (faces or 0) | sum(TEXT_FACES[w] for w in set(words))
            else:
                out["text_style_name"] = C.text(value, label, required=True, max_len=255)
        elif canonical == "text_style_name":
            out["text_style_name"] = C.text(value, label, required=True, max_len=255)
        elif canonical == "font":
            out["font"] = C.text(value, label, required=True, max_len=255)
        elif canonical in {"align", "valign"}:
            table = ALIGN if canonical == "align" else VALIGN
            if C.is_number(value):
                out[canonical] = int(value)
            else:
                name = str(value).strip().lower().replace(" ", "_")
                if name not in table:
                    raise validation(f"{label} must be one of {sorted(table)}", label, "center")
                out[canonical] = table[name]
        elif canonical in {"start_marker", "end_marker"}:
            out[canonical] = _marker(value, label)
        elif canonical == "arrows":
            where = str(value).strip().lower()
            if where not in {"start", "end", "both", "none"}:
                raise validation(f"{label} must be start, end, both or none", label, "end")
            out["start_marker"] = {"preset": "arrow", "visible": where in {"start", "both"}}
            out["end_marker"] = {"preset": "arrow", "visible": where in {"end", "both"}}
        elif canonical in {"pen_color", "fill_color"}:
            out[canonical] = [int(c) for c in C.color(value, label).split(",")]
        elif canonical in {"line_weight", "opacity"}:
            out[canonical] = C.integer(value, label)
        elif canonical in {"class", "name"}:
            out[canonical] = C.text(value, label, required=True, max_len=255)
    if faces is not None:
        out["text_style"] = faces
    return out


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
        self.wall_ids: set[str] = set()     # ids of walls declared anywhere in this call
        self.doc_mm_per_unit = 1.0
        self.script_objects: list[dict] = []  # extrude / column -> `massing` snippet
        self.styles: list[tuple[str, dict]] = []  # (local id, style) applied by script after the native create
        self.layers: dict[str, str] = {}      # local id -> requested layer name (per-object `layer`)
        self.kinds: dict[str, str] = {}       # local id -> friendly kind (space, wall, door, ...)
        self.index_of: dict[str, float] = {}  # local id -> position in the caller's objects list
        self.fixups: list[dict] = []          # normalised ops run after the creates in the same transaction
        self.opening_flags: dict[str, dict] = {}  # door/window id -> {"flip", "right"} for vs.AddSymToWall


def wall_ids_of(objects: list[dict]) -> set[str]:
    """Local ids of the walls in a vw_create objects list (for door/window wall refs)."""
    out: set[str] = set()
    for index, raw in enumerate(objects):
        if not isinstance(raw, dict):
            continue
        src = C.lower_keys(raw)
        kind = str(_get(src, "type", "object_type", "kind", "op", default="")).strip().lower()
        local_id = _get(src, "id", "operation_id", "ref")
        if kind == "wall" and local_id is not None:
            out.add(str(local_id).lstrip("$"))
        elif kind in {"room", "schematic_room"}:
            base = str(local_id).lstrip("$") if local_id is not None else f"o{index + 1}"
            out.update(f"{base}_{side}" for side in ("south", "east", "north", "west"))
    return out


def _effective_kind(kind: str, src: dict) -> str:
    """{"type":"parametric","plugin":"Door","wall":...} is a door: use the door path (dedicated width/height)."""
    if kind == "parametric":
        plugin = str(_get(src, "plugin", "plugin_name", "object", "pio", default="")).strip().lower()
        if plugin in {"door", "window"} and _get(src, "wall", "wall_uuid", "host", "in_wall") is not None:
            return plugin
    return kind


def compile_create(objects: list[dict], scale: float, *, descriptors: dict[str, str],
                   schemas: dict[str, dict], extra_wall_ids: Optional[set[str]] = None,
                   doc_mm_per_unit: float = 1.0) -> CreatePlan:
    plan = CreatePlan()
    plan.wall_ids = wall_ids_of(objects) | set(extra_wall_ids or ())
    plan.doc_mm_per_unit = doc_mm_per_unit or 1.0
    seen_ids: set[str] = set()
    # Walls first, so doors/windows may reference a wall listed after them (offset needs its geometry).
    order = sorted(range(len(objects)), key=lambda i: 0 if isinstance(objects[i], dict) and str(
        C.lower_keys(objects[i]).get("type") or "").strip().lower() == "wall" else 1)
    for index in order:
        raw = objects[index]
        field = f"objects[{index}]"
        src = C.lower_keys(raw)
        kind = C.choice(_get(src, "type", "object_type", "kind", "op"), f"{field}.type", CREATE_TYPES, CREATE_SYNONYMS)
        kind = _effective_kind(kind, src)
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
        layer = _get(src, "layer", "layer_name", "on_layer")
        layer = C.text(layer, f"{field}.layer", max_len=255).strip() if layer is not None else None
        if kind == "room":
            for k, sub in enumerate(_room_objects(src, field, scale, local_id)):
                sub_id = sub.pop("_id")
                _append(plan, sub, sub_id)
                plan.index_of[sub_id] = index + k / 100.0
                plan.kinds[sub_id] = sub.get("object_type", "")
                if layer:
                    plan.layers[sub_id] = layer
            continue
        plan.kinds[local_id] = kind
        plan.index_of[local_id] = index
        if layer:
            plan.layers[local_id] = layer
        plugin_name = str(_get(src, "plugin", "plugin_name", "object", "pio", default="")).strip()
        if kind == "parametric" and (schemas.get(plugin_name) or {}).get("script_only"):
            at = _pt(src, ("at", "position", "location", "point", "center"), ("x", "y"), f"{field}.at", scale,
                     required=False) or [0.0, 0.0]
            raw = _get(src, "parameters", "params", "values", "fields")
            item = {"kind": "pio", "plugin": plugin_name, "at": at, "id": local_id, "scale_mm": scale,
                    "rotation": C.number(_get(src, "rotation", "angle", default=0), f"{field}.rotation"),
                    "parameters": pio_parameters(raw, f"{field}.parameters") if raw not in (None, "", {}) else []}
            style = style_props(src, field, basic=True)
            if style:
                item["style"] = style
            if layer:
                item["layer"] = layer
            plan.script_objects.append(item)
            plan.ids.append(local_id)
            plan.warnings.append(f"{field}: {plugin_name} has no native schema provider; created through its "
                                 "parametric record (script).")
            continue
        if kind in SCRIPT_CREATE_TYPES:
            item = _massing_params(kind, src, field, scale)
            item["id"] = local_id
            style = style_props(src, field, basic=True)
            if style:
                item["style"] = style
            if layer:
                item["layer"] = layer
            plan.script_objects.append(item)
            plan.ids.append(local_id)
            continue
        if kind in {"door", "window"}:
            src, plan.opening_flags[local_id] = opening_options(kind, src, field, plan.warnings)
        params = _create_params(kind, src, field, scale, plan, descriptors, schemas)
        if kind == "wall":
            plan.local_walls[local_id] = params
        _append(plan, params, local_id)
        if kind == "window" and "sill_height" in params:
            plan.fixups.append(_window_sill_fixup(local_id, params, plan.doc_mm_per_unit))
        style = style_props(src, field, basic=True, skip=("name", "class", "class_name"))
        if style:
            plan.styles.append((local_id, style))
    return plan


def _window_sill_fixup(local_id: str, params: dict, doc_mm_per_unit: float) -> dict:
    """Live (VW 2024, bridge 1.0.0): the native window create writes the sill into 'Elevation' but leaves
    'Elevation Set At' = 'Head of Window', so the window HEAD sits at the sill height (window below the floor).
    Same transaction: set 'Sill of Window' and the elevation again (harmless when the bridge already does it)."""
    return {"type": "update_parametric", "params": {
        "target": "$" + local_id, "plugin_name": "Window",
        "descriptor_fingerprint": params.get("descriptor_fingerprint") or "pending",
        "parameters": [{"id": "ElevationSetAt", "type": "string", "value": "Sill of Window"},
                       {"id": "Elevation", "type": "real", "value": float(params["sill_height"]) / (doc_mm_per_unit or 1.0)}]}}


# Flat door/window keys of a vw_create object that are plug-in parameters (semantic spelling; mapped through
# FRIENDLY_PARAMS). "angle" is NOT one of them: it is the rotation alias.
OPENING_FLAT_KEYS = {"openangle", "openingangle", "swing", "swingdirection", "opens", "hinge", "hingeside", "handing",
                     "configuration", "config", "doortype", "windowtype", "operation", "leaves", "leaftype"}
# Which side of the wall (looking from the wall's start to its end) the door leaf swings into.
OPENING_SIDE_KEYS = {"side", "swingside", "openside", "opensto", "opensinto", "swingto"}
OPENING_SIDES = {"left": False, "right": True, "l": False, "r": True}


def opening_options(kind: str, src: dict, field: str, warnings: list) -> tuple[dict, dict]:
    """Flat door/window options of a vw_create object -> (src with them folded into `parameters`, AddSymToWall flags).

    Live (VW 2024, AddSymToWall into a wall drawn from start to end): right=False puts the leaf on the LEFT side of
    the wall, right=True on the right side (the door is turned 180 degrees); flip mirrors the hinge to the other
    jamb. With right=False the hinge is at the jamb nearer the wall START, flip=True moves it to the END jamb (and the
    other way round with right=True). Friendly keys:
      side / swing_side / opens_to: left|right   (swing: left|right is read the same way)
      hinge: start|end (jamb nearer the wall start/end); hinge: Left|Right stays the Door's SwingHingeSide parameter
      flip, right_side: explicit booleans (win over the derived values)
      swing: In|Out, open_angle, configuration, ... -> plug-in parameters (FRIENDLY_PARAMS)."""
    plugin = "Door" if kind == "door" else "Window"
    table = FRIENDLY_PARAMS.get(plugin, {})
    out = dict(src)
    extra: dict[str, Any] = {}
    side = hinge_end = None
    flip = right = None
    for key, value in src.items():
        sem = _semantic(key)
        label = f"{field}.{key}"
        if sem in OPENING_SIDE_KEYS:
            side = value
        elif sem in {"flip", "flipped", "mirror", "mirrored"}:
            flip = C.boolean(value, label)
        elif sem in {"rightside", "right"}:
            right = C.boolean(value, label)
        elif sem in OPENING_FLAT_KEYS:
            word = value.strip().lower() if isinstance(value, str) else value
            if sem in {"swing", "opens", "swingdirection"} and word in OPENING_SIDES:
                side = word
            elif sem in {"hinge", "hingeside", "handing"} and word in {"start", "end"}:
                hinge_end = word
            elif isinstance(value, (list, dict)):
                raise validation(f"{label} must be a single value", label, "In" if sem == "swing" else 45)
            elif table.get(sem) is None:
                warnings.append(f"{label} ignored: a {kind} has no {key!r} (use parameters for plug-in ids).")
            else:
                extra[str(key)] = value
        else:
            continue
        out.pop(key, None)
    if side is not None:
        word = str(side).strip().lower()
        if word not in OPENING_SIDES:
            raise validation(f"{field}.side must be left or right (the side of the wall, looking from its start to "
                             "its end, the leaf swings into)", f"{field}.side", "left")
        if right is None:
            right = OPENING_SIDES[word]
    right = bool(right)
    if flip is None:
        flip = (hinge_end == "end") != right if hinge_end else False
    if extra:
        raw = C.parse_json_maybe(_get(out, "parameters", "params", "values", "fields"))
        for alias in ("params", "values", "fields"):
            out.pop(alias, None)
        if isinstance(raw, list):
            out["parameters"] = list(raw) + [{"id": k, "value": v} for k, v in extra.items()]
        elif isinstance(raw, dict):
            out["parameters"] = {**raw, **extra}
        else:
            out["parameters"] = extra
    return out, {"flip": bool(flip), "right": right}


COLUMN_PLUGIN = "Column2"  # universal name of the Vectorworks Architect Column tool (Plug-ins/Column2.vso)


def _massing_params(kind: str, src: dict, field: str, scale: float) -> dict:
    """extrude: profile points (or a rect footprint) + height + base elevation; column: Column plug-in."""
    if kind == "extrude":
        raw_pts = _get(src, "points", "profile", "outline", "boundary", "vertices")
        if raw_pts is not None:
            pts = C.points(raw_pts, f"{field}.points", scale, minimum=3)
        else:
            a = _pt(src, ("start", "corner", "p1", "min"), ("x1", "y1"), f"{field}.start", scale, required=False)
            b = _pt(src, ("end", "opposite", "p2", "max"), ("x2", "y2"), f"{field}.end", scale, required=False)
            if a is None or b is None:
                w = _len(src, ("width", "w", "size_x"), f"{field}.width", scale, required=True, positive=True)
                d = _len(src, ("depth", "length", "d", "size_y"), f"{field}.depth", scale, required=True, positive=True)
                center = _pt(src, ("center", "centre"), ("cx", "cy"), f"{field}.center", scale, required=False)
                origin = a or _pt(src, ("origin", "at", "position", "location"), ("x", "y"), f"{field}.origin", scale,
                                  required=center is None)
                if center is not None and origin is None:
                    origin = [center[0] - w / 2, center[1] - d / 2]
                a, b = origin, [origin[0] + w, origin[1] + d]
            pts = [[a[0], a[1]], [b[0], a[1]], [b[0], b[1]], [a[0], b[1]]]
        bottom = _len(src, ("elevation", "bottom", "base", "base_elevation", "z"), f"{field}.elevation", scale, 0.0)
        top = _len(src, ("top",), f"{field}.top", scale)
        height = _len(src, ("height", "extrude_height", "h", "thickness"), f"{field}.height", scale,
                      required=top is None, positive=True)
        top = bottom + height if height is not None else top
        if top <= bottom:
            raise validation(f"{field}: top must be above the base elevation", f"{field}.height", 3000)
        return {"kind": "extrude", "points": pts, "bottom": bottom, "top": top}
    at = _pt(src, ("at", "center", "centre", "position", "location", "origin"), ("x", "y"), f"{field}.at", scale)
    lengths: dict[str, float] = {"OA Height": _len(src, ("height", "h"), f"{field}.height", scale, 3000.0, positive=True)}
    fields: dict[str, str] = {"Use Capital": "False", "Use Base": "False"}
    diameter = _len(src, ("diameter", "d"), f"{field}.diameter", scale, positive=True)
    if diameter is not None:
        fields.update({"ShaftType": "Round", "Shaft Type": "Round"})
        lengths["Diameter"] = diameter
    else:
        lengths["Shaft Width"] = _len(src, ("width", "w", "size"), f"{field}.width", scale, 400.0, positive=True)
        lengths["Shaft Depth"] = _len(src, ("depth", "length", "size"), f"{field}.depth", scale,
                                      lengths["Shaft Width"], positive=True)
    raw = C.parse_json_maybe(_get(src, "parameters", "params", "fields"))
    if raw is not None:
        if not isinstance(raw, dict):
            raise validation(f"{field}.parameters must be an object {{field: value}}", f"{field}.parameters",
                             {"Use Capital": True})
        for key, value in raw.items():
            if isinstance(value, bool):
                value = "True" if value else "False"
            fields[str(key)] = str(value)
            lengths.pop(str(key), None)
    plugin = str(_get(src, "plugin", "plugin_name", default=COLUMN_PLUGIN))
    return {"kind": "column", "plugin": plugin, "at": at, "lengths": lengths, "fields": fields,
            "rotation": C.number(_get(src, "rotation", "angle", default=0), f"{field}.rotation")}


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
        join = _get(src, "join_walls", "join", "auto_join")
        if join is not None:
            p["join_walls"] = C.boolean(join, f"{field}.join_walls")
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
    raw_params = _get(src, "parameters", "params", "values", "fields")
    raw_params = C.parse_json_maybe(raw_params) if raw_params is not None else None
    if kind in {"door", "window"} and isinstance(raw_params, dict):
        # Dedicated-field duplicates inside parameters ({"DoorWidth": "900"}) -> width/height/sill.
        kept: dict = {}
        for key, value in raw_params.items():
            sem = _semantic(key)
            target = next((name for name, aliases in OPENING_PARAM_ALIASES.items() if sem in aliases), None)
            if target == "sill" and kind != "window":
                target = None
            if target is None:
                kept[key] = value
                continue
            src_keys = {"width": ("width", "w"), "height": ("height", "h"), "sill": ("sill", "sill_height", "elevation")}[target]
            if _get(src, *src_keys) is None:
                src[src_keys[0]] = value
                plan.warnings.append(f"{field}.parameters.{key} was applied as the {kind}'s {target} field.")
            else:
                plan.warnings.append(f"{field}.parameters.{key} ignored: {target} is already given.")
        raw_params = kept
    if kind in {"door", "window"}:
        if wall_ref is None:
            raise validation(f"{field}.wall is required (the wall's uuid, or \"$<id>\" of a wall created in this call)",
                             f"{field}.wall", "$w1")
        wall_ref = str(wall_ref).strip()
        if wall_ref.startswith("uuid:"):
            wall_ref = wall_ref[5:]
        if not wall_ref.startswith("$") and not is_uuid(wall_ref):
            if wall_ref in plan.wall_ids or len(wall_ref) < 8:
                wall_ref = "$" + wall_ref  # "w1" -> "$w1" when it names a local wall
            else:
                raise validation(f"{field}.wall {wall_ref!r} is neither a wall uuid nor the id of a wall in this call "
                                 f"(wall ids here: {sorted(plan.wall_ids) or 'none'})", f"{field}.wall", "$w1")
        if wall_ref.startswith("$") and plan.wall_ids and wall_ref[1:] not in plan.wall_ids:
            raise validation(f"{field}.wall {wall_ref!r} does not name a wall created in this call (wall ids: "
                             f"{sorted(plan.wall_ids)}); for an existing wall pass its uuid", f"{field}.wall",
                             "$" + sorted(plan.wall_ids)[0])
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
    if raw_params is not None and raw_params != {}:
        p["parameters"] = parametric_parameters(raw_params, f"{field}.parameters", scale, schemas.get(plugin),
                                                warnings=plan.warnings, doc_mm_per_unit=plan.doc_mm_per_unit,
                                                plugin=plugin)
    return p


# Vectorworks parameter field styles (describe_parametric_schema.field_style).
_STYLE_INT, _STYLE_BOOL = 1, 2
_STYLE_REAL = {3, 9, 13}           # real / radio-ish numeric / angle
_STYLE_LENGTH = {7, 10, 11}        # dimension, coordinate X/Y (document length units)
_STYLE_TEXT = {4, 8}               # text, popup


def _schema_index(schema: Optional[dict]) -> tuple[dict[str, dict], dict[str, str]]:
    """by_id {id: entry}, alias {semantic id or display name: id}."""
    by_id: dict[str, dict] = {}
    alias: dict[str, str] = {}
    for entry in (schema or {}).get("parameters") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            pid = entry["id"]
            by_id[pid] = entry
            alias.setdefault(_semantic(pid), pid)
            display = str(entry.get("display_name") or "")
            display = re.sub(r"\(read only\)", "", display, flags=re.I)
            if display and not display.startswith("__"):
                alias.setdefault(_semantic(display), pid)
    return by_id, alias


def _shown_name(entry: dict) -> str:
    name = str(entry.get("display_name") or "")
    return "" if not name or name.startswith("__") or name == entry.get("id") else name


def did_you_mean(name: str, names: list[str], alias: Optional[dict] = None, n: int = 5) -> list[str]:
    """Up to n close names (case-insensitive substring hits first, then difflib); alias maps extra semantic
    spellings (e.g. display names) to a name."""
    import difflib
    low = str(name).lower()
    sem = _semantic(name)
    out: list[str] = []

    def add(value: Optional[str]) -> None:
        if value and value not in out:
            out.append(value)
    if alias and sem in alias:
        add(alias[sem])
    if alias:
        for match in difflib.get_close_matches(sem, list(alias), n=2, cutoff=0.8):
            add(alias[match])
    for cand in names:
        c = str(cand).lower()
        if low and (low in c or (len(c) > 2 and c in low)):
            add(cand)
    for key, target in (alias or {}).items():
        if sem and len(sem) > 2 and (sem in key or (len(key) > 3 and key in sem)):
            add(target)
    # Shared words ("Crosshatch" -> "... Cross ..."): words of >=4 letters found in the other name.
    import re as _re
    wanted_words = set(_re.findall(r"[a-z]{4,}", low))
    scored = []
    for cand in names:
        c = str(cand).lower()
        score = sum(1 for word in set(_re.findall(r"[a-z]{4,}", c)) if word in low)
        score += sum(1 for word in wanted_words if word in c)
        if score:
            scored.append((-score, len(c), cand))
    for _, _, cand in sorted(scored)[:n]:
        add(cand)
    lowered = {str(c).lower(): c for c in names}
    for match in difflib.get_close_matches(low, list(lowered), n=n, cutoff=0.6):
        add(lowered[match])
    if alias:
        for match in difflib.get_close_matches(sem, list(alias), n=n, cutoff=0.75):
            add(alias[match])
    return out[:n]


def _read_only(entry: Optional[dict]) -> bool:
    return bool(entry) and "read only" in str(entry.get("display_name") or "").lower()


def friendly_param(plugin: Optional[str], key: str, by_id: dict) -> Optional[str]:
    """Friendly name ("sill_height", "swing") -> the plug-in's real parameter id, or None."""
    table = FRIENDLY_PARAMS.get(str(plugin or ""))
    if not table:
        return None
    pid = table.get(_semantic(key))
    if pid is None or (by_id and pid not in by_id):
        return None
    return pid


def parametric_parameters(params: Any, field: str, scale: float, schema: Optional[dict], *,
                          warnings: Optional[list] = None, doc_mm_per_unit: float = 1.0,
                          plugin: Optional[str] = None) -> list[dict]:
    """{id: value} or [{id,type,value}] -> 0.7 typed parameter list.

    With the live schema: ids are matched case-insensitively (also by display name), friendly names (width,
    sill_height, open_angle, swing, ...) map to the plug-in's real ids, values are coerced to the parameter's type
    (numeric strings -> numbers, "90cm" -> document units for dimension fields, numbers -> text for popups) and
    read-only parameters are dropped with a warning (setting them fails the native readback)."""
    by_id, alias = _schema_index(schema)
    plugin = plugin or (schema or {}).get("universal_plugin_name") or (schema or {}).get("plugin_name")
    if isinstance(params, dict) and plugin == "Window":
        sem = {_semantic(k) for k in params}
        if sem & {"sill", "sillheight", "sillelevation"} and not sem & {"elevationsetat"} and \
                (not by_id or "ElevationSetAt" in by_id):
            # Elevation is the sill only with "Elevation Set At" = "Sill of Window" (live VW 2024)
            params = dict(params, ElevationSetAt="Sill of Window")
    note = warnings.append if warnings is not None else (lambda _m: None)
    entries: list[tuple[str, Any, Optional[str]]] = []
    if isinstance(params, dict):
        entries = [(str(k), v, None) for k, v in params.items()]
    elif isinstance(params, list):
        for entry in params:
            if not isinstance(entry, dict) or "id" not in entry:
                raise validation(f"{field} entries need id and value", field, [{"id": "OpenAngle", "value": 45}])
            entries.append((str(entry["id"]), entry.get("value"), str(entry["type"]).lower() if "type" in entry else None))
    else:
        raise validation(f"{field} must be an object {{\"ParamId\": value}}", field, {"OpenAngle": 45})
    out: list[dict] = []
    seen: set[str] = set()
    for key, value, explicit_type in entries:
        pid = key if key in by_id else (friendly_param(plugin, key, by_id) or alias.get(_semantic(key), key))
        if by_id and pid not in by_id and explicit_type is None:
            visible = [p for p in by_id if not str(by_id[p].get("display_name") or "").startswith("__")]
            friendly = {k: v for k, v in FRIENDLY_PARAMS.get(str(plugin or ""), {}).items() if v in by_id}
            close = [f"{p} ({by_id[p]['display_name']})" if _shown_name(by_id[p]) else p
                     for p in did_you_mean(key, visible, {**{k: v for k, v in alias.items() if v in visible},
                                                          **friendly})]
            raise validation(f"{field}.{key}: {plugin or 'the plug-in'} has no parameter {key!r}"
                             + (f"; did you mean {', '.join(close)}?" if close else "")
                             + " (vw_read(what='parametric_schema', target=..., search=...) lists them)",
                             f"{field}.{key}", {close[0].split(' (')[0] if close else "Width": 1000})
        if pid != key:
            note(f"{field}.{key} -> parameter id {pid!r}.")
        entry = by_id.get(pid)
        if _read_only(entry):
            note(f"{field}.{key} ignored: {pid!r} is read-only in Vectorworks (it is computed by the object).")
            continue
        if pid in seen:
            raise validation(f"{field}.{key} sets {pid!r} twice", field, {"OpenAngle": 45})
        seen.add(pid)
        if explicit_type and entry is None:
            out.append({"id": pid, "type": explicit_type, "value": value})
            continue
        out.append(_typed(pid, value, entry, scale, field, doc_mm_per_unit))
    return out


def _numeric(value: Any) -> bool:
    if C.is_number(value):
        return True
    try:
        float(str(value).strip())
        return str(value).strip() != ""
    except ValueError:
        return False


def _num_text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _typed(pid: str, value: Any, entry: Optional[dict], scale: float, field: str, doc_mm_per_unit: float = 1.0) -> dict:
    style = entry.get("field_style") if isinstance(entry, dict) and isinstance(entry.get("field_style"), int) else None
    label = f"{field}.{pid}"
    if style == _STYLE_BOOL or (style is None and isinstance(value, bool)):
        return {"id": pid, "type": "boolean", "value": C.boolean(value, label)}
    if style == _STYLE_INT:
        return {"id": pid, "type": "integer", "value": int(round(C.number(value, label)))}
    if style in _STYLE_LENGTH or (style is None and entry is not None and _numeric(entry.get("value"))
                                  and (C.is_number(value) or isinstance(value, str))):
        if isinstance(value, str) and any(ch.isalpha() or ch in "'\"" for ch in value):
            mm = C.length(value, label, 1.0)  # explicit unit in the string wins over `units`
        elif style in _STYLE_LENGTH:
            mm = C.length(value, label, scale)
        else:
            return {"id": pid, "type": "real", "value": C.number(value, label)}
        return {"id": pid, "type": "real", "value": mm / (doc_mm_per_unit or 1.0)}
    if style in _STYLE_REAL:
        return {"id": pid, "type": "real", "value": C.number(value, label)}
    if style in _STYLE_TEXT:
        return {"id": pid, "type": "string", "value": _num_text(value)}
    if style is None and C.is_number(value):
        return {"id": pid, "type": "integer" if isinstance(value, int) else "real", "value": value}
    return {"id": pid, "type": "string", "value": _num_text(value)}


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
        "extrude": {"type": "extrude", "points": [[0, 0], [8000, 0], [8000, 6000], [0, 6000]], "height": 9000},
        "column": {"type": "column", "at": [0, 0], "width": 400, "depth": 400, "height": 3000},
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
        self.typed: list[dict] = []        # set/parametric values that depend on the target's type (route_typed)
        self.native_order = False          # bridge has the native `order` op


# keys of a vw_edit change that are never object properties
EDIT_CONTROL_KEYS = {"op", "type", "action", "operation", "target", "targets", "objects", "object", "ref", "refs",
                     "uuid", "uuids", "properties", "props", "set", "values", "document", "units", "dry_run",
                     "preview", "idempotency_key", "id", "as"}


def _targets(src: dict, field: str) -> list[str]:
    value = _get(src, "targets", "target", "objects", "object", "ref", "refs", "uuid", "uuids", "name")
    value = C.parse_json_maybe(value)
    if value is None:
        raise validation(f"{field}.target is required (uuid, name, \"$id\" or \"selection\")", f"{field}.target", "uuid:...")
    if not isinstance(value, list):
        value = [value]
    return [v if isinstance(v, str) and v.lower() == "selection" else norm_ref(v, f"{field}.target") for v in value]


def compile_edit(changes: list[dict], scale: float, *, schemas: dict[str, dict], descriptors: dict[str, str],
                 native_order: bool = False) -> EditPlan:
    plan = EditPlan()
    plan.native_order = native_order
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
            props_src = C.parse_json_maybe(props_src) if props_src is not None else {}
            if not isinstance(props_src, dict):
                raise validation(f"{field}.properties must be an object", f"{field}.properties", {"fill_color": "#FF0000"})
            has_target = any(k in src for k in ("target", "targets", "objects", "object", "ref", "refs", "uuid", "uuids"))
            # flat form {"op":"set","target":..,"width":1800,"fill":..} (also mixed with properties)
            flat = {k: v for k, v in src.items() if k not in EDIT_CONTROL_KEYS and (has_target or k != "name")}
            props_src = {**flat, **props_src}
            if op == "class":
                props_src = {"class": _get(src, "class", "class_name", "value", "to")}
            if not props_src:
                raise validation(f"{field} has nothing to set: pass the values flat or in properties (e.g. name, "
                                 "class, fill_color, line_style; door/window width/height/sill; wall thickness/height; "
                                 "text; any plug-in parameter)", f"{field}.properties",
                                 {"op": "set", "target": "<uuid>", "fill_color": "#FF0000"})
            props: dict[str, str] = {}
            style_src: dict[str, Any] = {}
            typed: dict[str, Any] = {}
            for key, value in props_src.items():
                k = str(key).lower().replace(" ", "_")
                canonical = PROPERTY_SYNONYMS.get(k)
                if k == "fill" and not _is_color(value):
                    canonical = None  # fill: "none" | "solid" | hatch/gradient name -> style
                if canonical is None:
                    if k in STYLE_SYNONYMS:
                        style_src[key] = value
                    else:
                        typed[str(key)] = value  # routed by the target's type (vw_edit reads it first)
                    continue
                if canonical in {"fillColor", "penColor"}:
                    value = C.color(value, f"{field}.properties.{key}")
                elif canonical in {"fillPattern", "lineWeight", "opacity"}:
                    value = str(C.integer(value, f"{field}.properties.{key}"))
                props[canonical] = str(value)
            script_item["properties"] = props
            if props:
                native = [{"type": "set_properties", "params": {"edits": [{"ref": t, "properties": props}
                                                                          for t in targets]}}]
            style = style_props(style_src, f"{field}.properties") if style_src else {}
            if style:
                plan.script_ops.append({"op": "style", "targets": targets, "style": style})
                plan.needs_script = True
            if typed or "name" in props:
                plan.typed.append({"index": index, "field": field, "targets": targets, "values": typed,
                                   "name": props.get("name"), "kind": "set"})
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
            if params_src is None:
                flat = {k: v for k, v in src.items() if k not in EDIT_CONTROL_KEYS | {"plugin", "plugin_name", "name"}}
                params_src = flat or None
            if params_src is None:
                raise validation(f"{field} needs parameters {{id or friendly name: value}} (plugin is taken from the "
                                 "target)", f"{field}.parameters",
                                 {"op": "parametric", "target": "<door uuid>", "parameters": {"width": 1000, "open_angle": 45}})
            if plugin is None:
                # plug-in name comes from the target object (vw_edit reads it before compiling)
                plan.typed.append({"index": index, "field": field, "targets": targets, "kind": "parametric",
                                   "values": C.parse_json_maybe(params_src)})
                continue
            plugin = str(plugin)
            if (schemas.get(plugin) or {}).get("script_only"):
                # no native schema provider (e.g. Column2): set the parametric record fields by script
                plan.script_ops.append({"op": "pio", "targets": targets, "plugin": plugin, "scale_mm": scale,
                                        "parameters": pio_parameters(params_src, f"{field}.parameters")})
                plan.needs_script = True
                plan.warnings.append(f"{plugin} has no native schema provider; parameters were set through its "
                                     "parametric record (script).")
                continue
            parameters = parametric_parameters(C.parse_json_maybe(params_src), f"{field}.parameters", scale, schemas.get(plugin),
                                               warnings=plan.warnings, plugin=plugin)
            if not parameters:
                raise validation(f"{field}.parameters has nothing settable (see warnings: read-only parameters are "
                                 "computed by Vectorworks)", f"{field}.parameters", {"OpenAngle": 45})
            fingerprint = _get(src, "descriptor_fingerprint") or descriptors.get(plugin) or "pending"
            script_item.update({"plugin": plugin, "parameters": {p["id"]: p["value"] for p in parameters}})
            native = [{"type": "update_parametric", "params": {"target": t, "plugin_name": plugin,
                                                               "descriptor_fingerprint": fingerprint, "parameters": parameters}}
                      for t in targets]
        elif op == "layer":
            script_item["layer"] = C.text(_get(src, "layer", "to", "value"), f"{field}.layer", required=True)
        elif op == "order":
            raw_op = str(_get(src, "op", "type", "action", "operation", default="")).strip().lower().replace(" ", "_")
            position = _get(src, "position", "to", "where", "direction", "order", "value")
            if position is None:
                position = ORDER_OP_POSITIONS.get(raw_op)
            pos = ORDER_POSITIONS.get(str(position or "").strip().lower().replace(" ", "_").replace("-", "_"))
            if pos is None:
                raise validation(f"{field}.position must be front, back, forward or backward", f"{field}.position",
                                 {"op": "order", "target": "<uuid>", "position": "back"})
            script_item["position"] = pos
            if plan.native_order:
                plan.ops.extend({"type": "order", "params": {"target": t, "position": pos}} for t in targets)
                continue
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
    for entry in plan.typed:
        entry["targets"] = fix(entry["targets"])


def normalise_edit_ops(native_ops: list[dict], max_ops: int = 250) -> list[dict]:
    """Run the 0.7 normaliser on already-mm ops (validation + canonical form)."""
    if not native_ops:
        return []
    try:
        return ops._normalise_execute_operations(native_ops, coordinate_units="mm", max_operations=max_ops)
    except ValueError as exc:
        raise VWError("validation_error", str(exc), field="changes") from None
