"""
Vectorworks 2025 MCP Server — Connects Claude Code to Vectorworks via TCP.

Speaks a length-prefixed JSON protocol (4-byte BE length + UTF-8 JSON body)
to vw_listener.py running inside Vectorworks.

Usage: claude mcp add vectorworks -- python server.py

Env vars (all optional):
  VW_MCP_HOST      default 127.0.0.1
  VW_MCP_PORT      default 9877
  VW_MCP_TIMEOUT   per-request timeout in seconds, default 60
"""

import json, os, socket, struct, threading, uuid
from fastmcp import FastMCP

mcp = FastMCP("Vectorworks 2025")

HOST = os.environ.get("VW_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("VW_MCP_PORT", "9877"))
TIMEOUT = float(os.environ.get("VW_MCP_TIMEOUT", "60"))

# Persistent connection, guarded by a lock so concurrent MCP tool calls
# don't interleave frames on the same socket.
_sock = None
_lock = threading.Lock()


def _connect():
    global _sock
    if _sock is not None:
        return
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    s.connect((HOST, PORT))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    _sock = s


def _close():
    global _sock
    if _sock is not None:
        try:
            _sock.close()
        except OSError:
            pass
        _sock = None


def _recv_exact(n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = _sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Vectorworks closed the connection")
        buf.extend(chunk)
    return bytes(buf)


def _send_frame(payload: bytes):
    _sock.sendall(struct.pack(">I", len(payload)) + payload)


def _recv_frame() -> bytes:
    header = _recv_exact(4)
    (n,) = struct.unpack(">I", header)
    return _recv_exact(n)


def _send(action: str, params: dict = None) -> str:
    with _lock:
        # Retry once on connection error — common when listener restarts.
        for attempt in (0, 1):
            try:
                _connect()
                rid = uuid.uuid4().hex[:8]
                req = {"id": rid, "action": action, "params": params or {}}
                _send_frame(json.dumps(req).encode("utf-8"))
                resp = json.loads(_recv_frame().decode("utf-8"))
                if resp.get("success"):
                    v = resp.get("result", "OK")
                    return json.dumps(v, indent=2) if not isinstance(v, str) else v
                return f"VW Error: {resp.get('error', 'Unknown')}"
            except (ConnectionError, socket.timeout, OSError) as e:
                _close()
                if attempt == 0:
                    continue
                return (
                    f"Connection error: {e}. "
                    f"Is vw_listener.py running inside Vectorworks on {HOST}:{PORT}? "
                    "Open Tools > Plug-ins > Script Editor, paste vw_listener.py, Run."
                )


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
    """Capture viewport screenshot as PNG. Use Read tool to view it after.
    If file_path is empty, listener defaults to ~/.vectorworks-mcp/screenshot.png."""
    return _send("screenshot", {"file_path": file_path})


@mcp.tool
def vw_ping() -> str:
    """Health check. Returns listener version and handler count if connected."""
    return _send("ping")

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
