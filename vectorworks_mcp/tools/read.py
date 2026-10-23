"""vw_read: every read-only query."""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from .. import coerce as C
from .. import scripts_lib
from ..builders import is_uuid as builders_is_uuid, norm_ref
from ..catalog import READ_WHAT, TOOLS
from ..documents import normalise_doc, rev5_read_binding
from ..errors import VWError, validation
from ..host import doc_brief, get_host

WHAT_SYNONYMS = {"doc": "document", "info": "document", "document_info": "document", "docs": "documents",
                 "objects": "query", "find": "query", "search": "query", "lookup": "query", "list": "query",
                 "get_objects": "query", "inspect": "object", "detail": "object", "selected": "selection",
                 "sheet_layers": "sheets", "viewport_annotations": "annotations", "symbol": "symbols",
                 "class": "classes", "layer": "layers", "stats": "summary", "context": "summary", "counts": "summary",
                 "schema": "parametric_schema", "parametric_schemas": "parametric_schema", "caps": "capabilities",
                 "health": "capabilities", "record": "records", "savedviews": "saved_views", "views": "saved_views",
                 "resource": "resources", "camera": "view"}
_DROP = {"timing", "handle", "binding"}
_SUMMARY_DROP = {"ok", "tool", "native_summary", "query", "document", "timing", "binding"}
# Vectorworks node types the native summary reports as "type_<n>".
_NODE_NAMES = {"type_9": "locus_3d", "type_12": "quarter_arc", "type_14": "bitmap", "type_17": "locus",
               "type_34": "sweep", "type_38": "multi_extrude", "type_40": "mesh", "type_81": "light", "type_84": "solid",
               "type_85": "brep", "type_95": "solid", "type_111": "nurbs_curve", "type_113": "nurbs_surface",
               "type_114": "composite_surface", "type_122": "viewport"}


def _type_names(value: Any) -> Any:
    """Rename type_<n> keys (also nested per layer) to readable object type names."""
    if not isinstance(value, dict):
        return value
    out: dict = {}
    for k, v in value.items():
        name, v = _NODE_NAMES.get(k, k), _type_names(v)
        out[name] = out[name] + v if isinstance(v, int) and isinstance(out.get(name), int) else v
    return out


def _clean(item: Any, fields: list[str]) -> Any:
    if not isinstance(item, dict):
        return item
    if item.get("parent_uuid") and "wall" not in item and item.get("host", "wall") == "wall":
        item = dict(item, wall=item["parent_uuid"])  # door/window/symbol inserted in a wall
    if fields:
        return {k: item.get(k) for k in fields if k in item}
    out = {k: v for k, v in item.items() if k not in _DROP}
    if "class_name" in out and out.get("class_name") == out.get("class"):
        out.pop("class_name")
    return out


def _page(items: list, offset: int, limit: int, fields: list[str]) -> tuple[list, dict]:
    chunk = items[offset:offset + limit]
    more = len(items) > offset + limit
    return [_clean(i, fields) for i in chunk], {"cursor": str(offset) if offset else "", "returned": len(chunk),
                                                 "next_cursor": str(offset + limit) if more else None, "total": len(items)}


def _offset(cursor: Any) -> int:
    if cursor in (None, ""):
        return 0
    text = str(cursor).strip()
    if not re.fullmatch(r"\d{1,10}", text):
        raise validation("cursor must be the next_cursor value from a previous page", "cursor", "100")
    return int(text)


def _criteria(a: dict) -> Optional[str]:
    parts = []
    for key, code in (("class", "C"),):
        value = a.get(key)
        if value not in (None, ""):
            parts.append(f"({code}='{str(value).replace(chr(39), chr(39) * 2)}')")
    raw = a.get("criteria")
    if raw not in (None, "") and str(raw).strip().upper() != "ALL":
        raw = str(raw).strip()
        parts.append(raw if raw.startswith("(") else f"({raw})")
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0] if parts[0].startswith("((") else f"({parts[0]})"
    return "(" + " & ".join(parts) + ")"


