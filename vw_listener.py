"""
Vectorworks 2024/2025 MCP Listener - runs inside Vectorworks.

Opens a TCP socket (default 127.0.0.1:9877) and serves MCP requests using
non-blocking I/O via selectors. The generated launcher starts the listener in
dialog-pump mode, where a small Vectorworks dialog timer pumps the socket from
the Vectorworks main thread. This avoids the Vectorworks 2024 embedded-Python
background-thread pause that can leave the port open but unresponsive.

INSTALL OPTIONS
  A) Quick in Vectorworks 2024 - Resource Manager > New Resource > Script,
     choose Python Script, paste the generated vw_start_listener_2024.py, run it.
  B) Persistent menu command - Tools > Plug-ins > Plug-in Manager >
     New > Menu Command, paste the generated vw_start_listener_2024.py. Then
     Tools > Workspaces > Edit Current Workspace > Menus and drag the
     new command into a menu. Click it once per VW session to start.

STOP: create a file named STOP in the stop-file folder printed at startup,
or close the document / quit Vectorworks.

CONFIG (env vars, all optional):
  VW_MCP_HOST       default 127.0.0.1
  VW_MCP_PORT       default 9877
  VW_MCP_STOP_DIR   default ~/.vectorworks-mcp
  VW_MCP_MAX_FRAME_BYTES default 16777216
  VW_MCP_MODE       dialog | foreground | background
  VW_MCP_DIALOG_TIMER_MS default 50
"""
try:
    import vs
except ModuleNotFoundError:
    vs = None

import io, json, math, os, selectors, socket, struct, sys, threading, traceback, types

__VERSION__ = "0.3.0-socket"

# === CONFIGURATION ===
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9877
DEFAULT_MAX_FRAME_BYTES = 16 * 1024 * 1024
DEFAULT_DIALOG_TIMER_MS = 50


def _env_int(name, default, min_value=None, max_value=None):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("{name} must be an integer, got {value!r}".format(name=name, value=raw))
    if min_value is not None and value < min_value:
        raise ValueError("{name} must be >= {min}, got {value}".format(name=name, min=min_value, value=value))
    if max_value is not None and value > max_value:
        raise ValueError("{name} must be <= {max}, got {value}".format(name=name, max=max_value, value=value))
    return value


_CONFIG_ERROR = None
try:
    HOST = os.environ.get("VW_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    PORT = _env_int("VW_MCP_PORT", DEFAULT_PORT, 1, 65535)
    MAX_FRAME_BYTES = _env_int("VW_MCP_MAX_FRAME_BYTES", DEFAULT_MAX_FRAME_BYTES, 1024, 128 * 1024 * 1024)
    DIALOG_TIMER_MS = _env_int("VW_MCP_DIALOG_TIMER_MS", DEFAULT_DIALOG_TIMER_MS, 20, 5000)
except ValueError as e:
    _CONFIG_ERROR = str(e)
    HOST = DEFAULT_HOST
    PORT = DEFAULT_PORT
    MAX_FRAME_BYTES = DEFAULT_MAX_FRAME_BYTES
    DIALOG_TIMER_MS = DEFAULT_DIALOG_TIMER_MS

STOP_DIR = os.environ.get("VW_MCP_STOP_DIR") or os.path.join(
    os.path.expanduser("~"), ".vectorworks-mcp"
)
STOP_FILE = os.path.join(STOP_DIR, "STOP")
SCREENSHOT_DIR = STOP_DIR
_SHOULD_STOP = False
_STATE_MODULE = "_vw_mcp_listener_state"
_STATE = sys.modules.get(_STATE_MODULE)
if _STATE is None:
    _STATE = types.SimpleNamespace(listener_thread=None, listener_server=None, dialog_running=False)
    sys.modules[_STATE_MODULE] = _STATE
else:
    if not hasattr(_STATE, "listener_thread"):
        _STATE.listener_thread = None
    if not hasattr(_STATE, "listener_server"):
        _STATE.listener_server = None
    if not hasattr(_STATE, "dialog_running"):
        _STATE.dialog_running = False

# === HANDLE REGISTRY ===
_handles, _hcount = {}, [0]

def _reg(h):
    if h is None: return None
    _hcount[0] += 1
    hid = f"h{_hcount[0]}"
    _handles[hid] = h
    return hid

def _get(hid):
    return _handles.get(hid)

# === TYPE MAP ===
TYPE_NAMES = {
    2: "line", 3: "rect", 4: "oval", 5: "polygon", 6: "arc", 8: "freehand",
    10: "text", 11: "group", 13: "symbol", 15: "dimension", 16: "3d_polygon",
    18: "locus", 21: "extrude", 24: "mesh", 34: "wall", 38: "roof", 40: "floor",
    63: "roof_face", 68: "nurbs_curve", 71: "viewport", 85: "slab",
}

def _obj_info(h):
    try:
        tn = vs.GetTypeN(h) if hasattr(vs, 'GetTypeN') else vs.GetType(h)
        bbox = vs.GetBBox(h)
        return {"handle": _reg(h), "type": TYPE_NAMES.get(tn, f"type_{tn}"), "type_id": tn,
                "name": vs.GetName(h) or "",
                "bounds": {"top_left": list(bbox[0]), "bottom_right": list(bbox[1])}}
    except Exception:
        return {"handle": _reg(h), "type": "unknown", "name": "", "bounds": None}

def _ok(result): return {"success": True, "result": result}
def _err(msg): return {"success": False, "error": msg}


class ProtocolError(Exception):
    pass


def _alert(message):
    if vs is not None and hasattr(vs, "AlrtDialog"):
        try:
            vs.AlrtDialog(message)
            return
        except Exception:
            pass
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


def _message(message):
    if vs is not None and hasattr(vs, "Message"):
        try:
            vs.Message(message)
            return
        except Exception:
            pass
    _alert(message)


def _json_safe(value, depth=0):
    if depth > 25:
        return str(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, depth + 1) for v in value]
    return str(value)


