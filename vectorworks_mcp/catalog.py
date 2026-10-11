"""Static tool definitions (names, descriptions, input/output schemas,
annotations). Plain data so ``tools/list`` needs no heavy imports."""

from __future__ import annotations

S = {"type": "string"}
B = {"type": "boolean"}
N = {"type": "number"}
I = {"type": "integer"}
LEN = {"type": ["number", "string"]}          # 3000 | "3m" | "12'6\""
PT = {"type": ["array", "string", "object"]}   # [x,y] | "x,y" | {x,y}
STRS = {"type": "array", "items": S}
UNITS = {"type": "string", "enum": ["mm", "cm", "m", "in", "ft"]}
DOC = {"type": "string", "description": "Target document: fingerprint, file name or path (default: the one you used last)"}
IDEM = {"type": "string", "description": "Optional; derived automatically"}

STANDARD_VIEWS = ["top_plan", "top", "front", "back", "left", "right", "bottom", "iso", "right_iso", "left_iso",
                  "right_rear_iso", "left_rear_iso"]
RENDER_MODES = ["wireframe", "opengl", "shaded", "hidden_line", "dashed_hidden_line", "final_shaded", "sketch",
                "fast_renderworks", "final_renderworks"]
READ_WHAT = ["document", "documents", "layers", "classes", "summary", "query", "object", "selection", "view",
             "saved_views", "sheets", "viewports", "annotations", "symbols", "worksheets", "worksheet", "resources",
             "records", "parametric_schema", "capabilities", "plan_quality"]
CREATE_TYPES = ["line", "rect", "oval", "circle", "arc", "polygon", "polyline", "wall", "slab", "roof", "space", "text",
                "dimension", "symbol", "door", "window", "parametric", "room"]
EDIT_OPS = ["set", "move", "rotate", "scale", "mirror", "reshape", "duplicate", "delete", "parametric", "class", "layer",
            "group", "ungroup", "record", "select", "deselect"]


def out_schema(**props: dict) -> dict:
    """Permissive on purpose: only ok/error/warnings are typed, so structuredContent
    can never violate the advertised schema (values may legitimately be null)."""
    base = {"ok": B, "error": {"type": "object"}, "warnings": STRS}
    base.update({name: {} for name in props})
    return {"type": "object", "properties": base, "required": ["ok"]}


def obj(props: dict, required: tuple = ()) -> dict:
    schema = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
VIEW = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

