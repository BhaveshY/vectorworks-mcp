"""
Vectorworks 2024/2025 MCP Server - connects Claude Code to Vectorworks via TCP.

Speaks a length-prefixed JSON protocol (4-byte big-endian length followed by
UTF-8 JSON) to vw_listener.py running inside Vectorworks.

Recommended setup:
  powershell -ExecutionPolicy Bypass -File .\\scripts\\bootstrap-claude-code.ps1 -Verify

Environment variables, all optional:
  VW_MCP_HOST             default 127.0.0.1
  VW_MCP_PORT             default 9877
  VW_MCP_TIMEOUT          per-request timeout in seconds, default 60
  VW_MCP_MAX_FRAME_BYTES  max protocol frame size, default 16777216
"""

import atexit
import json
import os
import socket
import struct
import sys
import threading
import uuid
from typing import Any, Literal, Optional

try:
    from fastmcp import FastMCP
except ModuleNotFoundError as exc:
    if exc.name != "fastmcp":
        raise
    FastMCP = None
    _FASTMCP_IMPORT_ERROR: Optional[BaseException] = exc
else:
    _FASTMCP_IMPORT_ERROR = None


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9877
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_FRAME_BYTES = 16 * 1024 * 1024


class ConfigError(ValueError):
    """Raised when environment configuration cannot be used safely."""


class ProtocolError(RuntimeError):
    """Raised when the listener sends an invalid protocol frame."""


class _MissingFastMCP:
    def __init__(self, name: str):
        self.name = name

    def tool(self, func=None, *args, **kwargs):
        if func is None:
            return lambda decorated: decorated
        return func

    def run(self):
        raise RuntimeError(
            "The 'fastmcp' package is not installed. Install host dependencies "
            "from this repository first: py -m pip install -r requirements.txt"
        )


def _parse_int_env(
    name: str,
    default: int,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if min_value is not None and value < min_value:
        raise ConfigError(f"{name} must be >= {min_value}, got {value}")
    if max_value is not None and value > max_value:
        raise ConfigError(f"{name} must be <= {max_value}, got {value}")
    return value


def _parse_float_env(name: str, default: float, min_value: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < min_value:
        raise ConfigError(f"{name} must be >= {min_value}, got {value}")
    return value


def _load_config() -> tuple[str, int, float, int]:
    host = os.environ.get("VW_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    port = _parse_int_env("VW_MCP_PORT", DEFAULT_PORT, 1, 65535)
    timeout = _parse_float_env("VW_MCP_TIMEOUT", DEFAULT_TIMEOUT, 0.1)
    max_frame = _parse_int_env(
        "VW_MCP_MAX_FRAME_BYTES",
        DEFAULT_MAX_FRAME_BYTES,
        1024,
        128 * 1024 * 1024,
    )
    return host, port, timeout, max_frame


_CONFIG_ERROR: Optional[str] = None
try:
    HOST, PORT, TIMEOUT, MAX_FRAME_BYTES = _load_config()
except ConfigError as exc:
    _CONFIG_ERROR = str(exc)
    HOST = DEFAULT_HOST
    PORT = DEFAULT_PORT
    TIMEOUT = DEFAULT_TIMEOUT
    MAX_FRAME_BYTES = DEFAULT_MAX_FRAME_BYTES


mcp = FastMCP("Vectorworks 2024/2025") if FastMCP is not None else _MissingFastMCP("Vectorworks 2024/2025")

# Persistent connection, guarded by a lock so concurrent MCP tool calls do not
# interleave frames on the same socket.
_sock: Optional[socket.socket] = None
_lock = threading.Lock()


ObjectType = Literal["rect", "circle", "oval", "line", "arc", "polygon"]
PropertyName = Literal["name", "class", "fillColor", "penColor", "lineWeight", "opacity"]
ClassAction = Literal["list", "create", "delete"]
WorksheetAction = Literal["list", "read", "write", "read_range"]
SymbolAction = Literal["list", "insert"]
ExportFormat = Literal["pdf", "dxf", "dwg", "image"]
ImportFormat = Literal["auto", "dxf", "dwg", "png", "jpg", "jpeg", "tif", "tiff", "bmp"]
SelectionAction = Literal["get", "select", "clear", "delete", "move", "duplicate"]
PointList = list[list[float]]


def _close():
    global _sock
    if _sock is not None:
        try:
            _sock.close()
        except OSError:
            pass
        _sock = None


atexit.register(_close)


def _connect():
    global _sock
    if _sock is not None:
        return
    sock = socket.create_connection((HOST, PORT), timeout=TIMEOUT)
    sock.settimeout(TIMEOUT)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    _sock = sock


def _recv_exact(n: int) -> bytes:
    if _sock is None:
        raise ConnectionError("not connected")
    buf = bytearray()
    while len(buf) < n:
        chunk = _sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Vectorworks closed the connection")
        buf.extend(chunk)
    return bytes(buf)


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"request is not JSON serializable: {exc}") from exc


def _send_frame(payload: bytes):
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"request frame is {len(payload)} bytes, larger than VW_MCP_MAX_FRAME_BYTES={MAX_FRAME_BYTES}"
        )
    if _sock is None:
        raise ConnectionError("not connected")
    _sock.sendall(struct.pack(">I", len(payload)) + payload)


def _recv_frame() -> bytes:
    header = _recv_exact(4)
    (size,) = struct.unpack(">I", header)
    if size <= 0:
        raise ProtocolError(f"listener sent invalid frame length {size}")
    if size > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"listener frame is {size} bytes, larger than VW_MCP_MAX_FRAME_BYTES={MAX_FRAME_BYTES}"
        )
    return _recv_exact(size)