def _response_bytes(response):
    try:
        return json.dumps(response, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
    except Exception:
        safe = _json_safe(response)
        return json.dumps(safe, ensure_ascii=False, allow_nan=False).encode("utf-8")

def _set_rfield(h, rec, field, val):
    """Safely try SetRField, silently skip on failure."""
    try: vs.SetRField(h, rec, field, str(val))
    except Exception: pass

def _rgb_to_idx(r, g, b):
    """Convert RGB (0-65535) to VW color index."""
    if hasattr(vs, 'RGBToColorIndexN'):
        return vs.RGBToColorIndexN(r, g, b, False)
    if hasattr(vs, 'RGBToColorIndex'):
        return vs.RGBToColorIndex(r, g, b)
    return 0

# === HANDLERS ===

def handle_run_script(p):
    old = sys.stdout
    sys.stdout = cap = io.StringIO()
    try:
        exec(p.get("code", ""), {"vs": vs, "__builtins__": __builtins__})
        return _ok(cap.getvalue() or "OK")
    except Exception: return _err(traceback.format_exc())
    finally: sys.stdout = old

def handle_create_object(p):
    t = p.get("object_type", "").lower()
    x1, y1, x2, y2 = p.get("x1", 0), p.get("y1", 0), p.get("x2", 100), p.get("y2", 100)
    try:
        if t == "rect": vs.Rect(x1, y1, x2, y2)
        elif t == "circle": vs.ArcByCenter(x1, y1, p.get("radius", 50), 0, 360)
        elif t == "oval": vs.Oval(x1, y1, x2, y2)
        elif t == "line": vs.MoveTo(x1, y1); vs.LineTo(x2, y2)
        elif t == "arc": vs.ArcByCenter(x1, y1, p.get("radius", 50), p.get("start_angle", 0), p.get("sweep_angle", 90))
        elif t == "polygon":
            pts = p.get("points", [])
            if len(pts) < 2: return _err("Polygon requires at least 2 points")
            (vs.ClosePoly if p.get("closed", True) else vs.OpenPoly)()
            vs.BeginPoly()
            for pt in pts: vs.AddPoint(pt[0], pt[1])
            vs.EndPoly()
        else: return _err(f"Unknown type: {t}. Use: rect, circle, oval, line, arc, polygon")
        h = vs.LNewObj()
        if h and p.get("name"): vs.SetName(h, p["name"])
        if h and p.get("class_name"): vs.SetClass(h, p["class_name"])
        vs.ReDrawAll()
        return _ok(f"Created {t}, handle: {_reg(h)}")
    except Exception: return _err(traceback.format_exc())

def handle_get_layers(p):
    layers, h = [], vs.FLayer()
    while h is not None:
        try: layers.append({"name": vs.GetLName(h), "visible": vs.GetLVis(h) == 0})
        except Exception: pass
        h = vs.NextLayer(h)
    return _ok(layers)

def handle_get_objects(p):
    target_layer, target_type, limit = p.get("layer", ""), p.get("object_type", "").lower(), p.get("limit", 100)
    objs = []
    def collect(lh):
        obj = vs.FInLayer(lh)
        while obj is not None and len(objs) < limit:
            info = _obj_info(obj)
            if not target_type or info["type"] == target_type: objs.append(info)
            obj = vs.NextObj(obj)
    if target_layer:
        lh = vs.GetObject(target_layer)
        if lh is None: return _err(f"Layer '{target_layer}' not found")
        collect(lh)
    else:
        lh = vs.FLayer()
        while lh is not None and len(objs) < limit:
            collect(lh); lh = vs.NextLayer(lh)
    return _ok(objs)

def handle_set_property(p):
    h = _get(p.get("handle", ""))
    if h is None: return _err(f"Handle '{p.get('handle')}' not found. Use vw_get_objects first.")
    prop, val = p.get("property_name", ""), p.get("value", "")
    try:
        if prop == "name": vs.SetName(h, val)
        elif prop == "class": vs.SetClass(h, val)
        elif prop in ("fillColor", "penColor"):
            r, g, b = [int(x.strip()) for x in val.split(",")]
            idx = _rgb_to_idx(r, g, b)
            (vs.SetFillForeColor if prop == "fillColor" else vs.SetPenForeColor)(h, idx)
        elif prop == "lineWeight": vs.SetLW(h, int(val))
        elif prop == "opacity": vs.SetOpacity(h, int(val))
        else: return _err(f"Unknown property: {prop}. Use: name, class, fillColor, penColor, lineWeight, opacity")
        vs.ReDrawAll()
        return _ok(f"Set {prop}={val}")
    except Exception: return _err(traceback.format_exc())

def handle_find_objects(p):
    results, limit = [], p.get("limit", 100)
    def collect(h):
        if len(results) < limit: results.append(_obj_info(h))
    try:
        vs.ForEachObject(collect, p.get("criteria", "ALL"))
        return _ok(results)
    except Exception: return _err(traceback.format_exc())

def handle_manage_classes(p):
    action = p.get("action", "list").lower()
    try:
        if action == "list":
            return _ok([vs.ClassList(i) for i in range(1, vs.ClassNum() + 1)])
        elif action == "create":
            vs.NameClass(p.get("class_name", "")); return _ok(f"Created class '{p.get('class_name')}'")
        elif action == "delete":
            vs.DelClass(p.get("class_name", "")); return _ok(f"Deleted class '{p.get('class_name')}'")
        else: return _err("Unknown action. Use: list, create, delete")
    except Exception: return _err(traceback.format_exc())

def handle_worksheet(p):
    action, ws_name = p.get("action", "list").lower(), p.get("worksheet_name", "")
    row, col, num_rows = p.get("row", 1), p.get("col", 1), p.get("num_rows", 10)
    try:
        if action == "list":
            ws = []
            vs.ForEachObject(lambda h: ws.append(vs.GetName(h)), "T=WORKSHEET")
            return _ok(ws)
        ws_h = vs.GetObject(ws_name)
        if ws_h is None: return _err(f"Worksheet '{ws_name}' not found")
        if action == "read":
            return _ok({"row": row, "col": col, "value": str(vs.GetWSCellValue(ws_h, row, col))})
        elif action == "read_range":
            data = []
            for r in range(row, row + num_rows):
                rd = []
                for c in range(col, col + 20):
                    v = vs.GetWSCellValue(ws_h, r, c)
                    if v is None or str(v).strip() == "": break
                    rd.append(str(v))
                if not rd: break
                data.append(rd)
            return _ok(data)
        elif action == "write":
            vs.SetWSCellValue(ws_h, row, col, p.get("value", "")); vs.ReDrawAll()
            return _ok(f"Set cell ({row},{col})")
        else: return _err("Unknown action. Use: list, read, read_range, write")
    except Exception: return _err(traceback.format_exc())

def handle_symbol(p):
    action = p.get("action", "list").lower()
    try:
        if action == "list":
            syms = set()
            h = vs.FSymDef()
            while h is not None:
                syms.add(vs.GetName(h)); h = vs.NextSymDef(h)
            return _ok(list(syms))
        elif action == "insert":
            vs.Symbol(p.get("symbol_name", ""), p.get("x", 0), p.get("y", 0), p.get("rotation", 0))
            h = vs.LNewObj(); vs.ReDrawAll()
            return _ok(f"Inserted symbol, handle: {_reg(h)}")
        else: return _err("Unknown action. Use: list, insert")
    except Exception: return _err(traceback.format_exc())

def handle_export(p):
    fmt, fp = p.get("format", "").lower(), p.get("file_path", "")
    if not fp: return _err("file_path is required")
    menu = {"pdf": "Export PDF", "dxf": "Export DXF/DWG", "dwg": "Export DXF/DWG", "image": "Export Image File"}
    if fmt not in menu: return _err(f"Unknown format: {fmt}. Use: pdf, dxf, dwg, image")
    try:
        vs.DoMenuTextByName(menu[fmt], 0)
        return _ok(f"{fmt.upper()} export dialog opened. Save to: {fp}")
    except Exception: return _err(traceback.format_exc())

def handle_import_file(p):
    fp, fmt = p.get("file_path", ""), p.get("format", "auto").lower()
    if not fp: return _err("file_path is required")
    if not os.path.exists(fp): return _err(f"File not found: {fp}")
    if fmt == "auto": fmt = os.path.splitext(fp)[1].lstrip(".").lower()
    try:
        if fmt in ("dxf", "dwg"):
            vs.ImportDXFDWGFile(fp, False); vs.ReDrawAll()
            return _ok(f"Imported {fmt.upper()}: {fp}")
        elif fmt in ("png", "jpg", "jpeg", "tif", "tiff", "bmp"):
            vs.ImportImageFile(fp, (0, 0)); vs.ReDrawAll()
            return _ok(f"Imported image: {fp}, handle: {_reg(vs.LNewObj())}")
        else: return _err(f"Unsupported format: {fmt}. Use: dxf, dwg, png, jpg")
    except Exception: return _err(traceback.format_exc())

def handle_get_document_info(p):
    try:
        info = {"filename": vs.GetFName() or "Untitled", "filepath": vs.GetFPathName() or "",
                "layers": [], "layer_count": 0, "total_objects": 0}
        lh = vs.FLayer()
        while lh is not None:
            info["layer_count"] += 1
            info["layers"].append(vs.GetLName(lh))
            oh = vs.FInLayer(lh)
            while oh is not None: info["total_objects"] += 1; oh = vs.NextObj(oh)
            lh = vs.NextLayer(lh)
        return _ok(info)
    except Exception: return _err(traceback.format_exc())

def handle_screenshot(p):
    fp = p.get("file_path", "") or os.path.join(SCREENSHOT_DIR, "screenshot.png")
    try:
        os.makedirs(os.path.dirname(fp), exist_ok=True)
    except OSError:
        pass
    try:
        if hasattr(vs, 'ExportImageFile'): vs.ExportImageFile(fp)
        else: vs.DoMenuTextByName("Export Image File", 0)
        return _ok(fp)
    except Exception: return _err(traceback.format_exc())


def handle_stop(p):
    global _SHOULD_STOP
    _SHOULD_STOP = True
    return _ok("Listener stop requested")

def _iter_selected():
    """Iterate selected objects using FSObject/NextSObj."""
    h = vs.FSObject() if hasattr(vs, 'FSObject') else vs.FSActLayer()
    while h is not None:
        yield h
        h = vs.NextSObj(h)

def handle_selection(p):
    action, criteria = p.get("action", "get").lower(), p.get("criteria", "")
    try:
        if action == "get":
            return _ok([_obj_info(h) for i, h in enumerate(_iter_selected()) if i < 200])
        elif action == "select":
            if not criteria: return _err("criteria required")
            vs.DSelectAll(); vs.SelectObj(criteria); vs.ReDrawAll()
            return _ok(f"Selected {sum(1 for _ in _iter_selected())} objects")
        elif action == "clear":
            vs.DSelectAll(); vs.ReDrawAll(); return _ok("Selection cleared")
        elif action == "delete":
            to_del = list(_iter_selected())
            for h in to_del: vs.DelObject(h)
            vs.ReDrawAll(); return _ok(f"Deleted {len(to_del)} objects")
        elif action == "move":
            if not criteria: return _err("Provide dx,dy (e.g. '100,50')")
            dx, dy = [float(x) for x in criteria.split(",")]
            count = 0
            for h in _iter_selected(): vs.HMove(h, dx, dy); count += 1
            vs.ReDrawAll(); return _ok(f"Moved {count} objects by ({dx},{dy})")
        elif action == "duplicate":
            handles = []
            for h in _iter_selected():
                nh = vs.HDuplicate(h, 10, 10)
                handles.append(_reg(nh))
            vs.ReDrawAll(); return _ok(f"Duplicated {len(handles)} objects: {handles}")
        else: return _err("Unknown action. Use: get, select, clear, delete, move, duplicate")
    except Exception: return _err(traceback.format_exc())

# === PARAMETRIC ARCHITECTURAL HANDLERS ===

def handle_create_wall(p):
    sx, sy, ex, ey = p.get("start_x", 0), p.get("start_y", 0), p.get("end_x", 1000), p.get("end_y", 0)
    height, thickness = p.get("height", 3000), p.get("thickness", 200)
    try:
        vs.Wall(sx, sy, ex, ey)
        h = vs.LNewObj()
        if h is None: return _err("Failed to create wall")
        _set_rfield(h, 'Wall', 'Height', height)
        _set_rfield(h, 'Wall', 'Thickness', thickness)
        _set_rfield(h, 'Wall', 'Width', thickness)
        if p.get("style_name"): _set_rfield(h, 'Wall', 'Style', p["style_name"])
        vs.ReDrawAll()
        return _ok(f"Created wall ({sx},{sy})->({ex},{ey}), h={height}, t={thickness}, handle: {_reg(h)}")
    except Exception: return _err(traceback.format_exc())

def handle_insert_door(p):
    try:
        h = vs.CreateCustomObjectN('Door', (p.get("x", 0), p.get("y", 0)), p.get("rotation", 0), False)
        if h is None: return _err("Failed to create Door. Is the plugin available?")
        _set_rfield(h, 'Door', 'Width', p.get("width", 900))
        _set_rfield(h, 'Door', 'Height', p.get("height", 2100))
        vs.ReDrawAll()
        return _ok(f"Inserted door {p.get('width',900)}x{p.get('height',2100)}, handle: {_reg(h)}")
    except Exception: return _err(traceback.format_exc())

def handle_insert_window(p):
    try:
        h = vs.CreateCustomObjectN('Window', (p.get("x", 0), p.get("y", 0)), p.get("rotation", 0), False)
        if h is None: return _err("Failed to create Window. Is the plugin available?")
        _set_rfield(h, 'Window', 'Width', p.get("width", 1200))
        _set_rfield(h, 'Window', 'Height', p.get("height", 1500))
        _set_rfield(h, 'Window', 'Elevation In Wall', p.get("sill_height", 900))
        _set_rfield(h, 'Window', 'SillHeight', p.get("sill_height", 900))
        vs.ReDrawAll()
        return _ok(f"Inserted window, handle: {_reg(h)}")
    except Exception: return _err(traceback.format_exc())

def handle_create_slab(p):
    pts, thickness, elev = p.get("points", []), p.get("thickness", 200), p.get("elevation", 0)
    if len(pts) < 3: return _err("Slab requires at least 3 points")
    try:
        vs.BeginXtrd(elev, elev + thickness)
        vs.ClosePoly(); vs.BeginPoly()
        for pt in pts: vs.AddPoint(pt[0], pt[1])
        vs.EndPoly(); vs.EndXtrd()
        h = vs.LNewObj(); vs.ReDrawAll()
        return _ok(f"Created slab, {len(pts)} pts, t={thickness}, handle: {_reg(h)}")
    except Exception: return _err(traceback.format_exc())

def handle_create_roof(p):
    pts = p.get("points", [])
    bh, slope, oh, thick = p.get("bearing_height", 3000), p.get("slope", 30), p.get("overhang", 500), p.get("thickness", 200)
    if len(pts) < 3: return _err("Roof requires at least 3 points")
    try:
        cx, cy = sum(x[0] for x in pts)/len(pts), sum(x[1] for x in pts)/len(pts)
        h = vs.CreateCustomObjectN('Roof', (cx, cy), 0, False)
        if h is not None:
            for f, v in [('Slope', slope), ('Bearing Height', bh), ('Overhang', oh), ('Thickness', thick)]:
                _set_rfield(h, 'Roof', f, v)
            vs.ReDrawAll()
            return _ok(f"Created roof, slope={slope}deg, handle: {_reg(h)}")
        # Fallback: flat extrusion
        vs.BeginXtrd(bh, bh + thick)
        vs.ClosePoly(); vs.BeginPoly()
        for pt in pts: vs.AddPoint(pt[0], pt[1])
        vs.EndPoly(); vs.EndXtrd()
        vs.ReDrawAll()
        return _ok(f"Created flat roof at z={bh}, handle: {_reg(vs.LNewObj())}")
    except Exception: return _err(traceback.format_exc())

def handle_inspect_object(p):
    hid, pname = p.get("handle", ""), p.get("plugin_name", "")
    try:
        h, temp = None, False
        if hid:
            h = _get(hid)
            if h is None: return _err(f"Handle '{hid}' not found")
        elif pname:
            h = vs.CreateCustomObjectN(pname, (0, 0), 0, False)
            if h is None: return _err(f"Cannot create '{pname}'. Check plugin name.")
            temp = True
        else: return _err("Provide handle or plugin_name")

        tn = vs.GetTypeN(h) if hasattr(vs, 'GetTypeN') else vs.GetType(h)
        info = {"type": TYPE_NAMES.get(tn, f"type_{tn}"), "type_id": tn,
                "name": vs.GetName(h) or "", "fields": []}
        # Enumerate parametric record fields
        try:
            rec = vs.GetParametricRecord(h)
            if rec:
                rec_name = pname or info["name"]
                for i in range(1, vs.NumFields(rec) + 1):
                    fname = vs.GetFldName(rec, i)
                    try: fval = str(vs.GetRField(h, rec_name, fname))
                    except Exception: fval = ""
                    info["fields"].append({"name": fname, "value": fval})
        except Exception as e:
            info["fields_error"] = str(e)
        if temp: vs.DelObject(h); vs.ReDrawAll()
        return _ok(info)
    except Exception: return _err(traceback.format_exc())

# === DISPATCHER ===
HANDLERS = {
    "run_script": handle_run_script, "create_object": handle_create_object,
    "get_layers": handle_get_layers, "get_objects": handle_get_objects,
    "set_property": handle_set_property, "find_objects": handle_find_objects,
    "manage_classes": handle_manage_classes, "worksheet": handle_worksheet,
    "symbol": handle_symbol, "export": handle_export,
    "import_file": handle_import_file, "get_document_info": handle_get_document_info,
    "screenshot": handle_screenshot, "stop": handle_stop, "selection": handle_selection,
    "create_wall": handle_create_wall, "insert_door": handle_insert_door,
    "insert_window": handle_insert_window, "create_slab": handle_create_slab,
    "create_roof": handle_create_roof, "inspect_object": handle_inspect_object,
}
HANDLERS["ping"] = lambda p: _ok({"pong": True, "handlers": len(HANDLERS), "version": __VERSION__})

def dispatch(req):
    if not isinstance(req, dict):
        return {"id": "", "success": False, "error": "Request must be a JSON object"}
    handler = HANDLERS.get(req.get("action", ""))
    if not handler:
        return {"id": req.get("id", ""), "success": False, "error": f"Unknown action: {req.get('action')}"}
    try:
        result = handler(req.get("params", {}))
    except Exception:
        result = {"success": False, "error": traceback.format_exc()}
    result["id"] = req.get("id", "")
    return result


# === NON-BLOCKING SOCKET LAYER ===
# Single event loop (selectors). Each connection buffers bytes until a full
# length-prefixed JSON frame is available; we decode and dispatch on this
# listener thread, then queue the response.

class _ClientState:
    __slots__ = ("rbuf", "wbuf", "need", "max_frame_bytes")
    def __init__(self, max_frame_bytes=None):
        self.rbuf = bytearray()
        self.wbuf = bytearray()
        self.need = None  # current frame body length, or None if waiting for header
        self.max_frame_bytes = max_frame_bytes or MAX_FRAME_BYTES

    def feed(self, chunk):
        self.rbuf.extend(chunk)

    def pop_frame(self):
        if self.need is None:
            if len(self.rbuf) < 4:
                return None
            self.need = struct.unpack(">I", bytes(self.rbuf[:4]))[0]
            del self.rbuf[:4]
            if self.need <= 0:
                raise ProtocolError("invalid frame length {n}".format(n=self.need))
            if self.need > self.max_frame_bytes:
                raise ProtocolError(
                    "frame length {n} exceeds VW_MCP_MAX_FRAME_BYTES={m}".format(
                        n=self.need, m=self.max_frame_bytes
                    )
                )
        if len(self.rbuf) < self.need:
            return None
        body = bytes(self.rbuf[: self.need])
        del self.rbuf[: self.need]
        self.need = None
        return body

    def enqueue(self, payload: bytes):
        if len(payload) > self.max_frame_bytes:
            payload = _response_bytes(_err("response exceeded max frame size"))
        self.wbuf.extend(struct.pack(">I", len(payload)))
        self.wbuf.extend(payload)


def _stop_requested():
    if _SHOULD_STOP:
        return True
    try:
        if os.path.exists(STOP_FILE):
            try: os.remove(STOP_FILE)
            except OSError: pass
            return True
    except Exception:
        pass
    return False


def _drop(sel, fileobj):
    try: sel.unregister(fileobj)
    except (KeyError, ValueError): pass
    try: fileobj.close()
    except OSError: pass


def _client_events(state):
    events = selectors.EVENT_READ
    if state.wbuf:
        events |= selectors.EVENT_WRITE
    return events


def _set_client_events(sel, fileobj, state):
    try:
        sel.modify(fileobj, _client_events(state), data=state)
    except (KeyError, ValueError, OSError):
        pass


def _has_pending_writes(sel):
    try:
        for key in sel.get_map().values():
            state = key.data
            if state is not None and state.wbuf:
                return True
    except Exception:
        return False
    return False


class _ListenerServer:
    def __init__(self, show_alerts=True):
        self.show_alerts = show_alerts
        self.server_sock = None
        self.sel = None
        self.closed = False

    def start(self):
        global _SHOULD_STOP
        _SHOULD_STOP = False

        if vs is None:
            if self.show_alerts:
                _alert("VW MCP Listener must be run inside Vectorworks, where the 'vs' module is available.")
            return False
        if _CONFIG_ERROR:
            if self.show_alerts:
                _alert("VW MCP configuration error: {e}".format(e=_CONFIG_ERROR))
            return False

        try:
            os.makedirs(STOP_DIR, exist_ok=True)
        except OSError as e:
            if self.show_alerts:
                _alert("VW MCP could not create stop directory:\n{d}\n{e}".format(d=STOP_DIR, e=e))
            return False
        _stop_requested()  # clear any stale STOP from a previous session

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_sock.bind((HOST, PORT))
        except OSError as e:
            if self.show_alerts:
                _alert(
                    "VW MCP failed to bind {h}:{p}\n{e}\n\n"
                    "Is another listener already running? Close it or set VW_MCP_PORT.".format(
                        h=HOST, p=PORT, e=e
                    )
                )
            server_sock.close()
            return False
        server_sock.listen(8)
        server_sock.setblocking(False)

        sel = selectors.DefaultSelector()
        sel.register(server_sock, selectors.EVENT_READ, data=None)

        self.server_sock = server_sock
        self.sel = sel
        self.closed = False
        _STATE.listener_server = self

        if self.show_alerts:
            _alert(
                "VW MCP Listener STARTED (socket)\n"
                "Listening on {h}:{p}\n"
                "Version {v}\n"
                "Stop: create STOP file in:\n{d}".format(h=HOST, p=PORT, v=__VERSION__, d=STOP_DIR)
            )
        return True

    def _handle_key(self, key, mask):
        fileobj = key.fileobj

        if key.data is None:
            try:
                conn, _addr = fileobj.accept()
            except BlockingIOError:
                return
            conn.setblocking(False)
            self.sel.register(conn, selectors.EVENT_READ, data=_ClientState())
            return

        state = key.data

        if mask & selectors.EVENT_READ:
            try:
                chunk = fileobj.recv(65536)
            except (BlockingIOError, InterruptedError):
                pass
            except (ConnectionError, OSError):
                _drop(self.sel, fileobj)
                return
            else:
                if chunk == b"":
                    _drop(self.sel, fileobj)
                    return
                state.feed(chunk)
                while True:
                    try:
                        frame = state.pop_frame()
                    except ProtocolError:
                        _drop(self.sel, fileobj)
                        return
                    if frame is None:
                        break
                    rid = ""
                    try:
                        req = json.loads(frame.decode("utf-8"))
                        rid = req.get("id", "")
                        resp = dispatch(req)
                    except Exception as e:
                        resp = {"id": rid, "success": False, "error": "bad JSON: {}".format(e)}
                    state.enqueue(_response_bytes(resp))
                    _set_client_events(self.sel, fileobj, state)

        if mask & selectors.EVENT_WRITE and state.wbuf:
            try:
                sent = fileobj.send(state.wbuf)
                if sent == 0:
                    _drop(self.sel, fileobj)
                    return
                del state.wbuf[:sent]
                _set_client_events(self.sel, fileobj, state)
            except (BlockingIOError, InterruptedError):
                pass
            except (ConnectionError, OSError):
                _drop(self.sel, fileobj)

    def pump_once(self, timeout=0.0):
        if self.closed or self.sel is None:
            return False
        if _stop_requested() and not _has_pending_writes(self.sel):
            self.close()
            return False
        try:
            events = self.sel.select(timeout=timeout)
        except (OSError, ValueError):
            self.close()
            return False
        for key, mask in events:
            self._handle_key(key, mask)
        if _stop_requested() and not _has_pending_writes(self.sel):
            self.close()
            return False
        return not self.closed

    def pump(self, iterations=8, timeout=0.0):
        for index in range(max(1, iterations)):
            if not self.pump_once(timeout if index == 0 else 0.0):
                return False
        return True

    def serve_forever(self):
        try:
            while self.pump_once(timeout=0.1):
                pass
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        sel = self.sel
        server_sock = self.server_sock
        self.sel = None
        self.server_sock = None
        try:
            if sel is not None:
                for key in list(sel.get_map().values()):
                    try: key.fileobj.close()
                    except OSError: pass
        except Exception:
            pass
        try:
            if sel is not None:
                sel.close()
        except Exception:
            pass
        try:
            if server_sock is not None:
                server_sock.close()
        except OSError:
            pass
        if getattr(_STATE, "listener_server", None) is self:
            _STATE.listener_server = None
        if self.show_alerts:
            _alert("VW MCP Listener STOPPED.")


def main(show_alerts=True):
    server = _ListenerServer(show_alerts=show_alerts)
    if server.start():
        server.serve_forever()


def _listener_port_open():
    try:
        sock = socket.create_connection((HOST, PORT), timeout=0.2)
    except OSError:
        return False
    try:
        sock.close()
    except OSError:
        pass
    return True


def _existing_listener_healthy():
    request = {"id": "startup-ping", "action": "ping", "params": {}}
    payload = json.dumps(request).encode("utf-8")
    try:
        sock = socket.create_connection((HOST, PORT), timeout=0.6)
        sock.settimeout(0.6)
        try:
            sock.sendall(struct.pack(">I", len(payload)) + payload)
            header = b""
            while len(header) < 4:
                chunk = sock.recv(4 - len(header))
                if not chunk:
                    return False
                header += chunk
            size = struct.unpack(">I", header)[0]
            if size <= 0 or size > MAX_FRAME_BYTES:
                return False
            body = b""
            while len(body) < size:
                chunk = sock.recv(size - len(body))
                if not chunk:
                    return False
                body += chunk
            response = json.loads(body.decode("utf-8"))
            return bool(response.get("success") and response.get("result", {}).get("pong"))
        finally:
            sock.close()
    except Exception:
        return False


def _write_stop_file():
    try:
        os.makedirs(STOP_DIR, exist_ok=True)
        with open(STOP_FILE, "w") as f:
            f.write("stop\n")
        return True
    except Exception:
        return False


def _report_existing_or_stale_listener():
    if not _listener_port_open():
        return False
    if _existing_listener_healthy():
        _message("VW MCP Listener is already healthy on {h}:{p}".format(h=HOST, p=PORT))
        return True
    if _write_stop_file():
        _message(
            "VW MCP port {h}:{p} is open but not answering. "
            "A STOP file was written; wait a few seconds, then restart Vectorworks "
            "if the port still times out.".format(h=HOST, p=PORT)
        )
    else:
        _message(
            "VW MCP port {h}:{p} is open but not answering, and the STOP file "
            "could not be written. Restart Vectorworks, then run this launcher again.".format(
                h=HOST, p=PORT
            )
        )
    return True


def start_background():
    thread = getattr(_STATE, "listener_thread", None)
    if thread is not None and thread.is_alive():
        _message("VW MCP Listener is already running on {h}:{p}".format(h=HOST, p=PORT))
        return

    if _report_existing_or_stale_listener():
        return

    thread = threading.Thread(
        target=main,
        kwargs={"show_alerts": False},
        name="VW MCP Listener",
        daemon=True,
    )
    _STATE.listener_thread = thread
    thread.start()
    _message(
        "VW MCP Listener starting in background on {h}:{p}. "
        "Use vw_ping from Claude Code to confirm.".format(h=HOST, p=PORT)
    )


def _set_dialog_text(dialog_id, item_id, text):
    try:
        if hasattr(vs, "SetControlText"):
            vs.SetControlText(dialog_id, item_id, text)
        elif hasattr(vs, "SetItemText"):
            vs.SetItemText(dialog_id, item_id, text)
    except Exception:
        pass


def start_dialog():
    if getattr(_STATE, "dialog_running", False):
        _message("VW MCP Listener dialog is already running.")
        return
    if _report_existing_or_stale_listener():
        return

    required = (
        "CreateLayout",
        "CreateStaticText",
        "SetFirstLayoutItem",
        "SetBelowItem",
        "RunLayoutDialog",
        "RegisterDialogForTimerEvents",
        "DeregisterDialogFromTimerEvents",
    )
    missing = [name for name in required if vs is None or not hasattr(vs, name)]
    if missing:
        _alert(
            "VW MCP dialog-pump mode needs Vectorworks dialog APIs that are not available:\n{m}".format(
                m=", ".join(missing)
            )
        )
        return

    server = _ListenerServer(show_alerts=False)
    if not server.start():
        _alert("VW MCP Listener could not start on {h}:{p}.".format(h=HOST, p=PORT))
        return

    dialog_id = None
    timer_registered = [False]
    status_item = 4
    hint_item = 5
    setup_event = getattr(vs, "SetupDialogC", 12255)

    try:
        dialog_id = vs.CreateLayout("VW MCP Listener", False, "Stop", "")
        vs.CreateStaticText(
            dialog_id,
            status_item,
            "Listening on {h}:{p} - version {v}".format(h=HOST, p=PORT, v=__VERSION__),
            48,
        )
        vs.CreateStaticText(
            dialog_id,
            hint_item,
            "Keep this dialog open while Claude Code controls Vectorworks.",
            56,
        )
        vs.SetFirstLayoutItem(dialog_id, status_item)
        vs.SetBelowItem(dialog_id, status_item, hint_item, 0, 0)

        def dialog_handler(item, data):
            if item == setup_event:
                try:
                    vs.RegisterDialogForTimerEvents(dialog_id, DIALOG_TIMER_MS)
                    timer_registered[0] = True
                except Exception as e:
                    _set_dialog_text(dialog_id, status_item, "Timer registration failed: {e}".format(e=e))
                server.pump(iterations=4, timeout=0.0)
                return

            if item in (1, 2):
                global _SHOULD_STOP
                _SHOULD_STOP = True
                return

            if not server.pump(iterations=8, timeout=0.0):
                if timer_registered[0]:
                    try:
                        vs.DeregisterDialogFromTimerEvents(dialog_id)
                    except Exception:
                        pass
                    timer_registered[0] = False
                _set_dialog_text(dialog_id, status_item, "Stopped. Close this dialog to finish.")

        _STATE.dialog_running = True
        _message("VW MCP Listener dialog running on {h}:{p}".format(h=HOST, p=PORT))
        vs.RunLayoutDialog(dialog_id, dialog_handler)
    finally:
        if dialog_id is not None and timer_registered[0]:
            try:
                vs.DeregisterDialogFromTimerEvents(dialog_id)
            except Exception:
                pass
        server.close()
        _STATE.dialog_running = False
        _message("VW MCP Listener stopped.")


def _autostart_mode():
    mode = os.environ.get("VW_MCP_MODE", "").strip().lower()
    if mode:
        return mode
    if os.environ.get("VW_MCP_BACKGROUND", "").lower() in ("1", "true", "yes"):
        return "dialog"
    return "foreground"


if os.environ.get("VW_MCP_NO_AUTOSTART", "").lower() not in ("1", "true", "yes"):
    _mode = _autostart_mode()
    if _mode in ("dialog", "timer", "mainthread", "main-thread"):
        start_dialog()
    elif _mode in ("background", "thread"):
        start_background()
    else:
        main()