def _list_from(data: Any, *keys: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys + ("items",):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def handle(args: dict, ctx, warnings: list) -> dict:
    a = C.lower_keys(args)
    if "what" not in a:
        for alt in ("action", "kind", "type_of", "read"):
            if alt in a:
                a["what"] = a.pop(alt)
                break
    what = C.choice(a.get("what"), "what", READ_WHAT, WHAT_SYNONYMS)
    if a.get("query") in (None, "") and a.get("search") not in (None, ""):
        a["query"] = a["search"]  # search= is the documented spelling for name filters
    if what in {"viewports", "annotations"} and a.get("target") in (None, ""):
        alias = a.get("sheet") if what == "viewports" else (a.get("viewport") or a.get("sheet"))
        if alias not in (None, ""):
            a["target"] = alias
    if what == "plan_quality":
        return _plan_quality(a)
    host = get_host()
    fields = C.str_list(a.get("fields"), "fields")
    limit = C.clamp_int(a.get("limit"), "limit", 1, 1000, 100, warnings)
    offset = _offset(a.get("cursor"))
    need_doc = what not in {"capabilities", "documents"}
    target = host.session.target(a.get("document"), need_document=need_doc)
    inst = target.inst
    warnings.extend(target.warnings)
    out: dict = {"ok": True, "what": what}
    if target.document:
        out["document"] = doc_brief(target.document)

    if what == "document":
        info = host.call(target, "get_document_info", {})
        doc = normalise_doc(info)
        if doc:
            host.session.observe(inst, doc)
            out["document"] = doc_brief(doc, True)
        out["data"] = {k: v for k, v in (info or {}).items() if k not in {"timing", "binding"}} if isinstance(info, dict) else info
        return out
    if what == "documents":
        docs = host.session.list_documents(inst)
        out["data"] = [doc_brief(d, True) | ({"active": True} if d.get("active") else {}) for d in docs]
        if not inst.supports("list_documents"):
            warnings.append("Pre-1.0 bridge: only the active document is visible.")
        return out
    if what == "layers":
        if inst.supports("layers"):
            data = host.call(target, "layers", {"action": "list"})
        else:
            data = host.call(target, "get_layers", {})
        out["data"], out["page"] = _page(_list_from(data, "layers"), offset, limit, fields)
        return out
    if what == "classes":
        data = host.call(target, "manage_classes", {"action": "list", "query": a.get("query") or ""})
        items = _list_from(data, "classes")
        q = str(a.get("query") or "").lower()
        if q:
            items = [c for c in items if q in str(c.get("name", c) if isinstance(c, dict) else c).lower()]
        out["data"], out["page"] = _page(items, offset, limit, fields)
        return out
    if what == "summary":
        native_type, plugin = _type_filter(a.get("type"))
        data = host.call(target, "drawing_summary", {"layer": a.get("layer") or "", "object_type": native_type or plugin or "",
                                                     "limit": 5, "scan_limit": 100_000, "include_examples": False,
                                                     "example_limit": 0})
        out["data"] = {k: _type_names(v) if k.startswith("counts_by") else v
                       for k, v in data.items() if k not in _SUMMARY_DROP} if isinstance(data, dict) else data
        if isinstance(out["data"], dict) and not C.boolean(a.get("detail", False), "detail"):
            d = out["data"]
            for key in ("counts_by_layer_type", "objects_returned", "named_objects_returned", "layer_count"):
                d.pop(key, None)
            if d.get("possibly_truncated") is False:
                d.pop("possibly_truncated")
            d["layers"] = [{k: v for k, v in l.items() if k in {"name", "kind", "visible"}} if isinstance(l, dict) else l
                           for l in d.get("layers") or []]
        b = out["data"].get("bounds") if isinstance(out["data"], dict) else None
        if isinstance(b, dict) and all(isinstance(b.get(k), (int, float)) for k in ("top", "bottom")) and b["top"] < b["bottom"]:
            b["top"], b["bottom"] = b["bottom"], b["top"]  # native reports y-down; top is the larger y in mm
        return out
    if what in {"query", "selection"}:
        out = _query(host, target, a, what, fields, limit, offset, out)
        uuids = [i.get("uuid") for i in out.get("data") or [] if isinstance(i, dict) and i.get("uuid")]
        host.session.remember_query(target.document, uuids)
        if uuids:
            out["tip"] = "vw_edit(target='last_query') applies a change to all of these"
        if not fields:
            _as_table(out)
        return out
    if what == "object":
        return _object(host, target, a, fields, out, C.boolean(a.get("detail", False), "detail"))
    if what == "geometry":
        data = host.script(target, "section_read", {"target": norm_ref(a.get("target"), "target"),
                           "offset": offset, "limit": limit, "detail": C.boolean(a.get("detail", False), "detail")},
                           label="Read grouped geometry", kind="read", transaction=False)
        out["data"] = data["items"]
        out["revision"] = data["revision"]
        out["page"] = {"total": data["total"], "offset": offset, "has_more": offset + len(data["items"]) < data["total"]}
        if out["page"]["has_more"]:
            out["page"]["next_cursor"] = str(offset + len(data["items"]))
        return out
    if what == "view":
        out["data"] = _clean(host.call(target, "get_view", {}), [])
        return out
    if what == "saved_views":
        if inst.supports("saved_views"):
            data = host.call(target, "saved_views", {"action": "list"})
        elif host.can_script(inst):
            data = host.script(target, "saved_views", {"action": "list"}, label="List saved views", kind="read")
        else:
            raise host.bridge.missing_action(inst, "saved_views")
        out["data"], out["page"] = _page(_list_from(data, "saved_views", "views"), offset, limit, fields)
        return out
    if what == "sheets":
        if inst.supports("get_sheet_layers") and not (inst.v2 and inst.supports("layers")):
            # native get_sheet_layers needs a saved-document binding; v2 lists sheets via `layers`
            params = {"offset": offset, "limit": limit}
            if not inst.v2:
                params.update(rev5_read_binding(host.call(target, "get_document_info", {})))
            data = host.call(target, "get_sheet_layers", params)
            return _native_page(out, data, offset, fields)
        data = host.call(target, "layers", {"action": "list", "kind": "sheet"})
        items = [l for l in _list_from(data, "layers") if not isinstance(l, dict) or l.get("kind", "sheet") == "sheet"]
        out["data"], out["page"] = _page(items, offset, limit, fields)
        return out
    if what in {"viewports", "annotations"} and host.can_script(inst):
        # script read: works for unsaved documents (native get_viewports needs a saved-file binding) and returns
        # scale/view/render/layers/crop/centre per viewport
        if what == "viewports":
            sheet = C.text(a.get("target") or a.get("sheet") or a.get("layer") or a.get("name"), "target")
            args_ = {"action": "list", "sheet": sheet[5:] if sheet.startswith("name:") else sheet}  # "" = all sheets
        else:
            args_ = {"action": "annotations", "target": norm_ref(a.get("target"), "target")}
        data = host.script(target, "viewport", args_, label="Read viewports", kind="read", transaction=False) or {}
        items = data.get("viewports") if what == "viewports" else data.get("annotations")
        if what == "viewports":
            from .organize import viewport_record
            items = [viewport_record(r) for r in items or []]
        out["data"], out["page"] = _page(items or [], offset, limit, fields)
        out["via"] = "script"
        return out
    if what in {"viewports", "annotations"}:
        params: dict = {"offset": offset, "limit": limit}
        if what == "viewports":
            params["sheet_layer_uuid"] = _sheet_uuid(host, target, a.get("target") or a.get("layer") or a.get("name"))
            action = "get_viewports"
        else:
            params["viewport_uuid"] = _uuid(a.get("target"), "target", "viewport uuid")
            params["sheet_layer_uuid"] = _sheet_uuid(host, target, a.get("layer"), field="layer")
            action = "get_viewport_annotations"
        if not inst.v2:
            params.update(rev5_read_binding(host.call(target, "get_document_info", {})))
        return _native_page(out, host.call(target, action, params), offset, fields)
    if what in {"symbols", "worksheets"}:
        action = "symbol" if what == "symbols" else "worksheet"
        params = {"action": "list", "query": a.get("query") or a.get("name") or ""}
        if inst.v2:
            params.update({"offset": offset, "limit": limit})
        data = host.call(target, action, params)
        if isinstance(data, dict) and "next_cursor" in data:
            return _native_page(out, data, offset, fields, key=what)
        out["data"], out["page"] = _page(_list_from(data, what), offset, limit, fields)
        return out
    if what == "worksheet":
        return _worksheet(host, target, a, out, warnings)
    if what == "resources":
        return _resources(host, target, a, fields, limit, offset, out)
    if what == "records":
        ref = a.get("target")
        if inst.supports("records"):
            params = {"action": "get", "target": norm_ref(ref, "target")} if ref else {"action": "list"}
            out["data"] = _clean(host.call(target, "records", params), [])
        else:
            args_ = {"action": "object", "target": norm_ref(ref, "target")} if ref else {"action": "formats"}
            out["data"] = host.script(target, "records", args_, label="Read records", kind="read")
            out["via"] = "script"
        return out
    if what == "parametric_schema":
        plugin = C.text(a.get("target") or a.get("name") or (a.get("query") if not a.get("search") else None),
                        "target", required=True)
        search = str(a.get("search") or (a.get("query") if a.get("target") or a.get("name") else "") or "")
        detail = C.boolean(a.get("detail", False), "detail")
        if builders_is_uuid(plugin) or plugin.startswith("uuid:"):
            # parameters of one placed plug-in object (parametric record values)
            data = host.script(target, "pio_schema", {"target": norm_ref(plugin, "target")},
                               label="Read parameters", kind="read", transaction=False)
            out["data"] = _compact_schema(data, search, detail, limit if a.get("limit") else None)
            out["via"] = "script"
            return out
        schema = host.schema(inst, plugin)
        if schema.get("script_only"):
            data = host.script(target, "pio_schema", {"plugin": plugin}, label="Read parameters", kind="read",
                               transaction=False)
            out["data"] = _compact_schema(data, search, detail, limit if a.get("limit") else None)
            out["via"] = "script"
            warnings.append(f"{plugin} has no native schema provider: parameters come from its parametric record "
                            "(field types + current defaults); vw_edit op='parametric' sets them the same way.")
            return out
        out["data"] = _compact_schema(_clean(schema, []), search, detail, limit if a.get("limit") else None)
        return out
    if what == "capabilities":
        return _capabilities(host, inst, out)
    raise validation(f"what={what} is not implemented", "what", "query")


_OTYPE = {"walls": "wall", "rectangle": "rect", "rectangles": "rect", "rects": "rect", "doors": "door",
          "windows": "window", "texts": "text", "labels": "text", "lines": "line", "polygons": "polygon",
          "polylines": "polyline", "slabs": "slab", "roofs": "roof", "spaces": "space", "rooms": "space",
          "symbols": "symbol", "circles": "circle", "arcs": "arc", "ovals": "oval", "dimensions": "dimension",
          "linear_dimension": "dimension", "groups": "group", "viewports": "viewport"}


def _otype(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return _OTYPE.get(text, text)


# Native node / object types the bridge filters on directly (object_type).
NATIVE_TYPES = {"line", "rect", "oval", "circle", "arc", "polygon", "polyline", "text", "symbol", "group", "wall",
                "dimension", "linear_dimension", "slab", "roof", "space", "door", "window", "viewport", "image",
                "parametric", "locus", "extrude", "mesh", "solid", "worksheet", "layer_link"}
# Friendly names of common plug-in objects -> universal plug-in name (native compares case/space-insensitively).
PLUGIN_TYPES = {"stair": "Stair", "stairs": "Stair", "staircase": "Stair", "column": "Column", "columns": "Column",
                "pillar": "Column", "railing": "Railing", "railings": "Railing", "handrail": "Railing", "ramp": "Ramp",
                "data_tag": "Data Tag", "tag": "Data Tag", "tags": "Data Tag", "grid": "Grid Line",
                "grid_line": "Grid Line", "elevation_benchmark": "Elevation Benchmark", "lighting_device": "Lighting Device"}
_OPENINGS = {"door": "Door", "window": "Window"}


def _type_filter(value: Any) -> tuple[str, Optional[str]]:
    """Friendly type -> (native object_type, plug-in name or None)."""
    text = _otype(value)
    if not text:
        return "", None
    if text in _OPENINGS:
        return text, _OPENINGS[text]
    if text in NATIVE_TYPES:
        return text, None
    singular = text[:-1] if text.endswith("s") and text[:-1] in PLUGIN_TYPES else text
    plugin = PLUGIN_TYPES.get(singular) or str(value).strip()
    return "", plugin


def _known_type(value: Any) -> bool:
    text = _otype(value)
    return bool(text) and (text in NATIVE_TYPES or text in PLUGIN_TYPES or text.rstrip("s") in PLUGIN_TYPES)


def _pon(plugin: str) -> str:
    return "(PON='" + plugin.replace("'", "''") + "')"


def _uuid(value: Any, field: str, label: str) -> str:
    text = str(value or "").strip()
    if text.startswith("uuid:"):
        text = text[5:]
    if not text:
        raise validation(f"{field} must be the {label} (from vw_read)", field, "1A2B3C4D-...")
    return text


def _sheet_uuid(host, target, value: Any, field: str = "target") -> str:
    """Sheet layer uuid, or its name (looked up in the layer list)."""
    text = str(value or "").strip()
    if text.startswith("uuid:") or re.fullmatch(r"\{?[0-9A-Fa-f-]{32,36}\}?", text):
        return _uuid(text, field, "sheet layer uuid")
    if not text:
        raise validation(f"{field} must be the sheet layer name or uuid (vw_read(what='sheets'))", field, "A-101")
    if target.inst.supports("layers"):
        layers = _list_from(host.call(target, "layers", {"action": "list"}), "layers")
        hit = next((l for l in layers if isinstance(l, dict) and l.get("name") == text), None)
        if hit and hit.get("uuid"):
            return str(hit["uuid"])
    raise VWError("not_found", f"No sheet layer named {text!r}.", field=field,
                  next_step="List sheets with vw_read(what='sheets') and pass the name or uuid.")


def _native_page(out: dict, data: Any, offset: int, fields: list[str], key: str = "items") -> dict:
    items = _list_from(data, key)
    out["data"] = [_clean(i, fields) for i in items]
    nxt = data.get("next_cursor") if isinstance(data, dict) else None
    if isinstance(nxt, dict):  # rev-5 page record
        nxt = nxt.get("offset") if nxt.get("has_more", True) else None
    out["page"] = {"cursor": str(offset) if offset else "", "returned": len(items),
                   "next_cursor": str(nxt) if nxt not in (None, "", False) else None}
    return out


def _query(host, target, a: dict, what: str, fields: list[str], limit: int, offset: int, out: dict) -> dict:
    inst = target.inst
    text_filter = ""
    if what == "query" and a.get("type") in (None, "") and a.get("query") not in (None, "") and _known_type(a["query"]):
        a = dict(a, type=a["query"], query=None)  # weak models write query="Door" for type="door"
    if what == "query":
        text_filter = str(a.get("query") or a.get("name") or "").strip().lower()
    criteria = _criteria(a) if what == "query" else None
    native_type, plugin = _type_filter(a.get("type"))
    features = inst.status.get("object_read_features")
    features = set(features) if isinstance(features, list) else set()
    object_type = native_type
    if plugin and what == "query":
        # Doors/windows sit inside walls and other plug-ins are matched by name: bridges that advertise these
        # features filter natively; older ones need the plug-in-object-name criterion (PON).
        native_ok = ("wall_inserts" in features) if native_type in _OPENINGS else ("object_type_plugin_names" in features)
        if native_ok:
            object_type = native_type or plugin
        else:
            object_type = ""
            criteria = "(" + _pon(plugin) + (" & " + criteria if criteria else "") + ")"
    elif plugin:
        object_type = native_type or plugin
    native_fields = list(dict.fromkeys(fields + (["type"] if fields and "type" not in fields else []))) if fields else []
    if native_fields and "wall" in native_fields:
        native_fields = list(dict.fromkeys(native_fields + ["parent_uuid", "host"]))
    params: dict = {"mode": "selection" if what == "selection" else ("criteria" if criteria else "objects"),
                    "offset": offset, "limit": limit, "layer": a.get("layer") or "", "object_type": object_type,
                    "criteria": criteria or "ALL", "field_count": len(native_fields)}
    params.update({f"field_{i}": f for i, f in enumerate(native_fields, 1)})
    if not inst.supports("query_objects"):
        raise host.bridge.missing_action(inst, "query_objects")
    region = _region(a) if what == "query" else None
    if text_filter or region:
        return _filtered_query(host, target, params, text_filter, fields, limit, offset, out, region)
    data = host.call(target, "query_objects", params)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise VWError("protocol_error", "query_objects returned no items list")
    _add_room_names(host, target, items, fields)
    out["data"] = [_clean(i, fields) for i in items]
    if not fields:
        # detail=true adds the graphic attributes, not every native field (a weak model asked for detail on
        # every read: 11-25 KB per call, live)
        with_attrs = C.boolean(a.get("detail", False), "detail")
        out["data"] = [_compact_row(i, with_attrs) for i in out["data"]]
    more = bool(data.get("has_more"))
    out["page"] = {"cursor": str(offset) if offset else "", "returned": len(items),
                   "next_cursor": str(offset + len(items)) if more else None}
    if more:
        out["page"]["note"] = "More objects exist; pass cursor=next_cursor, or use what='summary' for counts."
    return out


_FILTER_SCAN_LIMIT = 20_000


def _pair_box(value: Any) -> Any:
    """[x1, y1, x2, y2] (flat, also strings) or {x1,y1,x2,y2} -> [[x1, y1], [x2, y2]]; anything else unchanged."""
    value = C.parse_json_maybe(value)
    if isinstance(value, dict):
        v = C.lower_keys(value)
        if all(k in v for k in ("x1", "y1", "x2", "y2")):
            return [[v["x1"], v["y1"]], [v["x2"], v["y2"]]]
    if isinstance(value, list) and len(value) == 4 and not any(isinstance(x, (list, dict)) for x in value):
        return [[value[0], value[1]], [value[2], value[3]]]
    return value


def _region(a: dict) -> Optional[tuple[float, float, float, float]]:
    """within=[[x1,y1],[x2,y2]] (a box) or near=[x,y] (+ radius, default 1000 mm) -> (xmin, ymin, xmax, ymax) in mm."""
    scale = C.scale_for(C.units_name(a.get("units")))
    if a.get("within_px") not in (None, ""):
        from .. import capture as cap
        m = cap.mapping_for(a.get("capture"))
        if not m:
            raise VWError("not_found", "within_px needs a plan capture from this session (its image_to_world).",
                          field="capture", next_step="Call vw_capture() (plan view) first, then pass pixel corners "
                          "of the image you received; or use within=[[x1,y1],[x2,y2]] in mm.")
        (u1, v1), (u2, v2) = C.points(_pair_box(a["within_px"]), "within_px", 1.0, minimum=2, maximum=2)
        xs = [m["x0"] + u * m["mm_per_px"] for u in (u1, u2)]
        ys = [m["y0"] - v * m["mm_per_px"] for v in (v1, v2)]
        return min(xs), min(ys), max(xs), max(ys)
    if a.get("within") not in (None, ""):
        p1, p2 = C.points(_pair_box(a["within"]), "within", scale, minimum=2, maximum=2)
        return min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1])
    if a.get("near") not in (None, ""):
        x, y = C.point(a["near"], "near", scale)[:2]
        r = C.number(a.get("radius", 1000), "radius") * scale
        return x - r, y - r, x + r, y + r
    return None


