"""
Vectorworks 2025 MCP Server — Connects Claude Code to Vectorworks via file bridge.
Usage: claude mcp add vectorworks -- python server.py
"""

import json, os, time, uuid
from fastmcp import FastMCP

mcp = FastMCP("Vectorworks 2025")

BRIDGE_PATH = os.environ.get("VW_BRIDGE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge"))
REQ_DIR = os.path.join(BRIDGE_PATH, "requests")
RES_DIR = os.path.join(BRIDGE_PATH, "responses")


def _send(action: str, params: dict = None) -> str:
    os.makedirs(REQ_DIR, exist_ok=True)
    os.makedirs(RES_DIR, exist_ok=True)
    rid = uuid.uuid4().hex[:8]
    req_path = os.path.join(REQ_DIR, f"req_{rid}.json")
    res_path = os.path.join(RES_DIR, f"res_{rid}.json")
    with open(req_path, "w") as f:
        json.dump({"id": rid, "action": action, "params": params or {}}, f)
    deadline = time.time() + 60
    while time.time() < deadline:
        if os.path.exists(res_path):
            time.sleep(0.05)
            try:
                with open(res_path) as f:
                    r = json.load(f)
                os.remove(res_path)
                if r.get("success"):
                    v = r.get("result", "OK")
                    return json.dumps(v, indent=2) if not isinstance(v, str) else v
                return f"VW Error: {r.get('error', 'Unknown')}"
            except (json.JSONDecodeError, OSError) as e:
                return f"Error reading response: {e}"
        time.sleep(0.3)
    try: os.remove(req_path)
    except OSError: pass
    return "TIMEOUT: Vectorworks did not respond. Is vw_listener.py running in the Script Editor?"


@mcp.tool
def vw_run_script(code: str) -> str:
    """Execute arbitrary Python inside Vectorworks. The 'vs' module is available.
    Use print() to return output. Escape hatch for anything other tools don't cover.
    Example: vw_run_script("h = vs.FSActLayer()\\nprint(vs.GetName(h))")"""
    return _send("run_script", {"code": code})

@mcp.tool
def vw_create_object(object_type: str, x1: float = 0, y1: float = 0, x2: float = 100, y2: float = 100,
                     radius: float = 50, points: list | None = None, closed: bool = True,
                     start_angle: float = 0, sweep_angle: float = 90, name: str = "", class_name: str = "") -> str:
    """Create geometry: rect | circle | oval | line | arc | polygon.
    x1,y1/x2,y2: corners or start/end. radius: for circle/arc. points: [[x,y],...] for polygon."""
    return _send("create_object", {"object_type": object_type, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        "radius": radius, "points": points or [], "closed": closed, "start_angle": start_angle,
        "sweep_angle": sweep_angle, "name": name, "class_name": class_name})

@mcp.tool
def vw_get_layers() -> str:
    """List all layers with name and visibility."""
    return _send("get_layers")

@mcp.tool
def vw_get_objects(layer: str = "", object_type: str = "", limit: int = 100) -> str:
    """List objects. Filter by layer name, type (rect/line/wall/etc), with limit."""
    return _send("get_objects", {"layer": layer, "object_type": object_type, "limit": limit})

@mcp.tool
def vw_set_object_property(handle: str, property_name: str, value: str) -> str:
    """Set property on object. property_name: name|class|fillColor|penColor|lineWeight|opacity.
    Colors as 'r,g,b' (0-65535 range), converted to color index internally."""
    return _send("set_property", {"handle": handle, "property_name": property_name, "value": value})

@mcp.tool
def vw_find_objects(criteria: str, limit: int = 100) -> str:
    """Find objects using VW criteria: 'T=RECT', 'T=WALL', 'C=Furniture', 'L=Design Layer-1',
    'N=MyObject', '(PON IN [Door,Window])', 'T=RECT & C=Structure', 'ALL'."""
    return _send("find_objects", {"criteria": criteria, "limit": limit})

@mcp.tool
def vw_manage_classes(action: str, class_name: str = "") -> str:
    """action: 'list' | 'create' | 'delete'. class_name ignored for list."""
    return _send("manage_classes", {"action": action, "class_name": class_name})

