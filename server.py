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
  VW_MCP_PREFLIGHT_CACHE_MS
                          safe-CAD preflight success cache in ms, default 750
"""

import atexit
import json
import os
import socket
import struct
import sys
import threading
import time
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
DEFAULT_PREFLIGHT_CACHE_MS = 750
MAX_PREFLIGHT_CACHE_MS = 5_000


class ConfigError(ValueError):
    """Raised when environment configuration cannot be used safely."""


class ProtocolError(RuntimeError):
    """Raised when the listener sends an invalid protocol frame."""


class RequestTransportError(ConnectionError):
    """Raised after a request frame may have reached the listener."""

    def __init__(self, action: str, stage: str, original: BaseException):
        self.action = action
        self.stage = stage
        self.original = original
        super().__init__(str(original))


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


def _load_config() -> tuple[str, int, float, int, int]:
    host = os.environ.get("VW_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    port = _parse_int_env("VW_MCP_PORT", DEFAULT_PORT, 1, 65535)
    timeout = _parse_float_env("VW_MCP_TIMEOUT", DEFAULT_TIMEOUT, 0.1)
    max_frame = _parse_int_env(
        "VW_MCP_MAX_FRAME_BYTES",
        DEFAULT_MAX_FRAME_BYTES,
        1024,
        128 * 1024 * 1024,
    )
    preflight_cache_ms = _parse_int_env(
        "VW_MCP_PREFLIGHT_CACHE_MS",
        DEFAULT_PREFLIGHT_CACHE_MS,
        0,
        MAX_PREFLIGHT_CACHE_MS,
    )
    return host, port, timeout, max_frame, preflight_cache_ms


_CONFIG_ERROR: Optional[str] = None
try:
    HOST, PORT, TIMEOUT, MAX_FRAME_BYTES, PREFLIGHT_CACHE_MS = _load_config()
except ConfigError as exc:
    _CONFIG_ERROR = str(exc)
    HOST = DEFAULT_HOST
    PORT = DEFAULT_PORT
    TIMEOUT = DEFAULT_TIMEOUT
    MAX_FRAME_BYTES = DEFAULT_MAX_FRAME_BYTES
    PREFLIGHT_CACHE_MS = DEFAULT_PREFLIGHT_CACHE_MS

PREFLIGHT_CACHE_SECONDS = PREFLIGHT_CACHE_MS / 1000.0


mcp = FastMCP("Vectorworks 2024/2025") if FastMCP is not None else _MissingFastMCP("Vectorworks 2024/2025")

# Persistent connection, guarded by a lock so concurrent MCP tool calls do not
# interleave frames on the same socket.
_sock: Optional[socket.socket] = None
_lock = threading.Lock()
_cad_safe_cache: Optional[tuple[float, dict[str, Any]]] = None


ObjectType = Literal["rect", "circle", "oval", "line", "arc", "polygon"]
PropertyName = Literal["name", "class", "fillColor", "penColor", "lineWeight", "opacity"]
ClassAction = Literal["list", "create", "delete"]
WorksheetAction = Literal["list", "read", "write", "read_range"]
SymbolAction = Literal["list", "insert"]
ExportFormat = Literal["pdf", "dxf", "dwg", "image"]
ImportFormat = Literal["auto", "dxf", "dwg", "png", "jpg", "jpeg", "tif", "tiff", "bmp"]
SelectionAction = Literal["get", "select", "clear", "delete", "move", "duplicate"]
PointList = list[list[float]]


_ANNOTATION_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

TOOL_SAFETY: dict[str, dict[str, Any]] = {
    "vw_tool_safety": {
        "category": "metadata",
        "wire_action": None,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "requires_cad_preflight": False,
    },
    "vw_ping": {
        "category": "health",
        "wire_action": "ping",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": False,
    },
    "vw_bridge_status": {
        "category": "health",
        "wire_action": "ping",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": False,
    },
    "vw_preflight_for_cad": {
        "category": "health",
        "wire_action": "ping",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": False,
    },
    "vw_get_document_info": {
        "category": "document-read",
        "wire_action": "get_document_info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_get_layers": {
        "category": "document-read",
        "wire_action": "get_layers",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_get_objects": {
        "category": "document-read",
        "wire_action": "get_objects",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_find_objects": {
        "category": "document-read",
        "wire_action": "find_objects",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_inspect_object": {
        "category": "document-read",
        "wire_action": "inspect_object",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_screenshot": {
        "category": "document-export",
        "wire_action": "screenshot",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_stop_listener": {
        "category": "listener-control",
        "wire_action": "stop",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": False,
    },
    "vw_create_object": {
        "category": "document-write",
        "wire_action": "create_object",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_set_object_property": {
        "category": "document-write",
        "wire_action": "set_property",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_worksheet": {
        "category": "mixed-document-write",
        "wire_action": "worksheet",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_symbol": {
        "category": "mixed-document-write",
        "wire_action": "symbol",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_export": {
        "category": "file-write",
        "wire_action": "export",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_import_file": {
        "category": "document-write",
        "wire_action": "import_file",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_create_wall": {
        "category": "document-write",
        "wire_action": "create_wall",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_insert_door": {
        "category": "document-write",
        "wire_action": "insert_door",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_insert_window": {
        "category": "document-write",
        "wire_action": "insert_window",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_create_slab": {
        "category": "document-write",
        "wire_action": "create_slab",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_create_roof": {
        "category": "document-write",
        "wire_action": "create_roof",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_manage_classes": {
        "category": "mixed-destructive",
        "wire_action": "manage_classes",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_selection": {
        "category": "mixed-destructive",
        "wire_action": "selection",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_run_script": {
        "category": "trusted-code",
        "wire_action": "run_script",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
}


_ACTION_SAFETY = {
    safety["wire_action"]: safety
    for safety in TOOL_SAFETY.values()
    if isinstance(safety.get("wire_action"), str) and safety.get("wire_action")
}


def _annotations_for(tool_name: str) -> dict[str, bool]:
    safety = TOOL_SAFETY[tool_name]
    return {key: bool(safety[key]) for key in _ANNOTATION_KEYS}


def _tool(tool_name: str):
    return mcp.tool(annotations=_annotations_for(tool_name))


def _clear_cad_safe_cache():
    global _cad_safe_cache
    _cad_safe_cache = None


def _close():
    global _sock
    _clear_cad_safe_cache()
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


def _action_safe_to_retry(action: str) -> bool:
    safety = _ACTION_SAFETY.get(action)
    if not safety:
        return False
    return (
        bool(safety.get("readOnlyHint"))
        and bool(safety.get("idempotentHint"))
        and not bool(safety.get("destructiveHint"))
    )


def _unknown_commit_state_help(action: str, error: RequestTransportError) -> str:
    return (
        "Unknown commit state after sending non-idempotent Vectorworks action "
        "'{action}': {err}\n\n"
        "The request may or may not have completed inside Vectorworks. The MCP "
        "host did not retry it, because retrying could duplicate or compound CAD "
        "changes. Check the Vectorworks document state, then rerun only the exact "
        "follow-up action you still need."
    ).format(action=action, err=error.original)


def _with_block_context(payload: dict[str, Any], blocked_action: Optional[str]) -> dict[str, Any]:
    if blocked_action:
        payload = dict(payload)
        payload["blocked"] = True
        payload["blocked_action"] = blocked_action
    return payload


def _cad_preflight_ping_error_payload(raw_status: Any, blocked_action: Optional[str] = None) -> dict[str, Any]:
    return _with_block_context(
        {
            "ok": False,
            "cad_api_safe": False,
            "reason": "preflight_ping_error",
            "next_action": "Fix listener connectivity before CAD work.",
            "raw_status": raw_status,
        },
        blocked_action,
    )


def _evaluate_cad_preflight_status(status: Any, blocked_action: Optional[str] = None) -> dict[str, Any]:
    if not isinstance(status, dict):
        return _with_block_context(
            {
                "ok": False,
                "cad_api_safe": False,
                "reason": "preflight_ping_non_object",
                "next_action": "Update/regenerate the Vectorworks listener before real CAD work.",
                "raw_status": status,
            },
            blocked_action,
        )

    if status.get("cad_api_safe") is True and status.get("transport_only") is not True:
        return {
            "ok": True,
            "cad_api_safe": True,
            "bridge_kind": status.get("bridge_kind", "unknown"),
            "dispatch_mode": status.get("dispatch_mode", "unknown"),
            "transport_only": bool(status.get("transport_only")),
            "native_bridge": bool(status.get("native_bridge")),
            "handlers": status.get("handlers"),
            "version": status.get("version"),
            "reason": "cad_api_safe",
            "next_action": "Call vw_get_document_info before non-trivial CAD work.",
            "raw_status": status,
        }

    if status.get("transport_only") is True:
        reason = "transport_only_bridge"
        next_action = "Do not call CAD handlers. Regenerate/run the dialog launcher or use a compiled native SDK bridge."
    elif "cad_api_safe" not in status:
        reason = "legacy_status_without_cad_api_safe"
        next_action = "Update/regenerate the Vectorworks listener before real CAD work."
    else:
        reason = "listener_reports_cad_api_unsafe"
        next_action = "Do not call CAD handlers until the dialog launcher or native SDK bridge is active."

    return _with_block_context(
        {
            "ok": False,
            "cad_api_safe": False,
            "bridge_kind": status.get("bridge_kind", "unknown"),
            "dispatch_mode": status.get("dispatch_mode", "unknown"),
            "transport_only": bool(status.get("transport_only")),
            "native_bridge": bool(status.get("native_bridge")),
            "reason": reason,
            "next_action": next_action,
            "raw_status": status,
        },
        blocked_action,
    )


def _remember_cad_safe_status(status: dict[str, Any]):
    global _cad_safe_cache
    if PREFLIGHT_CACHE_SECONDS <= 0:
        return
    _cad_safe_cache = (time.monotonic(), dict(status))


def _cached_cad_safe_status() -> Optional[dict[str, Any]]:
    if PREFLIGHT_CACHE_SECONDS <= 0 or _cad_safe_cache is None:
        return None
    timestamp, status = _cad_safe_cache
    if time.monotonic() - timestamp <= PREFLIGHT_CACHE_SECONDS:
        return dict(status)
    _clear_cad_safe_cache()
    return None


def _cad_preflight_block(action: str) -> Optional[str]:
    if _cached_cad_safe_status() is not None:
        return None

    response = _request_once("ping", None)
    if response.get("success") is not True:
        payload = _cad_preflight_ping_error_payload(response, blocked_action=action)
        return json.dumps(payload, indent=2, sort_keys=True)

    status = response.get("result")
    payload = _evaluate_cad_preflight_status(status, blocked_action=action)
    if payload["ok"] and isinstance(status, dict):
        _remember_cad_safe_status(status)
        return None
    return json.dumps(payload, indent=2, sort_keys=True)


def _request_once(action: str, params: Optional[dict[str, Any]]) -> dict[str, Any]:
    _connect()
    request_id = uuid.uuid4().hex[:8]
    request = {"id": request_id, "action": action, "params": params or {}}
    try:
        _send_frame(_json_bytes(request))
        response = _decode_response(_recv_frame())
    except (ConnectionError, TimeoutError, socket.timeout, OSError) as exc:
        raise RequestTransportError(action, "response", exc) from exc
    response_id = response.get("id")
    if response_id not in (None, "", request_id):
        raise ProtocolError(f"response id mismatch: expected {request_id}, got {response_id!r}")
    return response


def _send(action: str, params: Optional[dict[str, Any]] = None, require_cad_safe: bool = False) -> str:
    if _CONFIG_ERROR:
        return f"Configuration error: {_CONFIG_ERROR}"

    with _lock:
        for attempt in (0, 1):
            try:
                if require_cad_safe:
                    blocked = _cad_preflight_block(action)
                    if blocked:
                        return blocked
                response = _request_once(action, params)
                if response.get("success") is True:
                    return _format_result(response.get("result", "OK"))
                return f"VW Error ({action}): {response.get('error', 'Unknown listener error')}"
            except ProtocolError as exc:
                _close()
                return f"Protocol error: {exc}. Restart the Vectorworks listener if this persists."
            except RequestTransportError as exc:
                _close()
                if exc.action != action:
                    if attempt == 0:
                        continue
                    return _connection_help(exc.original)
                if attempt == 0 and _action_safe_to_retry(action):
                    continue
                if not _action_safe_to_retry(action):
                    return _unknown_commit_state_help(action, exc)
                return _connection_help(exc.original)
            except (ConnectionError, TimeoutError, socket.timeout, OSError) as exc:
                _close()
                if attempt == 0:
                    continue
                return _connection_help(exc)
            except Exception as exc:
                _close()
                return f"Unexpected error while talking to Vectorworks: {exc}"

    return "Unexpected error while talking to Vectorworks: request loop exited"


def _send_tool(tool_name: str, params: Optional[dict[str, Any]] = None) -> str:
    safety = TOOL_SAFETY[tool_name]
    action = safety.get("wire_action")
    if not isinstance(action, str) or not action:
        return f"Configuration error: {tool_name} does not declare a wire_action"
    return _send(action, params, require_cad_safe=bool(safety["requires_cad_preflight"]))


@_tool("vw_tool_safety")
def vw_tool_safety() -> str:
    """Return structured safety metadata for every Vectorworks MCP tool."""
    return json.dumps(TOOL_SAFETY, indent=2, sort_keys=True)


@_tool("vw_run_script")
def vw_run_script(code: str) -> str:
    """Execute Python inside Vectorworks. The 'vs' module is available.
    Use print() to return output. Escape hatch for anything other tools do not cover.
    Example: vw_run_script("h = vs.FSActLayer()\\nprint(vs.GetName(h))")"""
    return _send_tool("vw_run_script", {"code": code})


@_tool("vw_create_object")
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
    return _send_tool(
        "vw_create_object",
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


@_tool("vw_get_layers")
def vw_get_layers() -> str:
    """List all layers with name and visibility."""
    return _send_tool("vw_get_layers")


@_tool("vw_get_objects")
def vw_get_objects(layer: str = "", object_type: str = "", limit: int = 100) -> str:
    """List objects. Filter by layer name and type such as rect, line, or wall."""
    return _send_tool("vw_get_objects", {"layer": layer, "object_type": object_type, "limit": limit})


@_tool("vw_set_object_property")
def vw_set_object_property(handle: str, property_name: PropertyName, value: str) -> str:
    """Set an object property. Colors use 'r,g,b' values in Vectorworks 0-65535 color range."""
    return _send_tool("vw_set_object_property", {"handle": handle, "property_name": property_name, "value": value})


@_tool("vw_find_objects")
def vw_find_objects(criteria: str, limit: int = 100) -> str:
    """Find objects using VW criteria such as 'T=RECT', 'T=WALL', 'C=Furniture', or 'ALL'."""
    return _send_tool("vw_find_objects", {"criteria": criteria, "limit": limit})


@_tool("vw_manage_classes")
def vw_manage_classes(action: ClassAction, class_name: str = "") -> str:
    """List, create, or delete classes. class_name is ignored for list."""
    return _send_tool("vw_manage_classes", {"action": action, "class_name": class_name})


@_tool("vw_worksheet")
def vw_worksheet(
    action: WorksheetAction,
    worksheet_name: str = "",
    row: int = 1,
    col: int = 1,
    value: str = "",
    num_rows: int = 10,
) -> str:
    """Worksheet operations: list, read, write, or read_range."""
    return _send_tool(
        "vw_worksheet",
        {
            "action": action,
            "worksheet_name": worksheet_name,
            "row": row,
            "col": col,
            "value": value,
            "num_rows": num_rows,
        },
    )


@_tool("vw_symbol")
def vw_symbol(action: SymbolAction, symbol_name: str = "", x: float = 0, y: float = 0, rotation: float = 0) -> str:
    """List symbols or insert a symbol at x/y with rotation."""
    return _send_tool("vw_symbol", {"action": action, "symbol_name": symbol_name, "x": x, "y": y, "rotation": rotation})


@_tool("vw_export")
def vw_export(format: ExportFormat, file_path: str) -> str:
    """Export document. format is pdf, dxf, dwg, or image. file_path is the full output path."""
    return _send_tool("vw_export", {"format": format, "file_path": file_path})


@_tool("vw_import_file")
def vw_import_file(file_path: str, format: ImportFormat = "auto") -> str:
    """Import a DXF, DWG, or image file. Use auto to detect from the extension."""
    return _send_tool("vw_import_file", {"file_path": file_path, "format": format})


@_tool("vw_get_document_info")
def vw_get_document_info() -> str:
    """Get document metadata: filename, filepath, layer count, object count, and layer names."""
    return _send_tool("vw_get_document_info")


@_tool("vw_screenshot")
def vw_screenshot(file_path: str = "") -> str:
    """Capture viewport screenshot as PNG. Empty file_path defaults to ~/.vectorworks-mcp/screenshot.png."""
    return _send_tool("vw_screenshot", {"file_path": file_path})


@_tool("vw_ping")
def vw_ping() -> str:
    """Health check. Returns listener version, handler count, and CAD safety status if connected."""
    return _send_tool("vw_ping")


@_tool("vw_bridge_status")
def vw_bridge_status() -> str:
    """Return bridge status from the listener, including whether real CAD/API handlers are safe."""
    return _send_tool("vw_bridge_status")


@_tool("vw_preflight_for_cad")
def vw_preflight_for_cad() -> str:
    """Return structured go/no-go status before real CAD/API handlers."""
    raw = _send("ping")
    try:
        status = json.loads(raw)
    except json.JSONDecodeError:
        payload = _cad_preflight_ping_error_payload(raw)
        payload["reason"] = "ping_failed_or_non_json"
        return json.dumps(payload, sort_keys=True)

    payload = _evaluate_cad_preflight_status(status)
    if payload["ok"] and isinstance(status, dict):
        _remember_cad_safe_status(status)
    return json.dumps(payload, sort_keys=True)


@_tool("vw_stop_listener")
def vw_stop_listener() -> str:
    """Ask the Vectorworks listener to stop gracefully after replying."""
    return _send_tool("vw_stop_listener")


@_tool("vw_selection")
def vw_selection(action: SelectionAction, criteria: str = "") -> str:
    """Selection ops. For select, criteria is a VW criteria string. For move, criteria is 'dx,dy'."""
    return _send_tool("vw_selection", {"action": action, "criteria": criteria})


@_tool("vw_create_wall")
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
    return _send_tool(
        "vw_create_wall",
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


@_tool("vw_insert_door")
def vw_insert_door(x: float, y: float, width: float = 900, height: float = 2100, rotation: float = 0) -> str:
    """Insert parametric door. Place on or near a wall for auto-insertion."""
    return _send_tool("vw_insert_door", {"x": x, "y": y, "width": width, "height": height, "rotation": rotation})


@_tool("vw_insert_window")
def vw_insert_window(
    x: float,
    y: float,
    width: float = 1200,
    height: float = 1500,
    sill_height: float = 900,
    rotation: float = 0,
) -> str:
    """Insert parametric window. sill_height is floor to window bottom in mm."""
    return _send_tool(
        "vw_insert_window",
        {"x": x, "y": y, "width": width, "height": height, "sill_height": sill_height, "rotation": rotation},
    )


@_tool("vw_create_slab")
def vw_create_slab(points: PointList, thickness: float = 200, elevation: float = 0) -> str:
    """Create 3D floor slab from polygon. points is [[x, y], ...] in mm and needs at least 3 points."""
    return _send_tool("vw_create_slab", {"points": points, "thickness": thickness, "elevation": elevation})


@_tool("vw_create_roof")
def vw_create_roof(
    points: PointList,
    bearing_height: float = 3000,
    slope: float = 30,
    overhang: float = 500,
    thickness: float = 200,
) -> str:
    """Create roof from footprint. bearing_height is where roof starts. slope is in degrees."""
    return _send_tool(
        "vw_create_roof",
        {
            "points": points,
            "bearing_height": bearing_height,
            "slope": slope,
            "overhang": overhang,
            "thickness": thickness,
        },
    )


@_tool("vw_inspect_object")
def vw_inspect_object(handle: str = "", plugin_name: str = "") -> str:
    """Discover configurable parameters of a VW object. Provide handle or plugin_name such as Door or Wall."""
    return _send_tool("vw_inspect_object", {"handle": handle, "plugin_name": plugin_name})


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
