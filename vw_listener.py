"""
Vectorworks 2025 MCP Listener — Paste into VW Script Editor (Python) and Run.
Tools > Plug-ins > Script Editor > Python > paste > Run
To stop: create a file named STOP in the bridge folder.
CONFIGURE BRIDGE_PATH BELOW before running.
"""
import vs
import io, json, os, sys, time, traceback

# === CONFIGURATION ===
BRIDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge")
# If the above doesn't work in VW, uncomment and set:
# BRIDGE_PATH = r"C:\Users\YourName\vectorworks-mcp\bridge"
REQ_DIR = os.path.join(BRIDGE_PATH, "requests")
RES_DIR = os.path.join(BRIDGE_PATH, "responses")
STOP_FILE = os.path.join(BRIDGE_PATH, "STOP")

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
    fp = p.get("file_path", "") or os.path.join(BRIDGE_PATH, "screenshot.png")
    try:
        if hasattr(vs, 'ExportImageFile'): vs.ExportImageFile(fp)
        else: vs.DoMenuTextByName("Export Image File", 0)
        return _ok(fp)
    except Exception: return _err(traceback.format_exc())

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
    "screenshot": handle_screenshot, "selection": handle_selection,
    "create_wall": handle_create_wall, "insert_door": handle_insert_door,
    "insert_window": handle_insert_window, "create_slab": handle_create_slab,
    "create_roof": handle_create_roof, "inspect_object": handle_inspect_object,
}

def dispatch(req):
    handler = HANDLERS.get(req.get("action", ""))
    if not handler: return {"id": req.get("id", ""), "success": False, "error": f"Unknown action: {req.get('action')}"}
    result = handler(req.get("params", {}))
    result["id"] = req.get("id", "")
    return result

# === MAIN LOOP ===
def main():
    os.makedirs(REQ_DIR, exist_ok=True)
    os.makedirs(RES_DIR, exist_ok=True)
    if os.path.exists(STOP_FILE): os.remove(STOP_FILE)
    vs.AlrtDialog(f"VW MCP Listener STARTED\nBridge: {BRIDGE_PATH}\nCreate STOP file to stop.")
    while True:
        if os.path.exists(STOP_FILE): os.remove(STOP_FILE); break
        try: files = sorted(f for f in os.listdir(REQ_DIR) if f.startswith("req_") and f.endswith(".json"))
        except OSError: files = []
        for fname in files:
            req_path = os.path.join(REQ_DIR, fname)
            try:
                with open(req_path, "r") as f: request = json.load(f)
                os.remove(req_path)
                rid = request.get("id", fname.replace("req_","").replace(".json",""))
                with open(os.path.join(RES_DIR, f"res_{rid}.json"), "w") as f:
                    json.dump(dispatch(request), f)
            except Exception as e:
                rid = fname.replace("req_","").replace(".json","")
                try:
                    with open(os.path.join(RES_DIR, f"res_{rid}.json"), "w") as f:
                        json.dump({"id": rid, "success": False, "error": str(e)}, f)
                except Exception: pass
                try: os.remove(req_path)
                except OSError: pass
        time.sleep(0.3)
    vs.AlrtDialog("VW MCP Listener STOPPED.")

main()