@mcp.tool
def vw_worksheet(action: str, worksheet_name: str = "", row: int = 1, col: int = 1,
                 value: str = "", num_rows: int = 10) -> str:
    """Worksheet ops. action: list|read|write|read_range. read_range scans from (row,col) for num_rows."""
    return _send("worksheet", {"action": action, "worksheet_name": worksheet_name,
        "row": row, "col": col, "value": value, "num_rows": num_rows})

@mcp.tool
def vw_symbol(action: str, symbol_name: str = "", x: float = 0, y: float = 0, rotation: float = 0) -> str:
    """action: 'list' | 'insert'. Insert places symbol at (x,y) with rotation."""
    return _send("symbol", {"action": action, "symbol_name": symbol_name, "x": x, "y": y, "rotation": rotation})

@mcp.tool
def vw_export(format: str, file_path: str) -> str:
    """Export document. format: pdf|dxf|dwg|image. file_path: full output path."""
    return _send("export", {"format": format, "file_path": file_path})

@mcp.tool
def vw_import_file(file_path: str, format: str = "auto") -> str:
    """Import file. format: auto|dxf|dwg|image. Auto-detects from extension."""
    return _send("import_file", {"file_path": file_path, "format": format})

@mcp.tool
def vw_get_document_info() -> str:
    """Get document metadata: filename, filepath, layer count, object count, layer names."""
    return _send("get_document_info")

@mcp.tool
def vw_screenshot(file_path: str = "") -> str:
    """Capture viewport screenshot as PNG. Use Read tool to view it after. Defaults to bridge/screenshot.png."""
    return _send("screenshot", {"file_path": file_path or os.path.join(BRIDGE_PATH, "screenshot.png")})

@mcp.tool
def vw_selection(action: str, criteria: str = "") -> str:
    """Selection ops. action: get|select|clear|delete|move|duplicate.
    For 'select': criteria is VW criteria string. For 'move': criteria is 'dx,dy'."""
    return _send("selection", {"action": action, "criteria": criteria})

@mcp.tool
def vw_create_wall(start_x: float, start_y: float, end_x: float, end_y: float,
                   height: float = 3000, thickness: float = 200, style_name: str = "") -> str:
    """Create parametric wall. Coordinates in mm. Default 3m height, 200mm thick."""
    return _send("create_wall", {"start_x": start_x, "start_y": start_y, "end_x": end_x,
        "end_y": end_y, "height": height, "thickness": thickness, "style_name": style_name})

@mcp.tool
def vw_insert_door(x: float, y: float, width: float = 900, height: float = 2100, rotation: float = 0) -> str:
    """Insert parametric door. Place on/near wall for auto-insertion. Use vw_inspect_object for all params."""
    return _send("insert_door", {"x": x, "y": y, "width": width, "height": height, "rotation": rotation})

@mcp.tool
def vw_insert_window(x: float, y: float, width: float = 1200, height: float = 1500,
                     sill_height: float = 900, rotation: float = 0) -> str:
    """Insert parametric window. sill_height = floor to window bottom (default 900mm)."""
    return _send("insert_window", {"x": x, "y": y, "width": width, "height": height,
        "sill_height": sill_height, "rotation": rotation})

@mcp.tool
def vw_create_slab(points: list, thickness: float = 200, elevation: float = 0) -> str:
    """Create 3D floor slab from polygon. points: [[x,y],...] in mm. Min 3 points."""
    return _send("create_slab", {"points": points, "thickness": thickness, "elevation": elevation})

@mcp.tool
def vw_create_roof(points: list, bearing_height: float = 3000, slope: float = 30,
                   overhang: float = 500, thickness: float = 200) -> str:
    """Create roof from footprint. bearing_height: where roof starts. slope in degrees."""
    return _send("create_roof", {"points": points, "bearing_height": bearing_height,
        "slope": slope, "overhang": overhang, "thickness": thickness})

@mcp.tool
def vw_inspect_object(handle: str = "", plugin_name: str = "") -> str:
    """Discover ALL configurable parameters of any VW object. Provide handle OR plugin_name
    (e.g. 'Door','Window','Wall'). Returns field names, types, current values."""
    return _send("inspect_object", {"handle": handle, "plugin_name": plugin_name})

if __name__ == "__main__":
    mcp.run()