_ROW_DROP = {"type_id", "native_type", "fillColor", "penColor", "lineWeight", "fillPattern", "opacity", "host",
             "parent_uuid"}


def _compact_row(item: Any, attrs: bool = False) -> Any:
    """Default query row: identity, layer/class, the type's key values and a [[minx,miny],[maxx,maxy]] box.
    Graphic attributes and internal type ids come with detail=true (or fields=[...])."""
    if not isinstance(item, dict):
        return item
    row = {k: v for k, v in item.items() if k not in _ROW_DROP and v not in ("", None)}
    if row.get("class") == "None":
        row.pop("class")
    if row.get("plugin_name") and row.get("type") != "parametric":
        row.pop("plugin_name")
    if row.get("room_name") and row.get("room_name") == row.get("name"):
        row.pop("room_name")
    box = _item_box(item)
    if box:
        row["bounds"] = [[round(box[0]), round(box[1])], [round(box[2]), round(box[3])]]
    if attrs:
        got = {_ATTR_KEYS[k]: item[k] for k in _ATTR_KEYS if item.get(k) not in (None, "")}
        if got:
            row["attributes"] = got
    return row


_TABLE_OVER = 40


def _as_table(out: dict) -> None:
    """Large query results as {"columns": [...], "rows": [[...]]}: every key once instead of in every row
    (a weak model listed 180 spaces with detail: 51 KB, live)."""
    rows = out.get("data")
    if not isinstance(rows, list) or len(rows) <= _TABLE_OVER or not all(isinstance(r, dict) for r in rows):
        return
    columns: list[str] = []
    for r in rows:
        for k in r:
            if k not in columns:
                columns.append(k)
    out["data"] = {"columns": columns, "rows": [[r.get(c) for c in columns] for r in rows]}


