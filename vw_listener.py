"""
Vectorworks 2024/2025 MCP Listener - runs inside Vectorworks on the main thread.

Opens a TCP socket (default 127.0.0.1:9877) and serves MCP requests using
non-blocking I/O via selectors. All vs.* calls execute on the main thread,
which is the only thread where the vs module is safe.

INSTALL OPTIONS
  A) Quick - Tools > Plug-ins > Script Editor > Python > paste > Run
  B) Persistent menu command - Tools > Plug-ins > Plug-in Manager >
     New > Menu Command, paste this file as the script. Then
     Tools > Workspaces > Edit Current Workspace > Menus and drag the
     new command into a menu. Click it once per VW session to start.

STOP: create a file named STOP in the stop-file folder printed at startup,
or close the document / quit Vectorworks.

CONFIG (env vars, all optional):
  VW_MCP_HOST       default 127.0.0.1
  VW_MCP_PORT       default 9877
  VW_MCP_STOP_DIR   default ~/.vectorworks-mcp
  VW_MCP_MAX_FRAME_BYTES default 16777216
"""
try:
    import vs
except ModuleNotFoundError:
    vs = None

import io, json, math, os, selectors, socket, struct, sys, traceback

__VERSION__ = "0.3.0-socket"

# === CONFIGURATION ===
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9877
DEFAULT_MAX_FRAME_BYTES = 16 * 1024 * 1024


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
except ValueError as e:
    _CONFIG_ERROR = str(e)
    HOST = DEFAULT_HOST
    PORT = DEFAULT_PORT
    MAX_FRAME_BYTES = DEFAULT_MAX_FRAME_BYTES

STOP_DIR = os.environ.get("VW_MCP_STOP_DIR") or os.path.join(
    os.path.expanduser("~"), ".vectorworks-mcp"
)
STOP_FILE = os.path.join(STOP_DIR, "STOP")
SCREENSHOT_DIR = STOP_DIR
_SHOULD_STOP = False

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
# Single main-thread event loop (selectors). Each connection buffers bytes
# until a full length-prefixed JSON frame is available; we decode, dispatch
# on this thread (safe for vs.*), and queue the response.

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


def main():
    global _SHOULD_STOP
    _SHOULD_STOP = False

    if vs is None:
        _alert("VW MCP Listener must be run inside Vectorworks, where the 'vs' module is available.")
        return
    if _CONFIG_ERROR:
        _alert("VW MCP configuration error: {e}".format(e=_CONFIG_ERROR))
        return

    try:
        os.makedirs(STOP_DIR, exist_ok=True)
    except OSError as e:
        _alert("VW MCP could not create stop directory:\n{d}\n{e}".format(d=STOP_DIR, e=e))
        return
    _stop_requested()  # clear any stale STOP from a previous session

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_sock.bind((HOST, PORT))
    except OSError as e:
        _alert(
            "VW MCP failed to bind {h}:{p}\n{e}\n\n"
            "Is another listener already running? Close it or set VW_MCP_PORT.".format(
                h=HOST, p=PORT, e=e
            )
        )
        server_sock.close()
        return
    server_sock.listen(8)
    server_sock.setblocking(False)

    sel = selectors.DefaultSelector()
    sel.register(server_sock, selectors.EVENT_READ, data=None)

    _alert(
        "VW MCP Listener STARTED (socket)\n"
        "Listening on {h}:{p}\n"
        "Version {v}\n"
        "Stop: create STOP file in:\n{d}".format(h=HOST, p=PORT, v=__VERSION__, d=STOP_DIR)
    )

    try:
        while True:
            if _stop_requested() and not _has_pending_writes(sel):
                break
            events = sel.select(timeout=0.1)
            for key, mask in events:
                fileobj = key.fileobj

                # Server socket - accept new clients
                if key.data is None:
                    try:
                        conn, _addr = fileobj.accept()
                    except BlockingIOError:
                        continue
                    conn.setblocking(False)
                    sel.register(conn, selectors.EVENT_READ, data=_ClientState())
                    continue

                state = key.data

                # Readable: consume bytes, decode frames, dispatch, enqueue response
                if mask & selectors.EVENT_READ:
                    try:
                        chunk = fileobj.recv(65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except (ConnectionError, OSError):
                        _drop(sel, fileobj)
                        continue
                    if chunk == b"":
                        _drop(sel, fileobj)
                        continue
                    state.feed(chunk)
                    while True:
                        try:
                            frame = state.pop_frame()
                        except ProtocolError:
                            _drop(sel, fileobj)
                            break
                        if frame is None:
                            break
                        rid = ""
                        try:
                            req = json.loads(frame.decode("utf-8"))
                            rid = req.get("id", "")
                            resp = dispatch(req)  # main thread - safe for vs.*
                        except Exception as e:
                            resp = {"id": rid, "success": False, "error": "bad JSON: {}".format(e)}
                        state.enqueue(_response_bytes(resp))
                        _set_client_events(sel, fileobj, state)

                # Writable: flush pending bytes
                if mask & selectors.EVENT_WRITE and state.wbuf:
                    try:
                        sent = fileobj.send(state.wbuf)
                        if sent == 0:
                            _drop(sel, fileobj)
                            continue
                        del state.wbuf[:sent]
                        _set_client_events(sel, fileobj, state)
                    except (BlockingIOError, InterruptedError):
                        pass
                    except (ConnectionError, OSError):
                        _drop(sel, fileobj)
    finally:
        try:
            for key in list(sel.get_map().values()):
                try: key.fileobj.close()
                except OSError: pass
        except Exception:
            pass
        try: sel.close()
        except Exception: pass
        try: server_sock.close()
        except OSError: pass
        _alert("VW MCP Listener STOPPED.")


if os.environ.get("VW_MCP_NO_AUTOSTART", "").lower() not in ("1", "true", "yes"):
    main()