def _decode_response(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"listener returned non-UTF-8 JSON: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"listener returned malformed JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError(f"listener response must be a JSON object, got {type(value).__name__}")
    return value


def _format_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _connection_help(error: BaseException) -> str:
    return (
        f"Connection error: {error}. Could not reach the Vectorworks MCP listener on {HOST}:{PORT}. "
        "Start Vectorworks, run the generated vw_start_listener_2024.py from Resource Manager "
        "or the installed VW MCP Listener menu command, and verify VW_MCP_HOST/VW_MCP_PORT "
        "match on both sides."
    )


def _request_once(action: str, params: Optional[dict[str, Any]]) -> dict[str, Any]:
    _connect()
    request_id = uuid.uuid4().hex[:8]
    request = {"id": request_id, "action": action, "params": params or {}}
    _send_frame(_json_bytes(request))
    response = _decode_response(_recv_frame())
    response_id = response.get("id")
    if response_id not in (None, "", request_id):
        raise ProtocolError(f"response id mismatch: expected {request_id}, got {response_id!r}")
    return response


def _send(action: str, params: Optional[dict[str, Any]] = None) -> str:
    if _CONFIG_ERROR:
        return f"Configuration error: {_CONFIG_ERROR}"

    with _lock:
        for attempt in (0, 1):
            try:
                response = _request_once(action, params)
                if response.get("success") is True:
                    return _format_result(response.get("result", "OK"))
                return f"VW Error ({action}): {response.get('error', 'Unknown listener error')}"
            except ProtocolError as exc:
                _close()
                return f"Protocol error: {exc}. Restart the Vectorworks listener if this persists."
            except (ConnectionError, TimeoutError, socket.timeout, OSError) as exc:
                _close()
                if attempt == 0:
                    continue
                return _connection_help(exc)
            except Exception as exc:
                _close()
                return f"Unexpected error while talking to Vectorworks: {exc}"

    return "Unexpected error while talking to Vectorworks: request loop exited"


@mcp.tool
def vw_run_script(code: str) -> str:
    """Execute Python inside Vectorworks. The 'vs' module is available.
    Use print() to return output. Escape hatch for anything other tools do not cover.
    Example: vw_run_script("h = vs.FSActLayer()\\nprint(vs.GetName(h))")"""
    return _send("run_script", {"code": code})


@mcp.tool
def vw_create_object(
    object_type: ObjectType,
    x1: float = 0,
    y1: float = 0,
    x2: float = 100,
    y2: float = 100,
    radius: float = 50,
    points: Optional[PointList] = None,
    closed: bool = True,
    start_angle: float = 0,
    sweep_angle: float = 90,
    name: str = "",
    class_name: str = "",
) -> str:
    """Create geometry: rect, circle, oval, line, arc, or polygon.
    x1/y1/x2/y2 are corners or start/end. radius is for circle/arc.
    points is [[x, y], ...] for polygon."""
    return _send(
        "create_object",
        {
            "object_type": object_type,
            "x1": x1,
            "y1": y1,
            "x2": x2,
            "y2": y2,
            "radius": radius,
            "points": points or [],
            "closed": closed,
            "start_angle": start_angle,
            "sweep_angle": sweep_angle,
            "name": name,
            "class_name": class_name,
        },
    )


@mcp.tool
def vw_get_layers() -> str:
    """List all layers with name and visibility."""
    return _send("get_layers")


@mcp.tool
def vw_get_objects(layer: str = "", object_type: str = "", limit: int = 100) -> str:
    """List objects. Filter by layer name and type such as rect, line, or wall."""
    return _send("get_objects", {"layer": layer, "object_type": object_type, "limit": limit})


@mcp.tool
def vw_set_object_property(handle: str, property_name: PropertyName, value: str) -> str:
    """Set an object property. Colors use 'r,g,b' values in Vectorworks 0-65535 color range."""
    return _send("set_property", {"handle": handle, "property_name": property_name, "value": value})


@mcp.tool
def vw_find_objects(criteria: str, limit: int = 100) -> str:
    """Find objects using VW criteria such as 'T=RECT', 'T=WALL', 'C=Furniture', or 'ALL'."""
    return _send("find_objects", {"criteria": criteria, "limit": limit})


@mcp.tool
def vw_manage_classes(action: ClassAction, class_name: str = "") -> str:
    """List, create, or delete classes. class_name is ignored for list."""
    return _send("manage_classes", {"action": action, "class_name": class_name})


@mcp.tool
def vw_worksheet(
    action: WorksheetAction,
    worksheet_name: str = "",
    row: int = 1,
    col: int = 1,
    value: str = "",
    num_rows: int = 10,
) -> str:
    """Worksheet operations: list, read, write, or read_range."""
    return _send(
        "worksheet",
        {
            "action": action,
            "worksheet_name": worksheet_name,
            "row": row,
            "col": col,
            "value": value,
            "num_rows": num_rows,
        },
    )


@mcp.tool
def vw_symbol(action: SymbolAction, symbol_name: str = "", x: float = 0, y: float = 0, rotation: float = 0) -> str:
    """List symbols or insert a symbol at x/y with rotation."""
    return _send("symbol", {"action": action, "symbol_name": symbol_name, "x": x, "y": y, "rotation": rotation})


@mcp.tool
def vw_export(format: ExportFormat, file_path: str) -> str:
    """Export document. format is pdf, dxf, dwg, or image. file_path is the full output path."""
    return _send("export", {"format": format, "file_path": file_path})


@mcp.tool
def vw_import_file(file_path: str, format: ImportFormat = "auto") -> str:
    """Import a DXF, DWG, or image file. Use auto to detect from the extension."""
    return _send("import_file", {"file_path": file_path, "format": format})


@mcp.tool
def vw_get_document_info() -> str:
    """Get document metadata: filename, filepath, layer count, object count, and layer names."""
    return _send("get_document_info")


@mcp.tool
def vw_screenshot(file_path: str = "") -> str:
    """Capture viewport screenshot as PNG. Empty file_path defaults to ~/.vectorworks-mcp/screenshot.png."""
    return _send("screenshot", {"file_path": file_path})


@mcp.tool
def vw_ping() -> str:
    """Health check. Returns listener version, handler count, and CAD safety status if connected."""
    return _send("ping")


@mcp.tool
def vw_bridge_status() -> str:
    """Return bridge status from the listener, including whether real CAD/API handlers are safe."""
    return _send("ping")


@mcp.tool
def vw_stop_listener() -> str:
    """Ask the Vectorworks listener to stop gracefully after replying."""
    return _send("stop")


@mcp.tool
def vw_selection(action: SelectionAction, criteria: str = "") -> str:
    """Selection ops. For select, criteria is a VW criteria string. For move, criteria is 'dx,dy'."""
    return _send("selection", {"action": action, "criteria": criteria})


@mcp.tool
def vw_create_wall(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    height: float = 3000,
    thickness: float = 200,
    style_name: str = "",
) -> str:
    """Create parametric wall. Coordinates are in mm. Defaults to 3m height and 200mm thickness."""
    return _send(
        "create_wall",
        {
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
            "height": height,
            "thickness": thickness,
            "style_name": style_name,
        },
    )


@mcp.tool
def vw_insert_door(x: float, y: float, width: float = 900, height: float = 2100, rotation: float = 0) -> str:
    """Insert parametric door. Place on or near a wall for auto-insertion."""
    return _send("insert_door", {"x": x, "y": y, "width": width, "height": height, "rotation": rotation})


@mcp.tool
def vw_insert_window(
    x: float,
    y: float,
    width: float = 1200,
    height: float = 1500,
    sill_height: float = 900,
    rotation: float = 0,
) -> str:
    """Insert parametric window. sill_height is floor to window bottom in mm."""
    return _send(
        "insert_window",
        {"x": x, "y": y, "width": width, "height": height, "sill_height": sill_height, "rotation": rotation},
    )


@mcp.tool
def vw_create_slab(points: PointList, thickness: float = 200, elevation: float = 0) -> str:
    """Create 3D floor slab from polygon. points is [[x, y], ...] in mm and needs at least 3 points."""
    return _send("create_slab", {"points": points, "thickness": thickness, "elevation": elevation})


@mcp.tool
def vw_create_roof(
    points: PointList,
    bearing_height: float = 3000,
    slope: float = 30,
    overhang: float = 500,
    thickness: float = 200,
) -> str:
    """Create roof from footprint. bearing_height is where roof starts. slope is in degrees."""
    return _send(
        "create_roof",
        {
            "points": points,
            "bearing_height": bearing_height,
            "slope": slope,
            "overhang": overhang,
            "thickness": thickness,
        },
    )


@mcp.tool
def vw_inspect_object(handle: str = "", plugin_name: str = "") -> str:
    """Discover configurable parameters of a VW object. Provide handle or plugin_name such as Door or Wall."""
    return _send("inspect_object", {"handle": handle, "plugin_name": plugin_name})


def main() -> int:
    if _CONFIG_ERROR:
        print(f"Vectorworks MCP configuration error: {_CONFIG_ERROR}", file=sys.stderr)
        return 2
    try:
        mcp.run()
        return 0
    except RuntimeError as exc:
        print(f"Vectorworks MCP startup error: {exc}", file=sys.stderr)
        return 1
    finally:
        _close()


if __name__ == "__main__":
    raise SystemExit(main())