def _item_box(item: dict) -> Optional[tuple[float, float, float, float]]:
    b = item.get("bounds")
    if not isinstance(b, dict):
        return None
    try:
        (x1, y1), (x2, y2) = b["top_left"][:2], b["bottom_right"][:2]
    except (KeyError, TypeError, ValueError):
        return None
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def _filtered_query(host, target, params: dict, needle: str, fields: list[str], limit: int, offset: int,
                    out: dict, region: Optional[tuple] = None) -> dict:
    """query/name = case-insensitive 'contains' filter over object name, text content and Space room name;
    within/near = object bounds intersect the region. The cursor is the native offset to continue scanning from."""
    native_fields = [f for f in fields]
    if native_fields:
        native_fields = list(dict.fromkeys(native_fields + ["name", "text", "type", "plugin_name", "uuid", "bounds"]))
    hits: list = []
    scan = offset
    more = False
    while len(hits) < limit and scan - offset < _FILTER_SCAN_LIMIT:
        page = dict(params, offset=scan, limit=1000, field_count=len(native_fields))
        page.update({f"field_{i}": f for i, f in enumerate(native_fields, 1)})
        data = host.call(target, "query_objects", page)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise VWError("protocol_error", "query_objects returned no items list")
        _add_room_names(host, target, items, [])
        consumed = 0
        for item in items:
            consumed += 1
            if not isinstance(item, dict):
                continue
            hay = " ".join(str(item.get(k) or "") for k in ("name", "text", "room_name")).lower()
            box = _item_box(item) if region else None
            in_region = not region or (box is not None and box[0] <= region[2] and box[2] >= region[0]
                                       and box[1] <= region[3] and box[3] >= region[1])
            if needle in hay and in_region:
                hits.append(item)
                if len(hits) >= limit:
                    break
        scan += consumed
        native_more = bool(data.get("has_more")) or consumed < len(items)
        if not native_more:
            more = False
            break
        more = True
    out["data"] = [_clean(i, fields) for i in hits] if fields else [_compact_row(i) for i in hits]
    out["page"] = {"cursor": str(offset) if offset else "", "returned": len(hits),
                   "next_cursor": str(scan) if more else None,
                   "filter": "; ".join(([f"name/text contains {needle!r}"] if needle else []) +
                                       ([f"bounds intersect x {region[0]:g}..{region[2]:g}, y {region[1]:g}..{region[3]:g} mm"]
                                        if region else []))}
    if more:
        out["page"]["note"] = "More objects may match; pass cursor=next_cursor."
    return out


