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
  VW_MCP_HEALTH_TIMEOUT   ping/preflight timeout in seconds, default min(2, VW_MCP_TIMEOUT)
  VW_MCP_MAX_FRAME_BYTES  max protocol frame size, default 16777216
  VW_MCP_PREFLIGHT_CACHE_MS
                          safe-CAD preflight success cache in ms, default 750
"""

import atexit
import json
import math
import os
import socket
import struct
import sys
import threading
import time
import uuid
from typing import Annotated, Any, Literal, Optional

try:
    from pydantic import Field
except Exception:
    def Field(*_args: Any, **_kwargs: Any) -> None:
        return None

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
DEFAULT_HEALTH_TIMEOUT = 2.0
DEFAULT_MAX_FRAME_BYTES = 16 * 1024 * 1024
DEFAULT_PREFLIGHT_CACHE_MS = 750
MAX_PREFLIGHT_CACHE_MS = 5_000
NATIVE_PHASE_ONE_REQUIRED_ACTIONS = {
    "ping",
    "stop",
    "get_document_info",
    "get_layers",
    "get_objects",
    "selection",
    "create_object",
}
NATIVE_PHASE_ONE_CREATE_OBJECT_TYPES = {
    "arc",
    "box",
    "circle",
    "line",
    "oval",
    "rect",
    "rectangle",
}
NATIVE_PHASE_ONE_SELECTION_ACTIONS = {
    "clear",
    "delete",
    "get",
    "select",
}


class ConfigError(ValueError):
    """Raised when environment configuration cannot be used safely."""


class ProtocolError(RuntimeError):
    """Raised when the listener sends an invalid protocol frame."""


class RequestNotSentError(ProtocolError):
    """Raised when a request cannot be encoded/framed before any bytes are sent."""

    def __init__(self, action: str, original: BaseException):
        self.action = action
        self.original = original
        super().__init__(str(original))


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

    def run(self, *args, **kwargs):
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


def _load_config() -> tuple[str, int, float, float, int, int]:
    host = os.environ.get("VW_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    port = _parse_int_env("VW_MCP_PORT", DEFAULT_PORT, 1, 65535)
    timeout = _parse_float_env("VW_MCP_TIMEOUT", DEFAULT_TIMEOUT, 0.1)
    health_timeout = _parse_float_env("VW_MCP_HEALTH_TIMEOUT", min(DEFAULT_HEALTH_TIMEOUT, timeout), 0.1)
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
    return host, port, timeout, health_timeout, max_frame, preflight_cache_ms


_CONFIG_ERROR: Optional[str] = None
try:
    HOST, PORT, TIMEOUT, HEALTH_TIMEOUT, MAX_FRAME_BYTES, PREFLIGHT_CACHE_MS = _load_config()
except ConfigError as exc:
    _CONFIG_ERROR = str(exc)
    HOST = DEFAULT_HOST
    PORT = DEFAULT_PORT
    TIMEOUT = DEFAULT_TIMEOUT
    HEALTH_TIMEOUT = DEFAULT_HEALTH_TIMEOUT
    MAX_FRAME_BYTES = DEFAULT_MAX_FRAME_BYTES
    PREFLIGHT_CACHE_MS = DEFAULT_PREFLIGHT_CACHE_MS

PREFLIGHT_CACHE_SECONDS = PREFLIGHT_CACHE_MS / 1000.0


mcp = FastMCP("Vectorworks 2024/2025") if FastMCP is not None else _MissingFastMCP("Vectorworks 2024/2025")

# Persistent connection, guarded by a lock so concurrent MCP tool calls do not
# interleave frames on the same socket.
_sock: Optional[socket.socket] = None
_lock = threading.Lock()
_cad_safe_cache_lock = threading.Lock()
_cad_safe_cache: Optional[tuple[float, dict[str, Any]]] = None


ObjectType = Literal["rect", "circle", "oval", "line", "arc", "polygon"]
DoorSwing = Literal["left", "right"]
PropertyName = Literal["name", "class", "fillColor", "penColor", "lineWeight", "opacity"]
ClassAction = Literal["list", "create", "delete"]
WorksheetAction = Literal["list", "read", "write", "read_range"]
SymbolAction = Literal["list", "insert"]
ExportFormat = Literal["pdf", "dxf", "dwg", "image"]
ImportFormat = Literal["auto", "dxf", "dwg", "png", "jpg", "jpeg", "tif", "tiff", "bmp"]
SelectionAction = Literal["get", "select", "clear", "delete", "move", "duplicate"]
MAX_OBJECT_QUERY_LIMIT = 1000
ObjectQueryLimit = Annotated[int, Field(ge=1, le=MAX_OBJECT_QUERY_LIMIT)]
WorksheetRow = Annotated[int, Field(ge=1, le=1_048_576)]
WorksheetColumn = Annotated[int, Field(ge=1, le=16_384)]
WorksheetRowCount = Annotated[int, Field(ge=1, le=500)]
NonEmptyPath = Annotated[str, Field(min_length=1)]
PositiveLength = Annotated[float, Field(gt=0)]
Point2D = Annotated[list[float], Field(min_length=2, max_length=2)]
PointList = Annotated[list[Point2D], Field(max_length=1000)]
PolygonPointList = Annotated[list[Point2D], Field(min_length=3, max_length=1000)]
PrimitiveObjectList = Annotated[list[dict[str, Any]], Field(min_length=1, max_length=250)]
FloorPlanRoomList = Annotated[list[dict[str, Any]], Field(min_length=1, max_length=100)]
FloorPlanItemList = Annotated[list[dict[str, Any]], Field(max_length=250)]


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
    "vw_capabilities": {
        "category": "metadata",
        "wire_action": "ping",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
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
    "vw_drawing_summary": {
        "category": "document-read",
        "wire_action": None,
        "composes_actions": ["get_document_info", "get_layers", "get_objects"],
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
    "vw_batch_create_objects": {
        "category": "document-write",
        "wire_action": None,
        "composes_actions": ["create_object"],
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_plan_schematic_floor_plan": {
        "category": "schematic-floor-plan",
        "wire_action": None,
        "composes_actions": [],
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "requires_cad_preflight": False,
    },
    "vw_create_schematic_floor_plan": {
        "category": "schematic-floor-plan",
        "wire_action": None,
        "composes_actions": ["create_object"],
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_create_schematic_room": {
        "category": "schematic-floor-plan",
        "wire_action": None,
        "composes_actions": ["create_object"],
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_create_schematic_door": {
        "category": "schematic-floor-plan",
        "wire_action": None,
        "composes_actions": ["create_object"],
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_create_schematic_window": {
        "category": "schematic-floor-plan",
        "wire_action": None,
        "composes_actions": ["create_object"],
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
        "action_param": "action",
        "actions": {
            "list": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "writesDocument": False,
                "writesFiles": False,
                "confirmationRequired": False,
            },
            "read": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "writesDocument": False,
                "writesFiles": False,
                "confirmationRequired": False,
            },
            "read_range": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "writesDocument": False,
                "writesFiles": False,
                "confirmationRequired": False,
            },
            "write": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "writesDocument": True,
                "writesFiles": False,
                "confirmationRequired": False,
            },
        },
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_symbol": {
        "category": "mixed-document-write",
        "wire_action": "symbol",
        "action_param": "action",
        "actions": {
            "list": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "writesDocument": False,
                "writesFiles": False,
                "confirmationRequired": False,
            },
            "insert": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "writesDocument": True,
                "writesFiles": False,
                "confirmationRequired": False,
            },
        },
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
        "action_param": "action",
        "actions": {
            "list": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "writesDocument": False,
                "writesFiles": False,
                "confirmationRequired": False,
            },
            "create": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "writesDocument": True,
                "writesFiles": False,
                "confirmationRequired": False,
            },
            "delete": {
                "readOnlyHint": False,
                "destructiveHint": True,
                "idempotentHint": False,
                "writesDocument": True,
                "writesFiles": False,
                "confirmationRequired": True,
            },
        },
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_selection": {
        "category": "mixed-destructive",
        "wire_action": "selection",
        "action_param": "action",
        "actions": {
            "get": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "writesDocument": False,
                "writesSelection": False,
                "writesFiles": False,
                "confirmationRequired": False,
            },
            "select": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "writesDocument": False,
                "writesSelection": True,
                "writesFiles": False,
                "confirmationRequired": False,
            },
            "clear": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "writesDocument": False,
                "writesSelection": True,
                "writesFiles": False,
                "confirmationRequired": False,
            },
            "delete": {
                "readOnlyHint": False,
                "destructiveHint": True,
                "idempotentHint": False,
                "writesDocument": True,
                "writesSelection": True,
                "writesFiles": False,
                "confirmationRequired": True,
            },
            "move": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "writesDocument": True,
                "writesSelection": False,
                "writesFiles": False,
                "confirmationRequired": False,
            },
            "duplicate": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "writesDocument": True,
                "writesSelection": False,
                "writesFiles": False,
                "confirmationRequired": False,
            },
        },
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_run_script": {
        "category": "trusted-code",
        "wire_action": "run_script",
        "executesCode": True,
        "confirmationRequired": True,
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
}


_ACTION_SAFETY: dict[str, dict[str, Any]] = {}
for _tool_name, _safety in TOOL_SAFETY.items():
    _wire_action = _safety.get("wire_action")
    if isinstance(_wire_action, str) and _wire_action:
        _ACTION_SAFETY.setdefault(_wire_action, _safety)


def _operation_safety(action: str, params: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    safety = _ACTION_SAFETY.get(action)
    if not safety:
        return None
    action_param = safety.get("action_param")
    variants = safety.get("actions")
    if isinstance(action_param, str) and isinstance(variants, dict):
        variant_name = ""
        if isinstance(params, dict):
            variant_name = str(params.get(action_param, "") or "")
        variant = variants.get(variant_name)
        if isinstance(variant, dict):
            merged = dict(safety)
            merged.update(variant)
            merged["variant"] = variant_name
            return merged
    return safety


def _annotations_for(tool_name: str) -> dict[str, bool]:
    safety = TOOL_SAFETY[tool_name]
    return {key: bool(safety[key]) for key in _ANNOTATION_KEYS}


def _tool(tool_name: str):
    return mcp.tool(annotations=_annotations_for(tool_name))


def _clear_cad_safe_cache():
    global _cad_safe_cache
    with _cad_safe_cache_lock:
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


def _recv_exact_from(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Vectorworks closed the connection")
        buf.extend(chunk)
    return bytes(buf)


def _recv_exact(n: int) -> bytes:
    if _sock is None:
        raise ConnectionError("not connected")
    return _recv_exact_from(_sock, n)


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"request is not JSON serializable: {exc}") from exc


def _send_frame_to(sock: socket.socket, payload: bytes):
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"request frame is {len(payload)} bytes, larger than VW_MCP_MAX_FRAME_BYTES={MAX_FRAME_BYTES}"
        )
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def _send_frame(payload: bytes):
    if _sock is None:
        raise ConnectionError("not connected")
    _send_frame_to(_sock, payload)


def _recv_frame_from(sock: socket.socket) -> bytes:
    header = _recv_exact_from(sock, 4)
    (size,) = struct.unpack(">I", header)
    if size <= 0:
        raise ProtocolError(f"listener sent invalid frame length {size}")
    if size > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"listener frame is {size} bytes, larger than VW_MCP_MAX_FRAME_BYTES={MAX_FRAME_BYTES}"
        )
    return _recv_exact_from(sock, size)


def _recv_frame() -> bytes:
    if _sock is None:
        raise ConnectionError("not connected")
    return _recv_frame_from(_sock)


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


def _validate_response_envelope(response: dict[str, Any], request_id: str, action: str) -> None:
    response_id = response.get("id")
    if response_id != request_id:
        raise ProtocolError(f"response id mismatch for {action}: expected {request_id}, got {response_id!r}")

    success = response.get("success")
    if success is True:
        if "result" not in response:
            raise ProtocolError(f"listener success response for {action} did not include result")
        return
    if success is False:
        error = response.get("error")
        if not isinstance(error, str) or not error.strip():
            raise ProtocolError(f"listener failure response for {action} did not include a non-empty error string")
        return
    raise ProtocolError(f"listener response success for {action} was not boolean true/false")


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
        "Start Vectorworks, run the generated vw_load_listener_2024.py from Resource Manager "
        "or the installed VW MCP Listener menu command, and verify VW_MCP_HOST/VW_MCP_PORT "
        "match on both sides. If the port is open but requests time out, run "
        "scripts\\test-vectorworks-listener.ps1 or scripts\\doctor-vectorworks-mcp.ps1, create "
        "C:\\Users\\<you>\\.vectorworks-mcp\\STOP, and restart Vectorworks if the stale listener "
        "does not recover."
    )


def _action_safe_to_retry(action: str, params: Optional[dict[str, Any]] = None) -> bool:
    safety = _operation_safety(action, params)
    if not safety:
        return False
    return (
        bool(safety.get("readOnlyHint"))
        and bool(safety.get("idempotentHint"))
        and not bool(safety.get("destructiveHint"))
    )


def _unknown_commit_state_help(action: str, error: BaseException) -> str:
    original = getattr(error, "original", error)
    return (
        "Unknown commit state after sending non-idempotent Vectorworks action "
        "'{action}': {err}\n\n"
        "The request may or may not have completed inside Vectorworks. The MCP "
        "host did not retry it, because retrying could duplicate or compound CAD "
        "changes. Check the Vectorworks document state, then rerun only the exact "
        "follow-up action you still need."
    ).format(action=action, err=original)


def _request_not_sent_help(action: str, error: BaseException) -> str:
    original = getattr(error, "original", error)
    return (
        "Request was not sent to Vectorworks for action '{action}': {err}\n\n"
        "No CAD changes were started by this failed request. Fix the request "
        "payload or VW_MCP_MAX_FRAME_BYTES, then retry when ready."
    ).format(action=action, err=original)


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


def _native_readiness_errors(status: dict[str, Any]) -> list[str]:
    if status.get("native_bridge") is not True:
        return []

    errors: list[str] = []
    dispatch_mode = str(status.get("dispatch_mode", "") or "").strip().lower()
    bridge_kind = str(status.get("bridge_kind", "") or "").strip().lower()
    if dispatch_mode != "native_sdk":
        errors.append("dispatch_mode is not native_sdk")
    if not bridge_kind.startswith("native_sdk_bridge"):
        errors.append("bridge_kind does not start with native_sdk_bridge")

    native_phase = status.get("native_phase")
    if not isinstance(native_phase, int) or isinstance(native_phase, bool) or native_phase < 1:
        errors.append("native_phase is not >= 1")

    implemented_actions = status.get("implemented_actions")
    if not isinstance(implemented_actions, list) or not all(isinstance(action, str) for action in implemented_actions):
        errors.append("implemented_actions is not a list of strings")
    else:
        missing_actions = sorted(NATIVE_PHASE_ONE_REQUIRED_ACTIONS - set(implemented_actions))
        if missing_actions:
            errors.append("implemented_actions missing: {0}".format(", ".join(missing_actions)))

    if status.get("main_context_pump") != "win32_ui_timer":
        errors.append("main_context_pump is not win32_ui_timer")
    if status.get("main_context_pump_ready") is not True:
        errors.append("main_context_pump_ready is not true")

    return errors


def _native_action_readiness_errors(
    status: dict[str, Any],
    blocked_action: Optional[str],
    blocked_params: Optional[dict[str, Any]] = None,
) -> list[str]:
    if status.get("native_bridge") is not True or not blocked_action:
        return []

    errors: list[str] = []
    implemented_actions = status.get("implemented_actions")
    if isinstance(implemented_actions, list) and all(isinstance(action, str) for action in implemented_actions):
        if blocked_action not in set(implemented_actions):
            errors.append("action is not implemented by native bridge: {0}".format(blocked_action))

    params = blocked_params or {}
    if blocked_action == "create_object":
        object_type = str(params.get("object_type", "") or "").strip().lower()
        if object_type and object_type not in NATIVE_PHASE_ONE_CREATE_OBJECT_TYPES:
            errors.append("create_object object_type is not implemented by native bridge: {0}".format(object_type))
    elif blocked_action == "selection":
        selection_action = str(params.get("action", "") or "").strip().lower()
        if selection_action and selection_action not in NATIVE_PHASE_ONE_SELECTION_ACTIONS:
            errors.append("selection action is not implemented by native bridge: {0}".format(selection_action))

    return errors


def _evaluate_cad_preflight_status(
    status: Any,
    blocked_action: Optional[str] = None,
    blocked_params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
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

    dispatch_mode = str(status.get("dispatch_mode", "") or "").lower()
    bridge_kind = str(status.get("bridge_kind", "") or "").lower()
    if dispatch_mode == "foreground" or bridge_kind == "python_foreground_diagnostic":
        return _with_block_context(
            {
                "ok": False,
                "cad_api_safe": False,
                "bridge_kind": status.get("bridge_kind", "unknown"),
                "dispatch_mode": status.get("dispatch_mode", "unknown"),
                "transport_only": bool(status.get("transport_only")),
                "native_bridge": bool(status.get("native_bridge")),
                "reason": "foreground_diagnostic_bridge",
                "next_action": "Do not call CAD handlers. Replace the old foreground script with vw_load_listener_2024.py or use a compiled native SDK bridge.",
                "raw_status": status,
            },
            blocked_action,
        )

    native_errors = _native_readiness_errors(status)
    if native_errors:
        return _with_block_context(
            {
                "ok": False,
                "cad_api_safe": False,
                "bridge_kind": status.get("bridge_kind", "unknown"),
                "dispatch_mode": status.get("dispatch_mode", "unknown"),
                "transport_only": bool(status.get("transport_only")),
                "native_bridge": True,
                "handlers": status.get("handlers"),
                "version": status.get("version"),
                "main_context_pump": status.get("main_context_pump"),
                "main_context_pump_ready": status.get("main_context_pump_ready"),
                "reason": "native_bridge_not_phase1_ready",
                "next_action": "Do not call CAD handlers. Run scripts\\smoke-native-bridge.ps1 -Json and fix native bridge capabilities.",
                "native_readiness_errors": native_errors,
                "raw_status": status,
            },
            blocked_action,
        )

    native_action_errors = _native_action_readiness_errors(status, blocked_action, blocked_params)
    if native_action_errors:
        return _with_block_context(
            {
                "ok": False,
                "cad_api_safe": False,
                "bridge_kind": status.get("bridge_kind", "unknown"),
                "dispatch_mode": status.get("dispatch_mode", "unknown"),
                "transport_only": bool(status.get("transport_only")),
                "native_bridge": True,
                "handlers": status.get("handlers"),
                "version": status.get("version"),
                "main_context_pump": status.get("main_context_pump"),
                "main_context_pump_ready": status.get("main_context_pump_ready"),
                "implemented_actions": status.get("implemented_actions"),
                "reason": "native_bridge_action_not_implemented",
                "next_action": "Do not dispatch this CAD action to the native bridge. Use an implemented action, switch to the Python dialog listener for broader legacy coverage, or implement the native handler first.",
                "native_readiness_errors": native_action_errors,
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
            "main_context_pump": status.get("main_context_pump"),
            "main_context_pump_ready": status.get("main_context_pump_ready"),
            "reason": "cad_api_safe",
            "next_action": "Call vw_get_document_info before non-trivial CAD work.",
            "raw_status": status,
        }

    if status.get("transport_only") is True:
        reason = "transport_only_bridge"
        next_action = "Do not call CAD handlers. Regenerate/copy/run the stable loader or use a compiled native SDK bridge."
    elif "cad_api_safe" not in status:
        reason = "legacy_status_without_cad_api_safe"
        next_action = "Update/regenerate the Vectorworks listener before real CAD work."
    else:
        reason = "listener_reports_cad_api_unsafe"
        next_action = "Do not call CAD handlers until the stable loader or native SDK bridge is active."

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
    with _cad_safe_cache_lock:
        _cad_safe_cache = (time.monotonic(), dict(status))


def _cached_cad_safe_status() -> Optional[dict[str, Any]]:
    if PREFLIGHT_CACHE_SECONDS <= 0:
        return None
    with _cad_safe_cache_lock:
        if _cad_safe_cache is None:
            return None
        timestamp, status = _cad_safe_cache
        if time.monotonic() - timestamp <= PREFLIGHT_CACHE_SECONDS:
            return dict(status)
    _clear_cad_safe_cache()
    return None


def _cad_preflight_block(action: str, params: Optional[dict[str, Any]] = None) -> Optional[str]:
    cached_status = _cached_cad_safe_status()
    if cached_status is not None:
        payload = _evaluate_cad_preflight_status(cached_status, blocked_action=action, blocked_params=params)
        if payload["ok"]:
            return None
        return json.dumps(payload, indent=2, sort_keys=True)

    response = _request_once_health("ping", None)
    if response.get("success") is not True:
        payload = _cad_preflight_ping_error_payload(response, blocked_action=action)
        return json.dumps(payload, indent=2, sort_keys=True)

    status = response.get("result")
    payload = _evaluate_cad_preflight_status(status, blocked_action=action, blocked_params=params)
    if payload["ok"] and isinstance(status, dict):
        _remember_cad_safe_status(status)
        return None
    return json.dumps(payload, indent=2, sort_keys=True)


def _request_once(action: str, params: Optional[dict[str, Any]]) -> dict[str, Any]:
    _connect()
    request_id = uuid.uuid4().hex[:8]
    request = {"id": request_id, "action": action, "params": params or {}}
    try:
        payload = _json_bytes(request)
        _send_frame(payload)
    except ProtocolError as exc:
        raise RequestNotSentError(action, exc) from exc
    except (ConnectionError, TimeoutError, socket.timeout, OSError) as exc:
        raise RequestTransportError(action, "send", exc) from exc

    try:
        response = _decode_response(_recv_frame())
    except (ConnectionError, TimeoutError, socket.timeout, OSError) as exc:
        raise RequestTransportError(action, "response", exc) from exc
    _validate_response_envelope(response, request_id, action)
    return response


def _request_once_health(action: str, params: Optional[dict[str, Any]]) -> dict[str, Any]:
    request_id = uuid.uuid4().hex[:8]
    request = {"id": request_id, "action": action, "params": params or {}}
    try:
        payload = _json_bytes(request)
    except ProtocolError as exc:
        raise RequestNotSentError(action, exc) from exc

    try:
        with socket.create_connection((HOST, PORT), timeout=HEALTH_TIMEOUT) as sock:
            sock.settimeout(HEALTH_TIMEOUT)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                _send_frame_to(sock, payload)
            except ProtocolError as exc:
                raise RequestNotSentError(action, exc) from exc
            response = _decode_response(_recv_frame_from(sock))
    except (ConnectionError, TimeoutError, socket.timeout, OSError) as exc:
        raise RequestTransportError(action, "health", exc) from exc
    _validate_response_envelope(response, request_id, action)
    return response


def _send_health(action: str = "ping", params: Optional[dict[str, Any]] = None) -> str:
    if _CONFIG_ERROR:
        return f"Configuration error: {_CONFIG_ERROR}"
    try:
        response = _request_once_health(action, params)
        if response.get("success") is True:
            return _format_result(response.get("result", "OK"))
        return f"VW Error ({action}): {response.get('error', 'Unknown listener error')}"
    except RequestNotSentError as exc:
        return _request_not_sent_help(action, exc)
    except ProtocolError as exc:
        _close()
        return f"Protocol error: {exc}. Restart the Vectorworks listener if this persists."
    except RequestTransportError as exc:
        return _connection_help(exc.original)
    except (ConnectionError, TimeoutError, socket.timeout, OSError) as exc:
        return _connection_help(exc)
    except Exception as exc:
        return f"Unexpected error while talking to Vectorworks: {exc}"


def _send(action: str, params: Optional[dict[str, Any]] = None, require_cad_safe: bool = False) -> str:
    if _CONFIG_ERROR:
        return f"Configuration error: {_CONFIG_ERROR}"

    with _lock:
        for attempt in (0, 1):
            try:
                if require_cad_safe:
                    try:
                        blocked = _cad_preflight_block(action, params)
                    except ProtocolError as exc:
                        _close()
                        return f"Protocol error: {exc}. Restart the Vectorworks listener if this persists."
                    if blocked:
                        return blocked
                response = _request_once(action, params)
                if response.get("success") is True:
                    return _format_result(response.get("result", "OK"))
                return f"VW Error ({action}): {response.get('error', 'Unknown listener error')}"
            except RequestNotSentError as exc:
                _close()
                return _request_not_sent_help(action, exc)
            except ProtocolError as exc:
                _close()
                if not _action_safe_to_retry(action, params):
                    return _unknown_commit_state_help(action, exc)
                return f"Protocol error: {exc}. Restart the Vectorworks listener if this persists."
            except RequestTransportError as exc:
                _close()
                if exc.action != action:
                    if attempt == 0:
                        continue
                    return _connection_help(exc.original)
                if attempt == 0 and _action_safe_to_retry(action, params):
                    continue
                if not _action_safe_to_retry(action, params):
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


@_tool("vw_capabilities")
def vw_capabilities(include_tools: bool = True) -> str:
    """Return current bridge capabilities and the MCP tool surface agents can safely plan against."""
    raw_status = _send_health("ping")
    decoded_status = _decode_tool_result(raw_status)
    status_ok = not _tool_result_failed(raw_status, decoded_status)
    payload: dict[str, Any] = {
        "ok": status_ok,
        "tool": "vw_capabilities",
        "bridge_status": decoded_status,
        "native_phase_one_required_actions": sorted(NATIVE_PHASE_ONE_REQUIRED_ACTIONS),
        "native_phase_one_create_object_types": sorted(NATIVE_PHASE_ONE_CREATE_OBJECT_TYPES),
        "native_phase_one_selection_actions": sorted(NATIVE_PHASE_ONE_SELECTION_ACTIONS),
        "host_capabilities": {
            "batch_primitive_creation": True,
            "schematic_floor_plan_planning": True,
            "schematic_floor_plan_creation": True,
            "drawing_summary": True,
            "true_bim_objects": False,
        },
        "notes": [
            "Native phase 1 supports 2D primitives, reads, and bounded selection operations.",
            "Schematic floor-plan tools create drafting geometry, not BIM wall/door/window objects.",
        ],
    }
    if include_tools:
        payload["tools"] = sorted(TOOL_SAFETY)
        payload["tool_safety"] = TOOL_SAFETY
    return json.dumps(payload, indent=2, sort_keys=True)


@_tool("vw_run_script")
def vw_run_script(code: str, confirm: str = "") -> str:
    """Execute Python inside Vectorworks. The 'vs' module is available.
    Use print() to return output. Escape hatch for anything other tools do not cover.
    Requires confirm='RUN_TRUSTED_CODE'. Example:
    vw_run_script("h = vs.FSActLayer()\\nprint(vs.GetName(h))", confirm="RUN_TRUSTED_CODE")"""
    if confirm != "RUN_TRUSTED_CODE":
        return _confirmation_error(
            "vw_run_script",
            "RUN_TRUSTED_CODE",
            "vw_run_script executes trusted code inside Vectorworks and requires explicit confirmation",
        )
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


def _floor_plan_error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, sort_keys=True)


def _decode_tool_result(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _tool_result_failed(raw: str, decoded: Any) -> bool:
    if isinstance(decoded, dict):
        if decoded.get("blocked") is True:
            return True
        if decoded.get("ok") is False and ("reason" in decoded or "error" in decoded):
            return True
    return raw.startswith(
        (
            "Configuration error:",
            "Connection error:",
            "Protocol error:",
            "Request was not sent",
            "Unexpected error",
            "Unknown commit state",
            "VW Error",
        )
    )


def _send_create_primitive(params: dict[str, Any]) -> str:
    return _send_tool("vw_create_object", params)


_PRIMITIVE_COORD_KEYS = ("x1", "y1", "x2", "y2")
_PRIMITIVE_ALLOWED_KEYS = {
    "role",
    "object_type",
    "type",
    "x1",
    "y1",
    "x2",
    "y2",
    "radius",
    "start_angle",
    "sweep_angle",
    "name",
    "class_name",
}


def _json_error(tool: str, message: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "tool": tool, "error": message, **extra}, indent=2, sort_keys=True)


def _confirmation_error(tool: str, required_confirmation: str, reason: str) -> str:
    return _json_error(
        tool,
        reason,
        confirmation_required=True,
        required_confirmation=required_confirmation,
    )


def _is_real_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))


def _coerce_number(
    item: dict[str, Any],
    key: str,
    *,
    default: Optional[float] = None,
    required: bool = False,
    min_value: Optional[float] = None,
    label: str = "item",
) -> float:
    if key not in item or item.get(key) is None:
        if required:
            raise ValueError(f"{label}.{key} is required")
        if default is None:
            raise ValueError(f"{label}.{key} has no default")
        return float(default)
    value = item[key]
    if not _is_real_number(value):
        raise ValueError(f"{label}.{key} must be a finite number")
    result = float(value)
    if min_value is not None and result < min_value:
        raise ValueError(f"{label}.{key} must be >= {min_value}")
    return result


def _coerce_positive_number(
    item: dict[str, Any],
    key: str,
    *,
    default: Optional[float] = None,
    label: str = "item",
) -> float:
    result = _coerce_number(item, key, default=default, required=default is None, min_value=0, label=label)
    if result <= 0:
        raise ValueError(f"{label}.{key} must be > 0")
    return result


def _optional_text(item: dict[str, Any], key: str, default: str = "") -> str:
    value = item.get(key, default)
    if value is None:
        return default
    return str(value)


def _normalise_create_primitive(
    raw: dict[str, Any],
    *,
    label: str,
    default_class_name: str = "",
    name_prefix: str = "",
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")

    unknown = sorted(set(raw) - _PRIMITIVE_ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"{label} has unsupported key(s): {', '.join(unknown)}")

    object_type = str(raw.get("object_type", raw.get("type", "")) or "").strip().lower()
    if object_type == "rectangle" or object_type == "box":
        object_type = "rect"
    if object_type == "polygon":
        raise ValueError(f"{label}.object_type polygon is not supported by the native phase-1 bridge")
    if object_type not in NATIVE_PHASE_ONE_CREATE_OBJECT_TYPES:
        raise ValueError(f"{label}.object_type must be one of: {', '.join(sorted(NATIVE_PHASE_ONE_CREATE_OBJECT_TYPES))}")

    params: dict[str, Any] = {"object_type": object_type}
    if object_type in {"rect", "oval", "line"}:
        for key in _PRIMITIVE_COORD_KEYS:
            params[key] = _coerce_number(raw, key, required=True, label=label)
        if object_type == "line" and params["x1"] == params["x2"] and params["y1"] == params["y2"]:
            raise ValueError(f"{label} line endpoints must not be identical")
    elif object_type == "circle":
        params["x1"] = _coerce_number(raw, "x1", required=True, label=label)
        params["y1"] = _coerce_number(raw, "y1", required=True, label=label)
        params["radius"] = _coerce_positive_number(raw, "radius", label=label)
    elif object_type == "arc":
        params["x1"] = _coerce_number(raw, "x1", required=True, label=label)
        params["y1"] = _coerce_number(raw, "y1", required=True, label=label)
        params["radius"] = _coerce_positive_number(raw, "radius", label=label)
        params["start_angle"] = _coerce_number(raw, "start_angle", default=0, label=label)
        params["sweep_angle"] = _coerce_number(raw, "sweep_angle", default=90, label=label)

    name = _optional_text(raw, "name", "")
    if name_prefix:
        name = f"{name_prefix} {name}".strip() if name else name_prefix
    if name:
        params["name"] = name

    class_name = _optional_text(raw, "class_name", default_class_name)
    if class_name:
        params["class_name"] = class_name

    role = _optional_text(raw, "role", "primitive")
    if role:
        params["role"] = role
    return params


def _create_primitives(
    tool: str,
    primitives: list[dict[str, Any]],
    metadata: dict[str, Any],
    *,
    schematic: bool = False,
    bim_objects: bool = False,
    stop_on_error: bool = True,
) -> str:
    created: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, primitive in enumerate(primitives, start=1):
        params = dict(primitive)
        role = str(params.pop("role", "primitive"))
        raw = _send_create_primitive(params)
        decoded = _decode_tool_result(raw)
        entry = {
            "index": index,
            "role": role,
            "object_type": params.get("object_type"),
            "params": params,
            "result": decoded,
        }
        if _tool_result_failed(raw, decoded):
            failures.append(entry)
            if stop_on_error:
                break
            continue
        created.append(entry)

    if failures:
        return json.dumps(
            {
                "ok": False,
                "tool": tool,
                "schematic": schematic,
                "bim_objects": bim_objects,
                "attempted_count": len(created) + len(failures),
                "created_count": len(created),
                "failed_count": len(failures),
                "created": created,
                "failures": failures,
                "warning": "Primitive creation is not atomic; earlier successful primitives may already exist in the active Vectorworks document.",
                **metadata,
            },
            indent=2,
            sort_keys=True,
        )

    return json.dumps(
        {
            "ok": True,
            "tool": tool,
            "schematic": schematic,
            "bim_objects": bim_objects,
            "attempted_count": len(created),
            "created_count": len(created),
            "created": created,
            **metadata,
        },
        indent=2,
        sort_keys=True,
    )


def _create_floor_plan_primitives(tool: str, primitives: list[dict[str, Any]], metadata: dict[str, Any]) -> str:
    return _create_primitives(tool, primitives, metadata, schematic=True, bim_objects=False)


def _named(base: str, suffix: str) -> str:
    base = str(base or "").strip()
    if not base:
        return ""
    return f"{base} {suffix}"


def _line_endpoint(x: float, y: float, length: float, angle_degrees: float) -> tuple[float, float]:
    radians = math.radians(angle_degrees)
    return (x + length * math.cos(radians), y + length * math.sin(radians))


def _room_primitives(
    x: float,
    y: float,
    width: float,
    depth: float,
    wall_thickness: float,
    *,
    name: str = "",
    class_name: str = "A-FP-Schematic-Wall",
    role_prefix: str = "",
) -> list[dict[str, Any]]:
    if width <= 0 or depth <= 0:
        raise ValueError("room width and depth must be > 0")
    if wall_thickness <= 0:
        raise ValueError("wall_thickness must be > 0")
    if wall_thickness * 2 >= min(width, depth):
        raise ValueError("wall_thickness must be less than half of both width and depth")

    x2 = x + width
    y2 = y + depth
    t = wall_thickness
    prefix = f"{role_prefix}_" if role_prefix else ""
    return [
        {
            "role": f"{prefix}south_wall",
            "object_type": "rect",
            "x1": x,
            "y1": y,
            "x2": x2,
            "y2": y + t,
            "name": _named(name, "south wall"),
            "class_name": class_name,
        },
        {
            "role": f"{prefix}north_wall",
            "object_type": "rect",
            "x1": x,
            "y1": y2 - t,
            "x2": x2,
            "y2": y2,
            "name": _named(name, "north wall"),
            "class_name": class_name,
        },
        {
            "role": f"{prefix}west_wall",
            "object_type": "rect",
            "x1": x,
            "y1": y + t,
            "x2": x + t,
            "y2": y2 - t,
            "name": _named(name, "west wall"),
            "class_name": class_name,
        },
        {
            "role": f"{prefix}east_wall",
            "object_type": "rect",
            "x1": x2 - t,
            "y1": y + t,
            "x2": x2,
            "y2": y2 - t,
            "name": _named(name, "east wall"),
            "class_name": class_name,
        },
    ]


def _door_primitives(
    hinge_x: float,
    hinge_y: float,
    width: float,
    rotation: float,
    swing: DoorSwing,
    *,
    name: str = "",
    class_name: str = "A-FP-Schematic-Door",
    role_prefix: str = "",
) -> list[dict[str, Any]]:
    if width <= 0:
        raise ValueError("door width must be > 0")
    if swing not in ("left", "right"):
        raise ValueError("door swing must be left or right")

    sweep_angle = 90 if swing == "left" else -90
    leaf_angle = rotation + sweep_angle
    leaf_x, leaf_y = _line_endpoint(hinge_x, hinge_y, width, leaf_angle)
    prefix = f"{role_prefix}_" if role_prefix else ""
    return [
        {
            "role": f"{prefix}door_leaf",
            "object_type": "line",
            "x1": hinge_x,
            "y1": hinge_y,
            "x2": leaf_x,
            "y2": leaf_y,
            "name": _named(name, "leaf"),
            "class_name": class_name,
        },
        {
            "role": f"{prefix}door_swing",
            "object_type": "arc",
            "x1": hinge_x,
            "y1": hinge_y,
            "radius": width,
            "start_angle": rotation,
            "sweep_angle": sweep_angle,
            "name": _named(name, "swing"),
            "class_name": class_name,
        },
    ]


def _window_primitives(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    marker_depth: float,
    *,
    name: str = "",
    class_name: str = "A-FP-Schematic-Window",
    role_prefix: str = "",
) -> list[dict[str, Any]]:
    if marker_depth <= 0:
        raise ValueError("window marker_depth must be > 0")
    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy)
    if length <= 0:
        raise ValueError("window endpoints must not be identical")

    offset_x = (-dy / length) * (marker_depth / 2)
    offset_y = (dx / length) * (marker_depth / 2)
    prefix = f"{role_prefix}_" if role_prefix else ""
    return [
        {
            "role": f"{prefix}window_line_a",
            "object_type": "line",
            "x1": x1 + offset_x,
            "y1": y1 + offset_y,
            "x2": x2 + offset_x,
            "y2": y2 + offset_y,
            "name": _named(name, "line A"),
            "class_name": class_name,
        },
        {
            "role": f"{prefix}window_line_b",
            "object_type": "line",
            "x1": x1 - offset_x,
            "y1": y1 - offset_y,
            "x2": x2 - offset_x,
            "y2": y2 - offset_y,
            "name": _named(name, "line B"),
            "class_name": class_name,
        },
    ]


def _wall_segment_primitives(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    thickness: float,
    *,
    name: str = "",
    class_name: str = "A-FP-Schematic-Wall",
    role: str = "wall_segment",
) -> tuple[list[dict[str, Any]], list[str]]:
    if x1 == x2 and y1 == y2:
        raise ValueError("wall segment endpoints must not be identical")
    if thickness <= 0:
        return (
            [
                {
                    "role": role,
                    "object_type": "line",
                    "x1": x1,
                    "y1": y1,
                    "x2": x2,
                    "y2": y2,
                    "name": name,
                    "class_name": class_name,
                }
            ],
            [],
        )

    half = thickness / 2
    if y1 == y2:
        return (
            [
                {
                    "role": role,
                    "object_type": "rect",
                    "x1": min(x1, x2),
                    "y1": y1 - half,
                    "x2": max(x1, x2),
                    "y2": y1 + half,
                    "name": name,
                    "class_name": class_name,
                }
            ],
            [],
        )
    if x1 == x2:
        return (
            [
                {
                    "role": role,
                    "object_type": "rect",
                    "x1": x1 - half,
                    "y1": min(y1, y2),
                    "x2": x1 + half,
                    "y2": max(y1, y2),
                    "name": name,
                    "class_name": class_name,
                }
            ],
            [],
        )
    return (
        [
            {
                "role": role,
                "object_type": "line",
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "name": name,
                "class_name": class_name,
            }
        ],
        ["angled wall segment drawn as a centerline because native phase 1 has no polygon or rotated-rectangle primitive"],
    )


def _prefixed_name(prefix: str, name: str, fallback: str) -> str:
    name = str(name or "").strip() or fallback
    prefix = str(prefix or "").strip()
    return f"{prefix} {name}".strip() if prefix else name


def _build_schematic_floor_plan_primitives(
    rooms: list[dict[str, Any]],
    walls: Optional[list[dict[str, Any]]],
    doors: Optional[list[dict[str, Any]]],
    windows: Optional[list[dict[str, Any]]],
    *,
    wall_thickness: float,
    name: str,
    wall_class: str,
    door_class: str,
    window_class: str,
) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    if not rooms:
        raise ValueError("at least one room is required")

    primitives: list[dict[str, Any]] = []
    warnings: list[str] = []
    counts = {
        "rooms_count": len(rooms),
        "wall_segments_count": len(walls or []),
        "doors_count": len(doors or []),
        "windows_count": len(windows or []),
    }

    for index, room in enumerate(rooms, start=1):
        label = f"rooms[{index}]"
        if not isinstance(room, dict):
            raise ValueError(f"{label} must be an object")
        room_name = _prefixed_name(name, _optional_text(room, "name"), f"room {index}")
        room_class = _optional_text(room, "class_name", wall_class)
        primitives.extend(
            _room_primitives(
                _coerce_number(room, "x", required=True, label=label),
                _coerce_number(room, "y", required=True, label=label),
                _coerce_positive_number(room, "width", label=label),
                _coerce_positive_number(room, "depth", label=label),
                _coerce_positive_number(room, "wall_thickness", default=wall_thickness, label=label),
                name=room_name,
                class_name=room_class,
                role_prefix=f"room_{index}",
            )
        )

    for index, wall in enumerate(walls or [], start=1):
        label = f"walls[{index}]"
        if not isinstance(wall, dict):
            raise ValueError(f"{label} must be an object")
        wall_name = _prefixed_name(name, _optional_text(wall, "name"), f"wall segment {index}")
        wall_class_name = _optional_text(wall, "class_name", wall_class)
        wall_primitives, wall_warnings = _wall_segment_primitives(
            _coerce_number(wall, "x1", required=True, label=label),
            _coerce_number(wall, "y1", required=True, label=label),
            _coerce_number(wall, "x2", required=True, label=label),
            _coerce_number(wall, "y2", required=True, label=label),
            _coerce_number(wall, "thickness", default=wall_thickness, min_value=0, label=label),
            name=wall_name,
            class_name=wall_class_name,
            role=f"wall_segment_{index}",
        )
        primitives.extend(wall_primitives)
        warnings.extend([f"{label}: {warning}" for warning in wall_warnings])

    for index, door in enumerate(doors or [], start=1):
        label = f"doors[{index}]"
        if not isinstance(door, dict):
            raise ValueError(f"{label} must be an object")
        swing = _optional_text(door, "swing", "left").lower()
        if swing not in ("left", "right"):
            raise ValueError(f"{label}.swing must be left or right")
        typed_swing: DoorSwing = "left" if swing == "left" else "right"
        door_name = _prefixed_name(name, _optional_text(door, "name"), f"door {index}")
        door_class_name = _optional_text(door, "class_name", door_class)
        primitives.extend(
            _door_primitives(
                _coerce_number(door, "hinge_x", required=True, label=label),
                _coerce_number(door, "hinge_y", required=True, label=label),
                _coerce_positive_number(door, "width", default=900, label=label),
                _coerce_number(door, "rotation", default=0, label=label),
                typed_swing,
                name=door_name,
                class_name=door_class_name,
                role_prefix=f"door_{index}",
            )
        )

    for index, window in enumerate(windows or [], start=1):
        label = f"windows[{index}]"
        if not isinstance(window, dict):
            raise ValueError(f"{label} must be an object")
        window_name = _prefixed_name(name, _optional_text(window, "name"), f"window {index}")
        window_class_name = _optional_text(window, "class_name", window_class)
        primitives.extend(
            _window_primitives(
                _coerce_number(window, "x1", required=True, label=label),
                _coerce_number(window, "y1", required=True, label=label),
                _coerce_number(window, "x2", required=True, label=label),
                _coerce_number(window, "y2", required=True, label=label),
                _coerce_positive_number(window, "marker_depth", default=150, label=label),
                name=window_name,
                class_name=window_class_name,
                role_prefix=f"window_{index}",
            )
        )

    return primitives, warnings, counts


@_tool("vw_batch_create_objects")
def vw_batch_create_objects(
    objects: PrimitiveObjectList,
    default_class_name: str = "",
    name_prefix: str = "",
    stop_on_error: bool = True,
) -> str:
    """Create many native phase-1 primitives in one MCP call.
    Supported object_type values are rect/rectangle/box, circle, oval, line, and arc. Not atomic."""
    try:
        primitives = [
            _normalise_create_primitive(
                item,
                label=f"objects[{index}]",
                default_class_name=default_class_name,
                name_prefix=name_prefix,
            )
            for index, item in enumerate(objects, start=1)
        ]
    except ValueError as exc:
        return _json_error("vw_batch_create_objects", str(exc))

    return _create_primitives(
        "vw_batch_create_objects",
        primitives,
        {
            "primitive_count": len(primitives),
            "default_class_name": default_class_name,
            "name_prefix": name_prefix,
            "stop_on_error": stop_on_error,
        },
        schematic=False,
        bim_objects=False,
        stop_on_error=stop_on_error,
    )


@_tool("vw_plan_schematic_floor_plan")
def vw_plan_schematic_floor_plan(
    rooms: FloorPlanRoomList,
    walls: Optional[FloorPlanItemList] = None,
    doors: Optional[FloorPlanItemList] = None,
    windows: Optional[FloorPlanItemList] = None,
    wall_thickness: PositiveLength = 200,
    name: str = "",
    wall_class: str = "A-FP-Schematic-Wall",
    door_class: str = "A-FP-Schematic-Door",
    window_class: str = "A-FP-Schematic-Window",
) -> str:
    """Plan a schematic floor plan without touching Vectorworks. Use this before creating large layouts."""
    try:
        primitives, warnings, counts = _build_schematic_floor_plan_primitives(
            rooms,
            walls,
            doors,
            windows,
            wall_thickness=wall_thickness,
            name=name,
            wall_class=wall_class,
            door_class=door_class,
            window_class=window_class,
        )
    except ValueError as exc:
        return _json_error("vw_plan_schematic_floor_plan", str(exc), schematic=True, bim_objects=False)

    return json.dumps(
        {
            "ok": True,
            "tool": "vw_plan_schematic_floor_plan",
            "schematic": True,
            "bim_objects": False,
            "primitive_count": len(primitives),
            "primitives": primitives,
            "warnings": warnings,
            **counts,
        },
        indent=2,
        sort_keys=True,
    )


@_tool("vw_create_schematic_floor_plan")
def vw_create_schematic_floor_plan(
    rooms: FloorPlanRoomList,
    walls: Optional[FloorPlanItemList] = None,
    doors: Optional[FloorPlanItemList] = None,
    windows: Optional[FloorPlanItemList] = None,
    wall_thickness: PositiveLength = 200,
    name: str = "",
    wall_class: str = "A-FP-Schematic-Wall",
    door_class: str = "A-FP-Schematic-Door",
    window_class: str = "A-FP-Schematic-Window",
    stop_on_error: bool = True,
) -> str:
    """Create a multi-room schematic floor plan from structured rooms, wall segments, doors, and windows.
    This creates 2D drafting primitives, not BIM wall/door/window objects."""
    try:
        primitives, warnings, counts = _build_schematic_floor_plan_primitives(
            rooms,
            walls,
            doors,
            windows,
            wall_thickness=wall_thickness,
            name=name,
            wall_class=wall_class,
            door_class=door_class,
            window_class=window_class,
        )
    except ValueError as exc:
        return _json_error("vw_create_schematic_floor_plan", str(exc), schematic=True, bim_objects=False)

    return _create_primitives(
        "vw_create_schematic_floor_plan",
        primitives,
        {
            "primitive_count": len(primitives),
            "warnings": warnings,
            "stop_on_error": stop_on_error,
            **counts,
        },
        schematic=True,
        bim_objects=False,
        stop_on_error=stop_on_error,
    )


@_tool("vw_create_schematic_room")
def vw_create_schematic_room(
    x: float,
    y: float,
    width: PositiveLength,
    depth: PositiveLength,
    wall_thickness: PositiveLength = 200,
    name: str = "",
    class_name: str = "A-FP-Schematic-Wall",
) -> str:
    """Create a rectangular schematic room from four 2D wall rectangles.
    Coordinates use the active document units. This is drafting geometry, not BIM walls."""
    try:
        primitives = _room_primitives(x, y, width, depth, wall_thickness, name=name, class_name=class_name)
    except ValueError as exc:
        return _floor_plan_error(str(exc))

    return _create_floor_plan_primitives(
        "vw_create_schematic_room",
        primitives,
        {"origin": [x, y], "width": width, "depth": depth, "wall_thickness": wall_thickness},
    )


@_tool("vw_create_schematic_door")
def vw_create_schematic_door(
    hinge_x: float,
    hinge_y: float,
    width: PositiveLength = 900,
    rotation: float = 0,
    swing: DoorSwing = "left",
    name: str = "",
    class_name: str = "A-FP-Schematic-Door",
) -> str:
    """Draw a schematic door leaf and swing arc. This is drafting geometry, not a BIM door."""
    try:
        primitives = _door_primitives(
            hinge_x,
            hinge_y,
            width,
            rotation,
            swing,
            name=name,
            class_name=class_name,
        )
    except ValueError as exc:
        return _floor_plan_error(str(exc))

    return _create_floor_plan_primitives(
        "vw_create_schematic_door",
        primitives,
        {
            "hinge": [hinge_x, hinge_y],
            "width": width,
            "rotation": rotation,
            "swing": swing,
        },
    )


@_tool("vw_create_schematic_window")
def vw_create_schematic_window(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    marker_depth: PositiveLength = 150,
    name: str = "",
    class_name: str = "A-FP-Schematic-Window",
) -> str:
    """Draw a schematic double-line window marker between two points.
    This is drafting geometry, not a BIM window."""
    try:
        primitives = _window_primitives(
            x1,
            y1,
            x2,
            y2,
            marker_depth,
            name=name,
            class_name=class_name,
        )
    except ValueError as exc:
        return _floor_plan_error(str(exc))

    return _create_floor_plan_primitives(
        "vw_create_schematic_window",
        primitives,
        {
            "start": [x1, y1],
            "end": [x2, y2],
            "marker_depth": marker_depth,
        },
    )


@_tool("vw_get_layers")
def vw_get_layers() -> str:
    """List all layers with name and visibility."""
    return _send_tool("vw_get_layers")


@_tool("vw_get_objects")
def vw_get_objects(layer: str = "", object_type: str = "", limit: ObjectQueryLimit = 100) -> str:
    """List objects. Filter by layer name and type such as rect, line, or wall."""
    return _send_tool("vw_get_objects", {"layer": layer, "object_type": object_type, "limit": limit})


@_tool("vw_drawing_summary")
def vw_drawing_summary(layer: str = "", object_type: str = "", limit: ObjectQueryLimit = 1000) -> str:
    """Summarize document, layers, and a bounded object inventory for production planning/verification."""
    steps = [
        ("document_info", lambda: _send_tool("vw_get_document_info")),
        ("layers", lambda: _send_tool("vw_get_layers")),
        ("objects", lambda: _send_tool("vw_get_objects", {"layer": layer, "object_type": object_type, "limit": limit})),
    ]
    decoded: dict[str, Any] = {}
    for step, call in steps:
        raw = call()
        value = _decode_tool_result(raw)
        if _tool_result_failed(raw, value):
            return json.dumps(
                {
                    "ok": False,
                    "tool": "vw_drawing_summary",
                    "failed_step": step,
                    "result": value,
                },
                indent=2,
                sort_keys=True,
            )
        decoded[step] = value

    document_info = decoded["document_info"] if isinstance(decoded["document_info"], dict) else {}
    layers = decoded["layers"] if isinstance(decoded["layers"], list) else []
    objects = decoded["objects"] if isinstance(decoded["objects"], list) else []

    by_type: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    by_layer_type: dict[str, dict[str, int]] = {}
    named_count = 0
    bounds: Optional[dict[str, float]] = None
    examples: list[dict[str, Any]] = []

    for obj in objects:
        if not isinstance(obj, dict):
            continue
        obj_type = str(obj.get("type") or "unknown")
        obj_layer = str(obj.get("layer") or "unknown")
        by_type[obj_type] = by_type.get(obj_type, 0) + 1
        by_layer[obj_layer] = by_layer.get(obj_layer, 0) + 1
        layer_counts = by_layer_type.setdefault(obj_layer, {})
        layer_counts[obj_type] = layer_counts.get(obj_type, 0) + 1
        if str(obj.get("name") or "").strip():
            named_count += 1
        if len(examples) < 20:
            examples.append(
                {
                    key: obj.get(key)
                    for key in ("handle", "type", "name", "layer", "bounds")
                    if key in obj
                }
            )

        obj_bounds = obj.get("bounds")
        if isinstance(obj_bounds, dict):
            top_left = obj_bounds.get("top_left")
            bottom_right = obj_bounds.get("bottom_right")
            if (
                isinstance(top_left, list)
                and isinstance(bottom_right, list)
                and len(top_left) >= 2
                and len(bottom_right) >= 2
                and all(_is_real_number(value) for value in top_left[:2] + bottom_right[:2])
            ):
                x_values = [float(top_left[0]), float(bottom_right[0])]
                y_values = [float(top_left[1]), float(bottom_right[1])]
                left, right = min(x_values), max(x_values)
                top, bottom = min(y_values), max(y_values)
                if bounds is None:
                    bounds = {"left": left, "top": top, "right": right, "bottom": bottom}
                else:
                    bounds["left"] = min(bounds["left"], left)
                    bounds["top"] = min(bounds["top"], top)
                    bounds["right"] = max(bounds["right"], right)
                    bounds["bottom"] = max(bounds["bottom"], bottom)

    return json.dumps(
        {
            "ok": True,
            "tool": "vw_drawing_summary",
            "query": {"layer": layer, "object_type": object_type, "limit": limit},
            "document": document_info,
            "layer_count": len(layers),
            "layers": layers,
            "objects_returned": len(objects),
            "document_total_objects": document_info.get("total_objects"),
            "possibly_truncated": len(objects) >= limit,
            "named_objects_returned": named_count,
            "counts_by_type": dict(sorted(by_type.items())),
            "counts_by_layer": dict(sorted(by_layer.items())),
            "counts_by_layer_type": {
                layer_name: dict(sorted(type_counts.items()))
                for layer_name, type_counts in sorted(by_layer_type.items())
            },
            "bounds": bounds,
            "examples": examples,
        },
        indent=2,
        sort_keys=True,
    )


@_tool("vw_set_object_property")
def vw_set_object_property(handle: str, property_name: PropertyName, value: str) -> str:
    """Set an object property. Colors use 'r,g,b' values in Vectorworks 0-65535 color range."""
    return _send_tool("vw_set_object_property", {"handle": handle, "property_name": property_name, "value": value})


@_tool("vw_find_objects")
def vw_find_objects(criteria: str, limit: ObjectQueryLimit = 100) -> str:
    """Find objects using VW criteria such as 'T=RECT', 'T=WALL', 'C=Furniture', or 'ALL'."""
    return _send_tool("vw_find_objects", {"criteria": criteria, "limit": limit})


@_tool("vw_manage_classes")
def vw_manage_classes(action: ClassAction, class_name: str = "", confirm: str = "") -> str:
    """List, create, or delete classes. class_name is ignored for list. Delete requires confirm='DELETE_CLASS'."""
    if action == "delete" and confirm != "DELETE_CLASS":
        return _confirmation_error(
            "vw_manage_classes",
            "DELETE_CLASS",
            "class deletion is destructive and requires explicit confirmation",
        )
    return _send_tool("vw_manage_classes", {"action": action, "class_name": class_name})


@_tool("vw_worksheet")
def vw_worksheet(
    action: WorksheetAction,
    worksheet_name: str = "",
    row: WorksheetRow = 1,
    col: WorksheetColumn = 1,
    value: str = "",
    num_rows: WorksheetRowCount = 10,
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
def vw_export(format: ExportFormat, file_path: NonEmptyPath) -> str:
    """Open the Vectorworks export dialog for pdf, dxf, dwg, or image.
    file_path is the requested save path to choose in the dialog; the listener
    reports whether the operation needs manual save confirmation."""
    return _send_tool("vw_export", {"format": format, "file_path": file_path})


@_tool("vw_import_file")
def vw_import_file(file_path: NonEmptyPath, format: ImportFormat = "auto") -> str:
    """Import a DXF, DWG, or image file. Use auto to detect from the extension."""
    return _send_tool("vw_import_file", {"file_path": file_path, "format": format})


@_tool("vw_get_document_info")
def vw_get_document_info() -> str:
    """Get document metadata: filename, filepath, layer count, object count, and layer names."""
    return _send_tool("vw_get_document_info")


@_tool("vw_screenshot")
def vw_screenshot(file_path: str = "") -> str:
    """Open Vectorworks Export Image File dialog. Empty file_path suggests ~/.vectorworks-mcp/screenshot.png."""
    return _send_tool("vw_screenshot", {"file_path": file_path})


@_tool("vw_ping")
def vw_ping() -> str:
    """Health check. Returns listener version, handler count, and CAD safety status if connected."""
    return _send_health("ping")


@_tool("vw_bridge_status")
def vw_bridge_status() -> str:
    """Return bridge status from the listener, including whether real CAD/API handlers are safe."""
    return _send_health("ping")


@_tool("vw_preflight_for_cad")
def vw_preflight_for_cad() -> str:
    """Return structured go/no-go status before real CAD/API handlers."""
    raw = _send_health("ping")
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
def vw_selection(action: SelectionAction, criteria: str = "", confirm: str = "", limit: ObjectQueryLimit = 1000) -> str:
    """Selection ops. For select, criteria is a VW criteria string. Delete requires confirm='DELETE_SELECTED'."""
    if action == "delete" and confirm != "DELETE_SELECTED":
        return _confirmation_error(
            "vw_selection",
            "DELETE_SELECTED",
            "selection delete is destructive and requires explicit confirmation",
        )
    return _send_tool("vw_selection", {"action": action, "criteria": criteria, "limit": limit})


@_tool("vw_create_wall")
def vw_create_wall(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    height: PositiveLength = 3000,
    thickness: PositiveLength = 200,
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
def vw_insert_door(x: float, y: float, width: PositiveLength = 900, height: PositiveLength = 2100, rotation: float = 0) -> str:
    """Insert parametric door. Place on or near a wall for auto-insertion."""
    return _send_tool("vw_insert_door", {"x": x, "y": y, "width": width, "height": height, "rotation": rotation})


@_tool("vw_insert_window")
def vw_insert_window(
    x: float,
    y: float,
    width: PositiveLength = 1200,
    height: PositiveLength = 1500,
    sill_height: float = 900,
    rotation: float = 0,
) -> str:
    """Insert parametric window. sill_height is floor to window bottom in mm."""
    return _send_tool(
        "vw_insert_window",
        {"x": x, "y": y, "width": width, "height": height, "sill_height": sill_height, "rotation": rotation},
    )


@_tool("vw_create_slab")
def vw_create_slab(points: PolygonPointList, thickness: PositiveLength = 200, elevation: float = 0) -> str:
    """Create an extruded floor-like solid from a polygon. This is not a BIM slab object."""
    return _send_tool("vw_create_slab", {"points": points, "thickness": thickness, "elevation": elevation})


@_tool("vw_create_roof")
def vw_create_roof(
    points: PolygonPointList,
    bearing_height: float = 3000,
    slope: float = 30,
    overhang: float = 500,
    thickness: PositiveLength = 200,
) -> str:
    """Try to create a roof custom object from a footprint, with flat extrusion fallback."""
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
        mcp.run(transport="stdio", show_banner=False)
        return 0
    except RuntimeError as exc:
        print(f"Vectorworks MCP startup error: {exc}", file=sys.stderr)
        return 1
    finally:
        _close()


if __name__ == "__main__":
    raise SystemExit(main())
