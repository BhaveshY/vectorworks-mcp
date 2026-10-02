"""vw_organize: layers, classes, stories, saved views, worksheet cells,
resource import."""

from __future__ import annotations

import re

from .. import coerce as C
from .. import scripts_lib
from ..errors import VWError, validation
from ..host import doc_brief, get_host

KINDS = ["layer", "class", "story", "saved_view", "worksheet", "resource"]
ACTIONS = ["create", "set", "delete", "write", "import"]
KIND_SYN = {"layers": "layer", "design_layer": "layer", "sheet": "layer", "sheet_layer": "layer", "classes": "class",
            "stories": "story", "level": "story", "view": "saved_view", "saved_views": "saved_view",
            "worksheets": "worksheet", "cell": "worksheet", "resources": "resource", "symbol": "resource"}
ACTION_SYN = {"add": "create", "new": "create", "make": "create", "update": "set", "edit": "set", "modify": "set",
              "rename": "set", "show": "set", "hide": "set", "activate": "set", "remove": "delete", "del": "delete",
              "put": "write", "set_cell": "write", "load": "import"}


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
    action = C.choice(a.get("action") or a.get("op"), "action", ACTIONS, ACTION_SYN)
    host = get_host()
    target = host.session.target(a.get("document"), write=True)
    inst = target.inst
    name = a.get("name")
    out: dict = {"ok": True, "kind": kind, "action": action}
    if target.document:
        out["document"] = doc_brief(target.document)
    scale = C.scale_for(C.units_name(a.get("units")))

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
            params["scale"] = C.number(a["scale"], "scale")
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


def _clean(value):
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if k != "timing"}
    return value
