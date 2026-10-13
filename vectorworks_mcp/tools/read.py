"""vw_read: every read-only query."""

from __future__ import annotations

import re
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
    for key, code in (("class", "C"), ("name", "N")):
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
        out["data"] = {k: v for k, v in data.items() if k not in _SUMMARY_DROP} if isinstance(data, dict) else data
        return out
    if what in {"query", "selection"}:
        return _query(host, target, a, what, fields, limit, offset, out)
    if what == "object":
        return _object(host, target, a, fields, out)
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
            sheet = C.text(a.get("target") or a.get("layer") or a.get("name"), "target", required=True)
            args_ = {"action": "list", "sheet": sheet[5:] if sheet.startswith("name:") else sheet}
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
        plugin = C.text(a.get("target") or a.get("name") or a.get("query"), "target", required=True)
        if builders_is_uuid(plugin) or plugin.startswith("uuid:"):
            # parameters of one placed plug-in object (parametric record values)
            out["data"] = host.script(target, "pio_schema", {"target": norm_ref(plugin, "target")},
                                      label="Read parameters", kind="read", transaction=False)
            out["via"] = "script"
            return out
        schema = host.schema(inst, plugin)
        if schema.get("script_only"):
            out["data"] = host.script(target, "pio_schema", {"plugin": plugin}, label="Read parameters", kind="read",
                                      transaction=False)
            out["via"] = "script"
            warnings.append(f"{plugin} has no native schema provider: parameters come from its parametric record "
                            "(field types + current defaults); vw_edit op='parametric' sets them the same way.")
            return out
        out["data"] = _clean(schema, [])
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
    if what == "query" and a.get("type") in (None, "") and a.get("query") not in (None, ""):
        if _known_type(a["query"]):
            a = dict(a, type=a["query"])  # weak models write query="Door" for type="door"
        else:
            out.setdefault("warnings", []).append("query is a text filter only for symbols/classes/worksheets; for "
                                                  "objects use type=, name=, class=, layer= or criteria=.")
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
    data = host.call(target, "query_objects", params)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise VWError("protocol_error", "query_objects returned no items list")
    out["data"] = [_clean(i, fields) for i in items]
    more = bool(data.get("has_more"))
    out["page"] = {"cursor": str(offset) if offset else "", "returned": len(items),
                   "next_cursor": str(offset + len(items)) if more else None}
    if more:
        out["page"]["note"] = "More objects exist; pass cursor=next_cursor, or use what='summary' for counts."
    return out


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


def _object(host, target, a: dict, fields: list[str], out: dict) -> dict:
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
    record = _clean(item, fields)
    if host.can_script(target.inst):
        try:
            detail = host.script(target, "object_detail", {"target": "uuid:" + str(item.get("uuid"))},
                                 label="Read object detail", kind="read", transaction=False)
            if isinstance(detail, dict):
                record = {**record, **{k: v for k, v in detail.items() if k not in record}}
        except VWError as exc:
            out.setdefault("warnings", []).append(f"detail unavailable: {exc.message}")
    out["data"] = record
    return out


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
            out["data"], out["page"] = _page(_list_from(data, rtype), offset, limit, fields)
        else:
            out["data"] = _clean(data, [])
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
    out["via"] = "script"
    if rtype in {"hatches", "line_types", "gradients", "image_fills", "tiles", "text_styles"}:
        out["usage"] = ("Use the name in vw_edit set properties / vw_create objects (fill, line_style, text_style); "
                        "source='library' names are imported into the document automatically on first use.")
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