TOOLS: list[dict] = [
    {
        "name": "vw_status",
        "title": "Vectorworks status",
        "description": (
            "Health and context: Vectorworks instances, open documents, UI state (busy/dialog), setup problems with the "
            "fix. Never fails. Also: job_id -> progress/result of a long operation; idempotency_key -> whether a write "
            "was applied; instance -> pin one Vectorworks instance; disconnect=true closes host sockets. Call it first "
            "and after any connection error. Example: vw_status() or vw_status(job_id=\"j42\")."),
        "input": obj({"job_id": S, "idempotency_key": S, "instance": S, "detail": B, "disconnect": B}),
        "output": out_schema(ready=B, instances={"type": "array"}, documents={"type": "array"}, job={"type": "object"}),
        "annotations": RO,
    },
    {
        "name": "vw_read",
        "title": "Read drawing data",
        "description": (
            "Read-only. what=document|documents|layers|classes|summary (counts by type/layer/class)|query (objects; "
            "filter type (wall, door, window, space, stair, ...; doors/windows in walls come with wall+width)/layer/"
            "class/name/criteria, paged by cursor)|object (target)|selection|view|saved_views|sheets|"
            "viewports (target=sheet)|annotations|symbols|worksheets|worksheet (target, range 'A1:D20')|resources "
            "(resource_type)|records|parametric_schema (target='Door')|capabilities|plan_quality (plan). Lengths in mm; "
            "use the returned uuid in later calls. Example: vw_read(what=\"query\", type=\"wall\", limit=50)."),
        "input": obj({"what": {"type": "string", "enum": READ_WHAT}, "document": DOC, "target": S, "type": S, "layer": S,
                      "class": S, "name": S, "criteria": S, "query": S, "resource_type": S, "fields": STRS, "range": S,
                      "limit": I, "cursor": S, "plan": {"type": "object"}}, ("what",)),
        "output": out_schema(data={}, page={"type": "object"}, document={"type": "object"}),
        "annotations": RO,
    },
    {
        "name": "vw_capture",
        "title": "Capture view image",
        "description": (
            "See the drawing: returns an image (long edge <= 1568 px) plus a link to the full-resolution file. view="
            "drawing (default, fit all)|current|selection|objects (uuids)|layer|sheet|saved_view, optional temporary "
            "standard_view and render_mode. The user's view, layer and selection are restored afterwards. Use after "
            "edits to verify. Example: vw_capture(view=\"objects\", objects=[\"<uuid>\"], standard_view=\"iso\")."),
        "input": obj({"view": {"type": "string", "enum": ["drawing", "current", "selection", "objects", "layer", "sheet",
                                                           "saved_view"]},
                      "objects": STRS, "layer": S, "sheet": S, "saved_view": S,
                      "standard_view": {"type": "string", "enum": STANDARD_VIEWS},
                      "render_mode": {"type": "string", "enum": RENDER_MODES},
                      "format": {"type": "string", "enum": ["png", "jpg"]}, "margin_pct": N, "document": DOC}),
        "output": out_schema(path=S, width=I, height=I, resource=S, restored=B),
        "annotations": RO,
    },
    {
        "name": "vw_create",
        "title": "Create objects",
        "description": (
            "Create many objects in ONE call (one undo step); the receipt (ids->uuids) is authoritative. Room in ONE "
            "call: walls with ids + {type:door|window, wall:\"$<wall id>\", at:[x,y], width} + space points; touching "
            "walls are joined (join_walls). mm unless units. Types: line start,end | rect origin,width,height | circle "
            "center,radius | polygon/slab/roof/space points | wall start,end,thickness,height | text at,text | dimension "
            "| symbol | door/window | parametric plugin,parameters | room. Example: objects=[{\"type\":\"wall\",\"id\":"
            "\"w1\",\"start\":[0,0],\"end\":[5000,0]},{\"type\":\"door\",\"wall\":\"$w1\",\"at\":[1500,0]}]."),
        "input": obj({
            "objects": {"type": "array", "items": {"type": "object", "properties": {
                "type": {"type": "string", "enum": CREATE_TYPES}, "id": S, "name": S, "class": S,
                "start": PT, "end": PT, "at": PT, "center": PT, "origin": PT, "points": {"type": ["array", "string"]},
                "width": LEN, "height": LEN, "depth": LEN, "radius": LEN, "thickness": LEN, "offset": LEN, "sill": LEN,
                "elevation": LEN, "rotation": N, "text": S, "size": N, "symbol": S, "plugin": S, "wall": S, "style": S,
                "parameters": {"type": "object"}, "join_walls": B}, "required": ["type"]}},
            "floor_plan": {"type": "object"}, "units": UNITS, "join_walls": B, "dry_run": B, "document": DOC,
            "idempotency_key": IDEM}),
        "output": out_schema(created={"type": "array"}, ids={"type": "object"}, document={"type": "object"}),
        "annotations": WRITE,
    },
    {
        "name": "vw_edit",
        "title": "Edit objects",
        "description": (
            "Change existing objects in one undo step. changes=[{op, target|targets, ...}]; target = uuid, name, or "
            "\"selection\". ops: set properties{name,class,fill_color,pen_color,line_weight,opacity,fill_pattern} | move "
            "by=[dx,dy] | rotate angle,center? | scale factor | mirror axis x/y | reshape start,end | duplicate by,count | "
            "delete | parametric plugin,parameters | class | layer | group | ungroup | record record,fields | select | "
            "deselect. Example: changes=[{\"op\":\"move\",\"target\":\"<uuid>\",\"by\":[1000,0]},{\"op\":\"set\","
            "\"target\":\"<uuid>\",\"properties\":{\"fill_color\":\"#FF0000\"}}]."),
        "input": obj({
            "changes": {"type": "array", "items": {"type": "object", "properties": {
                "op": {"type": "string", "enum": EDIT_OPS}, "target": S, "targets": STRS, "properties": {"type": "object"},
                "by": PT, "angle": N, "center": PT, "factor": N, "axis": S, "start": PT, "end": PT, "count": I,
                "plugin": S, "parameters": {"type": "object"}, "class": S, "layer": S, "record": S,
                "fields": {"type": "object"}, "criteria": S}, "required": ["op"]}},
            "units": UNITS, "dry_run": B, "document": DOC, "idempotency_key": IDEM}, ("changes",)),
        "output": out_schema(applied={"type": "integer"}, receipts={"type": "array"}, document={"type": "object"}),
        "annotations": DESTRUCTIVE,
    },
    {
        "name": "vw_apply",
        "title": "Raw atomic transaction",
        "description": (
            "Power-user transaction in the 0.7 grammar (one undo step): operations=[{type, operation_id?, params}] with "
            "type create (params.object_type + fields) | set_properties | transform | reshape | update_parametric | "
            "duplicate | delete, or sheet/viewport/annotation lifecycle ops when the bridge supports them; '$id' refers "
            "to an earlier create. Doors/windows: prefer vw_create. join_walls (default true) joins touching walls. "
            "Grammar: resource vw://guide. Example: operations=[{\"type\":"
            "\"create\",\"operation_id\":\"r1\",\"params\":{\"object_type\":\"rect\",\"x1\":0,\"y1\":0,\"x2\":100,\"y2\":50}}]."),
        "input": obj({"operations": {"type": "array", "items": {"type": "object"}}, "coordinate_units": UNITS,
                      "join_walls": B, "idempotency_key": IDEM, "document": DOC}, ("operations",)),
        "output": out_schema(committed=B, receipts={"type": "array"}, idempotency_key=S),
        "annotations": DESTRUCTIVE,
    },
    {
        "name": "vw_view",
        "title": "Change the view",
        "description": (
            "Change what the user sees (no geometry changes). Any of: standard_view, projection, render_mode, fit="
            "drawing|selection|objects|layer (+objects), zoom, center [x,y], saved_view (restore), save_view_as (create "
            "saved view), layer (make active). No arguments = report the current view. Example: vw_view(standard_view="
            "\"top_plan\", fit=\"drawing\")."),
        "input": obj({"standard_view": {"type": "string", "enum": STANDARD_VIEWS},
                      "projection": {"type": "string", "enum": ["orthogonal", "perspective", "plan"]},
                      "render_mode": {"type": "string", "enum": RENDER_MODES},
                      "fit": {"type": "string", "enum": ["drawing", "selection", "objects", "layer"]}, "objects": STRS,
                      "zoom": N, "center": PT, "margin_pct": N, "saved_view": S, "save_view_as": S, "layer": S,
                      "document": DOC}),
        "output": out_schema(previous={"type": "object"}, current={"type": "object"}),
        "annotations": VIEW,
    },
    {
        "name": "vw_organize",
        "title": "Layers, classes, stories",
        "description": (
            "Manage document structure. kind=layer|class|story|saved_view|worksheet|resource with action=create|set|"
            "delete|write|import. Fields: name, new_name, visibility show|gray|hide, active, scale, elevation, opacity, "
            "fill_color, pen_color, line_weight, layer_kind design|sheet, cell 'B3' + value (worksheet write), source "
            "path + resource_type (resource import). Deleting non-empty layers needs force=true. Example: vw_organize("
            "kind=\"layer\", action=\"create\", name=\"Furniture\", scale=50)."),
        "input": obj({"kind": {"type": "string", "enum": ["layer", "class", "story", "saved_view", "worksheet", "resource"]},
                      "action": {"type": "string", "enum": ["create", "set", "delete", "write", "import"]},
                      "name": S, "new_name": S, "visibility": {"type": "string", "enum": ["show", "gray", "hide"]},
                      "active": B, "scale": N, "elevation": LEN, "opacity": N, "fill_color": S, "pen_color": S,
                      "line_weight": N, "use_at_creation": B, "layer_kind": {"type": "string", "enum": ["design", "sheet"]},
                      "title": S, "cell": S, "value": S, "source": S, "resource_type": S, "force": B, "units": UNITS,
                      "document": DOC}, ("kind", "action")),
        "output": out_schema(result={}),
        "annotations": DESTRUCTIVE,
    },
    {
        "name": "vw_document",
        "title": "Documents and files",
        "description": (
            "Documents and file I/O. action=list|info|activate|new|open|save|save_as|close|export|import|dismiss_home "
            "(close the Vectorworks Home screen). path = absolute "
            "file path; format pdf|dwg|dxf|ifc|png|jpg|vwx (from extension if omitted). close of a modified document needs "
            "discard_changes=\"discard\" (ask the user). Long exports/imports report progress; if still_running, poll "
            "vw_status(job_id). Example: vw_document(action=\"export\", path=\"C:\\\\Out\\\\plan.pdf\", range=\"all_pages\")."),
        "input": obj({"action": {"type": "string", "enum": ["list", "info", "activate", "new", "open", "save", "save_as",
                                                             "close", "export", "import", "dismiss_home"]},
                      "path": S, "format": {"type": "string", "enum": ["pdf", "dwg", "dxf", "ifc", "png", "jpg", "tif", "bmp", "vwx"]},
                      "document": DOC, "range": {"type": "string", "enum": ["current_view", "all_pages", "sheet"]},
                      "sheet": S, "dpi": I, "replace": B, "discard_changes": S, "template": S, "at": PT, "wait_ms": I},
                     ("action",)),
        "output": out_schema(result={}, document={"type": "object"}, job_id=S),
        "annotations": DESTRUCTIVE,
    },
    {
        "name": "vw_script",
        "title": "Run a Vectorworks script",
        "description": (
            "Escape hatch: run Python (vs.* API, `vs` pre-imported) or VectorScript inside Vectorworks as ONE undo step; "
            "rolled back on error. Assign `result = ...` to return JSON. Use only when no other tool covers the task. "
            "vs.* calls are checked against the SDK signatures before sending (COLOR = (r, g, b) 0..65535; "
            "lint=false skips). Example: vw_script(code=\"h = vs.FSActLayer()\\nresult = vs.GetName(h)\")."),
        "input": obj({"code": S, "language": {"type": "string", "enum": ["python", "vectorscript"]}, "undo_label": S,
                      "background": B, "lint": B, "document": DOC}, ("code",)),
        "output": out_schema(result={}, stdout=S, stderr=S, rolled_back=B),
        "annotations": DESTRUCTIVE,
    },
]