def _add_room_names(host, target, items: list, fields: list[str]) -> None:
    """Spaces: room_name = the displayed label (the object name is a separate, often generated, value)."""
    if fields and "room_name" not in fields:
        return
    spaces = [i for i in items if isinstance(i, dict) and (i.get("type") == "space" or i.get("plugin_name") == "Space")
              and i.get("uuid")]
    if not spaces or not host.can_script(target.inst):
        return
    # Each script costs ~0.4 s in a big drawing (Vectorworks' Python context setup), and searches read labels page by
    # page: cache labels per document until this server writes anything (host.writes).
    # also expires every minute: the user may rename rooms by hand in Vectorworks meanwhile
    key = ((target.document or {}).get("fingerprint"), host.writes, int(time.monotonic() // 60))
    cache = _ROOM_LABELS.setdefault(key, {})
    if len(_ROOM_LABELS) > 8:
        for old in list(_ROOM_LABELS)[:-4]:
            _ROOM_LABELS.pop(old, None)
    missing = [i["uuid"] for i in spaces if str(i["uuid"]).lower() not in cache][:1000]
    if missing:
        # empty cache: all Spaces of the document at once (searches then cost one script, not one per page)
        args = {"all_labels": True} if not cache else {"labels": missing}
        try:
            data = host.script(target, "object_detail", args,
                               label="Read room names", kind="read", transaction=False) or {}
        except VWError:
            return
        for k, v in (data.get("labels") or {}).items():
            cache[str(k).lower()] = v
    for item in spaces:
        label = cache.get(str(item["uuid"]).lower())
        if label is not None:
            item["room_name"] = label


_ROOM_LABELS: dict[tuple, dict] = {}


def _find_by_uuid(host, target, uuid: str, fields: list[str]) -> Optional[dict]:
    if target.inst.v2:
        # v2: exact lookup (GetObjectByUuid; also finds doors/windows inside walls). Filter anyway in case an
        # older bridge ignores the uuid parameter and returns a normal page.
        try:
            data = host.call(target, "query_objects", {"mode": "objects", "uuids": [uuid], "offset": 0,
                                                       "limit": 5, "layer": "", "object_type": "", "criteria": "ALL",
                                                       "field_count": 0})
            for item in (data.get("items") if isinstance(data, dict) else None) or []:
                if isinstance(item, dict) and str(item.get("uuid", "")).lower() == uuid.lower():
                    return item
            if isinstance(data, dict) and "missing_uuids" in data:
                return None  # the bridge did an exact lookup: the object does not exist
        except VWError as exc:
            if exc.code not in {"native_error", "native_rejected", "stale_reference"}:
                raise
    offset = 0
    for _ in range(100):
        data = host.call(target, "query_objects", {"mode": "objects", "offset": offset, "limit": 1000, "layer": "",
                                                   "object_type": "", "criteria": "ALL", "field_count": 0})
        items = data.get("items") if isinstance(data, dict) else []
        for item in items or []:
            if isinstance(item, dict) and str(item.get("uuid", "")).lower() == uuid.lower():
                return item
        if not (isinstance(data, dict) and data.get("has_more")):
            return None
        offset += len(items)
    return None


def _object(host, target, a: dict, fields: list[str], out: dict, detail: bool = False) -> dict:
    ref = norm_ref(a.get("target") or a.get("name"), "target")
    item: Optional[dict] = None
    if ref.startswith("name:"):
        data = host.call(target, "query_objects", {"mode": "criteria", "offset": 0, "limit": 5,
                                                   "criteria": "((N='" + ref[5:].replace("'", "''") + "'))",
                                                   "layer": "", "object_type": "", "field_count": 0})
        items = data.get("items") if isinstance(data, dict) else []
        item = items[0] if items else None
    elif ref.startswith("uuid:"):
        item = _find_by_uuid(host, target, ref[5:], fields)
    else:
        raise validation("target must be a uuid or an object name", "target", "uuid:1A2B-...")
    if item is None:
        raise VWError("not_found", f"No object matches {ref}.")
    record = _clean(item, fields) if (fields or detail) else compact_record(item)
    if host.can_script(target.inst) and not fields:
        try:
            extra = host.script(target, "object_detail", {"target": "uuid:" + str(item.get("uuid")), "detail": detail},
                                label="Read object detail", kind="read", transaction=False)
            if isinstance(extra, dict):
                if not detail:
                    extra = _trim_detail(extra)
                record = {**record, **{k: v for k, v in extra.items() if k not in record or k == "layer"}}
        except VWError as exc:
            out.setdefault("warnings", []).append(f"detail unavailable: {exc.message}")
    if not detail and not fields:
        record.pop("type_id", None)
        out["more"] = "detail=true adds all records/parametric parameters"
    out["data"] = record
    return out


_ATTR_KEYS = {"fillColor": "fill_color", "penColor": "pen_color", "lineWeight": "line_weight",
              "fillPattern": "fill_pattern", "opacity": "opacity"}
_KEEP = ("uuid", "type", "plugin_name", "name", "layer", "class", "room_id", "height", "net_area", "gross_area",
         "width", "parent_uuid", "text")


def compact_record(item: dict) -> dict:
    """Native object record -> compact form (attributes grouped, bounds as [[x1,y1],[x2,y2]])."""
    out = {k: item[k] for k in _KEEP if item.get(k) not in (None, "")}
    if out.get("parent_uuid"):
        out["wall"] = out.pop("parent_uuid")
    if isinstance(out.get("text"), str) and len(out["text"]) > 200:
        out["text"] = out["text"][:200] + "..."
    attrs = {_ATTR_KEYS[k]: item[k] for k in _ATTR_KEYS if item.get(k) not in (None, "")}
    if attrs:
        out["attributes"] = attrs
    bounds = item.get("bounds")
    if isinstance(bounds, dict) and bounds.get("top_left") and bounds.get("bottom_right"):
        tl, br = bounds["top_left"], bounds["bottom_right"]
        out["bounds"] = [[_r(tl[0]), _r(br[1])], [_r(br[0]), _r(tl[1])]]
    return out


def _r(v):
    try:
        v = round(float(v), 1)
        return int(v) if v == int(v) else v
    except (TypeError, ValueError):
        return v


def _trim_detail(extra: dict) -> dict:
    extra = {k: v for k, v in extra.items() if k not in {"type_id", "uuid", "name", "class", "plugin"}}
    inserts = extra.get("inserts")
    if isinstance(inserts, list) and len(inserts) > 6:
        extra["inserts"] = {"count": len(inserts), "first": inserts[:6]}
    points = extra.get("points")
    if isinstance(points, list) and len(points) > 50:
        extra["points"] = points[:50]
    return extra


# Parameter field styles -> type names (describe_parametric_schema field_style / vs.GetFldType).
_FIELD_TYPES = {1: "integer", 2: "boolean", 3: "real", 4: "text", 7: "length", 8: "popup", 9: "radio", 10: "coord_x",
                11: "coord_y", 13: "angle", 14: "static", 18: "class", 20: "angle", 21: "area", 22: "volume",
                24: "material", 29: "texture"}
# The parameters most edits need, per plug-in (shown first in the compact schema).
_USEFUL = {
    "Door": ["Width", "Height", "Configuration", "SwingOperation", "SwingDirection", "SwingHingeSide", "OpenAngle",
             "LeafType", "TopShape", "Transom", "SideLights", "Threshold", "Lintel", "UseWallDepth", "DoorThickness",
             "JambWidth", "JambDepth", "IntTrim", "ExtTrim", "IDLabel", "IDPrefix", "OnSched", "3DOpen",
             "Show3DHinge", "DoorFR", "Manuf", "Model", "Notes"],
    "Window": ["Width", "Height", "Elevation", "ElevationSetAt", "Configuration", "CasementOperation", "HungOperation",
               "OpenAngle", "TopShape", "Transom", "IncludeSill", "SillStyle", "SillDepth", "Lintels", "UseWallDepth",
               "JambWidth", "JambDepth", "SashWidth", "IntTrim", "ExtTrim", "MuntinPattern", "IDLabel", "IDPrefix",
               "OnSchedule", "Show3DOpen", "Manuf", "Model", "WindowDesc"],
    "Space": ["11_Number", "11_Room ID", "11_Space Type", "11_Gross Height", "11_Net Height", "11_Show 3D Detail",
              "11_Boundary Display", "11_Occupancy Type", "11_Proposed Area", "11_Calc Dims", "11_On Schedule",
              "Area", "Gross Area", "Perim", "Volume", "Floor Finish", "Ceiling Material", "Remarks"],
}


def _compact_schema(data: Any, search: str, detail: bool, limit: Optional[int]) -> Any:
    """Schema / parametric record -> {plugin, total, parameters:[{id, name?, type, value}]} with the ~30 most
    useful parameters (search filters id + display name; detail=true returns everything)."""
    if not isinstance(data, dict) or not isinstance(data.get("parameters"), list):
        return data
    plugin = data.get("universal_plugin_name") or data.get("plugin_name") or ""
    params = [p for p in data["parameters"] if isinstance(p, dict)]
    if detail and not search:
        return data
    rows = []
    for p in params:
        name = str(p.get("display_name") or "")
        hidden = name.startswith("__") or str(p.get("id", "")).startswith("__")
        row = {"id": p.get("id")}
        if name and not hidden and name != p.get("id"):
            row["name"] = name
        style = p.get("field_style")
        row["type"] = p.get("type") or _FIELD_TYPES.get(style, str(style) if style is not None else "")
        value = p.get("value")
        row["value"] = value[:60] if isinstance(value, str) else value
        if "read only" in name.lower():
            row["read_only"] = True
        rows.append((row, hidden))
    from ..builders import FRIENDLY_SHOWN
    shown = FRIENDLY_SHOWN.get(plugin, {})
    if search:
        q = search.lower()
        by_id = {r["id"]: r for r, _h in rows}
        hits = [by_id[pid] for key, pid in shown.items() if q in key and pid in by_id]
        hits += [r for r, _h in rows if r not in hits and (q in str(r["id"]).lower() or q in str(r.get("name", "")).lower())]
        out_rows = hits if detail else hits[:limit or 40]
    else:
        useful = _USEFUL.get(plugin, [])
        by_id = {r["id"]: r for r, hidden in rows}
        out_rows = [by_id[i] for i in useful if i in by_id]
        out_rows += [r for r, hidden in rows if not hidden and r not in out_rows and r.get("type") not in
                     {"class", "material", "texture", "static"}]
        out_rows = out_rows[:limit or 30]
    result = {"plugin": plugin, "total": len(params), "shown": len(out_rows), "parameters": out_rows}
    if data.get("descriptor_fingerprint"):
        result["descriptor_fingerprint"] = data["descriptor_fingerprint"]
    if data.get("source"):
        result["source"] = data["source"]
    if shown:
        result["friendly_names"] = shown
    if len(out_rows) < len(params):
        result["more"] = "search='<part of id or name>' filters all parameters; detail=true returns every parameter"
    return result


_CELL = re.compile(r"^([A-Za-z]{1,3})(\d{1,6})$")


def _cell(ref: str) -> tuple[int, int]:
    m = _CELL.match(ref.strip())
    if not m:
        raise validation(f"{ref!r} is not a cell like 'B3'", "range", "A1:D20")
    col = 0
    for ch in m.group(1).upper():
        col = col * 26 + (ord(ch) - 64)
    return int(m.group(2)), col


def _worksheet(host, target, a: dict, out: dict, warnings: list) -> dict:
    name = C.text(a.get("target") or a.get("name"), "target", required=True)
    rng = str(a.get("range") or "").strip()
    if rng:
        start, _, end = rng.partition(":")
        r1, c1 = _cell(start)
        r2, c2 = _cell(end or start)
    else:
        sheets = _list_from(host.call(target, "worksheet", {"action": "list", "query": name}), "worksheets")
        meta = next((s for s in sheets if isinstance(s, dict) and s.get("name") == name), None)
        if meta is None:
            raise VWError("not_found", f"No worksheet named {name!r}.")
        r1, c1, r2, c2 = 1, 1, min(int(meta.get("rows") or 1), 20), min(int(meta.get("columns") or 1), 10)
        if (meta.get("rows") or 0) > 20 or (meta.get("columns") or 0) > 10:
            warnings.append(f"Showing A1:{chr(64 + c2)}{r2}; pass range='A1:H60' for more.")
    if (r2 - r1 + 1) * (c2 - c1 + 1) > 600:
        raise validation("range is limited to 600 cells per call", "range", "A1:J60")
    rows = []
    for r in range(r1, r2 + 1):
        row = []
        for c in range(c1, c2 + 1):
            cell = host.call(target, "worksheet", {"action": "read", "worksheet_name": name, "row": r, "column": c})
            row.append(cell.get("displayed_value") if isinstance(cell, dict) else cell)
        rows.append(row)
    out["data"] = {"worksheet": name, "range": f"{_colname(c1)}{r1}:{_colname(c2)}{r2}", "rows": rows}
    return out


def _colname(col: int) -> str:
    name = ""
    while col:
        col, rem = divmod(col - 1, 26)
        name = chr(65 + rem) + name
    return name


def _resources(host, target, a: dict, fields: list[str], limit: int, offset: int, out: dict) -> dict:
    rtype = str(a.get("resource_type") or a.get("type") or "all").strip().lower().replace(" ", "_").replace("-", "_")
    rtype = scripts_lib.RESOURCE_SYNONYMS.get(rtype, rtype)
    query = a.get("query") or a.get("name") or ""
    if rtype in {"all", "symbols", "worksheets", ""}:
        data = host.call(target, "resources", {"action": "list", "query": query})
        if rtype in {"symbols", "worksheets"}:
            items = _list_from(data, rtype)
            if query:
                q = str(query).lower()
                items = [i for i in items if q in str(i.get("name", i) if isinstance(i, dict) else i).lower()]
            out["data"], out["page"] = _page(items, offset, limit, fields)
            return out
        if not query:
            out["data"] = _clean(data, [])
            return out
        # search across every resource kind: symbols/worksheets natively + attribute resources by script
        q = str(query).lower()
        rows = [{"name": i.get("name"), "type": kind[:-1]} for kind in ("symbols", "worksheets")
                for i in _list_from(data, kind) if isinstance(i, dict) and q in str(i.get("name", "")).lower()]
        if host.can_script(target.inst):
            codes = [scripts_lib.RESOURCE_TYPES[k] for k in ("hatches", "line_types", "text_styles", "gradients",
                                                              "image_fills", "tiles", "wall_styles", "slab_styles")]
            res = host.script(target, "resource_list", {"resource_types": codes, "query": query, "offset": 0,
                                                        "limit": 1000, "library": True},
                              label="Search resources", kind="read", transaction=False) or {}
            rows += _list_from(res, "items")
        out["data"], out["page"] = _page(rows, offset, limit, fields)
        out["page"]["filter"] = f"name contains {query!r}"
        return out
    code = scripts_lib.RESOURCE_TYPES.get(rtype)
    if code is None:
        try:
            code = int(rtype)
        except ValueError:
            raise validation(f"resource_type must be one of {', '.join(['all'] + sorted(scripts_lib.RESOURCE_TYPES))} "
                             "or a BuildResourceList number", "resource_type", "hatches") from None
    data = host.script(target, "resource_list", {"resource_type": code, "query": query, "offset": offset, "limit": limit,
                                                 "library": True},
                       label="List resources", kind="read", transaction=False)
    out["data"] = _list_from(data, "items")
    out["page"] = {"cursor": str(offset) if offset else "", "returned": len(out["data"]),
                   "next_cursor": data.get("next_cursor") if isinstance(data, dict) else None,
                   "total": data.get("total") if isinstance(data, dict) else None}
    if not fields and not C.boolean(a.get("detail", False), "detail"):
        # Plain names (half the tokens); names already in the document are listed separately.
        items = out["data"]
        out["data"] = [i.get("name") if isinstance(i, dict) else i for i in items]
        in_doc = [i.get("name") for i in items if isinstance(i, dict) and i.get("source") == "document"]
        if in_doc:
            out["in_document"] = in_doc
    if rtype in {"hatches", "line_types", "gradients", "image_fills", "tiles", "text_styles"}:
        out["usage"] = "Use a name as fill / line_style / text_style; library names import on first use."
    return out


def _capabilities(host, inst, out: dict) -> dict:
    status = inst.status
    try:
        manifest = host.bridge.capabilities(inst)
    except VWError:
        manifest = {}
    actions = sorted(inst.implemented())
    out["data"] = {
        "instance": inst.summary(),
        "native_actions": actions,
        "create_object_types": status.get("create_object_types") or manifest.get("create_object_types"),
        "object_read_features": status.get("object_read_features"),
        "script_features": ({k: v for k, v in scripts_lib.SNIPPETS.items()} if host.can_script(inst) else {}),
        "script_features_status": "not live-tested; implemented via run_script" if host.can_script(inst) else
                                  "unavailable: bridge has no run_script",
        "tools": {t["name"]: {k: v for k, v in t["annotations"].items()} for t in TOOLS},
    }
    return out


def _plan_quality(a: dict) -> dict:
    plan = C.parse_json_maybe(a.get("plan"))
    if not isinstance(plan, dict):
        raise validation("plan (the plan manifest object) is required for what='plan_quality'", "plan",
                         {"rooms": [], "doors": [], "windows": []})
    from ..analysis.plan_quality import PlanValidationError, evaluate_plan_payload  # lazy: 52 KB module
    try:
        report = evaluate_plan_payload(plan)
    except PlanValidationError as exc:
        raise VWError("validation_error", "Plan manifest is malformed.", field="plan",
                      details={"problems": exc.as_list()}) from None
    return {"ok": True, "what": "plan_quality", "data": report, "execution": "host_only"}
