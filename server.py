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
  VW_MCP_AUTH_TOKEN       local protocol auth token; defaults to the token file
  VW_MCP_AUTH_TOKEN_FILE  auth token file path; default ~/.vectorworks-mcp/auth-token
  VW_MCP_INSECURE_NO_AUTH set to 1 only for local diagnostics/tests
  VW_MCP_ENABLE_RUN_SCRIPT
                          set to 1 to expose trusted Python execution
  VW_MCP_PREFLIGHT_CACHE_MS
                          safe-CAD preflight success cache in ms, default 5000
  VW_MCP_TOOL_PROFILE     fast-native (default) or explicit diagnostic compat
  VW_MCP_TRACE            set to 1 for token-safe request timing JSON on stderr
"""

import atexit
import functools
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
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
    from fastmcp.tools import ToolResult
except ModuleNotFoundError as exc:
    if exc.name != "fastmcp":
        raise
    FastMCP = None
    ToolResult = None
    _FASTMCP_IMPORT_ERROR: Optional[BaseException] = exc
else:
    _FASTMCP_IMPORT_ERROR = None


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9877
DEFAULT_TIMEOUT = 60.0
DEFAULT_HEALTH_TIMEOUT = 2.0
DEFAULT_MAX_FRAME_BYTES = 16 * 1024 * 1024
DEFAULT_PREFLIGHT_CACHE_MS = 5_000
MAX_PREFLIGHT_CACHE_MS = 5_000
DEFAULT_AUTH_TOKEN_FILENAME = "auth-token"
CONNECTOR_VERSION = "0.5.0"
MIN_FAST_NATIVE_CAPABILITY_REVISION = 4
MCP_SERVER_INSTRUCTIONS = (
    "Use the fast-native phase-4 bridge with capability revision 4 or newer. "
    "Start with vw_status(action='context') and require a capability fingerprint. "
    "Read through vw_read/vw_catalog and send one atomic vw_apply or "
    "vw_execute_operations call with a unique idempotency key for edits; never decompose "
    "work into per-object MCP calls. Use vw_io, vw_view, and vw_document only when their "
    "native action is advertised. Never use modal Python, menus, schematic helpers, or any "
    "fallback. If a capability is unavailable, stop and upgrade/restart the native bridge."
)
MCP_TOOL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}
NATIVE_PHASE_ONE_REQUIRED_ACTIONS = {
    "ping",
    "stop",
    "get_document_info",
    "get_layers",
    "get_objects",
    "selection",
    "create_object",
    "batch_create_objects",
}
NATIVE_PHASE_TWO_REQUIRED_ACTIONS = NATIVE_PHASE_ONE_REQUIRED_ACTIONS | {
    "create_wall",
    "create_text",
    "create_linear_dimension",
    "set_property",
    "manage_classes",
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
NATIVE_PHASE_TWO_CREATE_OBJECT_TYPES = NATIVE_PHASE_ONE_CREATE_OBJECT_TYPES | {
    "dimension",
    "linear_dimension",
    "text",
    "wall",
}
NATIVE_PHASE_FOUR_CREATE_OBJECT_TYPES = NATIVE_PHASE_TWO_CREATE_OBJECT_TYPES | {
    "door",
    "polygon",
    "polyline",
    "parametric",
    "roof",
    "slab",
    "space",
    "symbol",
    "window",
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
    def __init__(self, name: str, **_kwargs: Any):
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


def _validate_loopback_host(host: str, env_name: str = "VW_MCP_HOST") -> str:
    normalized = str(host or "").strip() or DEFAULT_HOST
    if normalized.lower() == "localhost":
        return normalized
    try:
        if ipaddress.ip_address(normalized).is_loopback:
            return normalized
    except ValueError as exc:
        raise ConfigError(f"{env_name} must be a loopback IP address or localhost, got {normalized!r}") from exc
    raise ConfigError(f"{env_name} must be loopback-only; refusing {normalized!r}")


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _default_state_dir() -> Path:
    configured = os.environ.get("VW_MCP_STOP_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    userprofile = os.environ.get("USERPROFILE", "").strip()
    if userprofile:
        return Path(userprofile) / ".vectorworks-mcp"
    return Path.home() / ".vectorworks-mcp"


def _default_auth_token_file() -> Path:
    configured = os.environ.get("VW_MCP_AUTH_TOKEN_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _default_state_dir() / DEFAULT_AUTH_TOKEN_FILENAME


def _read_auth_token_file() -> str:
    try:
        path = _default_auth_token_file()
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _load_auth_token() -> str:
    if _truthy_env("VW_MCP_INSECURE_NO_AUTH"):
        return ""
    token = os.environ.get("VW_MCP_AUTH_TOKEN", "").strip()
    if token:
        return token
    return _read_auth_token_file()


def _auth_configuration_error() -> Optional[str]:
    if AUTH_TOKEN or ALLOW_INSECURE_NO_AUTH:
        return None
    return (
        "VW_MCP_AUTH_TOKEN is required for the local Vectorworks protocol. "
        "Run scripts\\run-mcp-server.ps1 or scripts\\register-claude-code.ps1 to generate "
        f"{_default_auth_token_file()}, or set VW_MCP_INSECURE_NO_AUTH=1 only for local diagnostics."
    )


def _load_config() -> tuple[str, int, float, float, int, int]:
    host = os.environ.get("VW_MCP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    host = _validate_loopback_host(host)
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
AUTH_TOKEN = _load_auth_token()
ALLOW_INSECURE_NO_AUTH = _truthy_env("VW_MCP_INSECURE_NO_AUTH")
ENABLE_RUN_SCRIPT = _truthy_env("VW_MCP_ENABLE_RUN_SCRIPT")
TRACE_ENABLED = _truthy_env("VW_MCP_TRACE")
_EXACT_NAME_CRITERIA_RE = re.compile(r"^\(\(N='([^']{1,255})'\)\)$")
_SIMPLE_FIND_CRITERIA_RE = re.compile(
    r"^(?P<key>[NTC])\s*=\s*(?:'(?P<quoted>[^']{1,255})'|(?P<bare>[A-Za-z0-9_. -]{1,255}))$",
    re.IGNORECASE,
)


def _is_exact_name_criteria(criteria: str) -> bool:
    return bool(_EXACT_NAME_CRITERIA_RE.fullmatch(str(criteria or "").strip()))


def _exact_name_from_criteria(criteria: str) -> Optional[str]:
    match = _EXACT_NAME_CRITERIA_RE.fullmatch(str(criteria or "").strip())
    if not match:
        return None
    return match.group(1)


def _unwrap_simple_criteria(criteria: str) -> str:
    text = str(criteria or "").strip()
    for _ in range(2):
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1].strip()
    return text


def _parse_simple_find_criteria(criteria: str) -> Optional[tuple[str, str]]:
    text = str(criteria or "").strip()
    if text.upper() == "ALL":
        return ("all", "")

    exact_name = _exact_name_from_criteria(text)
    if exact_name is not None:
        return ("name", exact_name)

    match = _SIMPLE_FIND_CRITERIA_RE.fullmatch(_unwrap_simple_criteria(text))
    if not match:
        return None

    key = match.group("key").upper()
    value = (match.group("quoted") or match.group("bare") or "").strip()
    if not value:
        return None
    if key == "N":
        return ("name", value)
    if key == "T":
        return ("type", value.lower())
    if key == "C":
        return ("class", value)
    return None


mcp = (
    FastMCP(
        "Vectorworks 2024/2025",
        instructions=MCP_SERVER_INSTRUCTIONS,
        version=CONNECTOR_VERSION,
    )
    if FastMCP is not None
    else _MissingFastMCP(
        "Vectorworks 2024/2025",
        instructions=MCP_SERVER_INSTRUCTIONS,
        version=CONNECTOR_VERSION,
    )
)

# Persistent connection, guarded by a lock so concurrent MCP tool calls do not
# interleave frames on the same socket.
_sock: Optional[socket.socket] = None
_lock = threading.Lock()
_cad_safe_cache_lock = threading.Lock()
_cad_safe_cache: Optional[tuple[float, dict[str, Any]]] = None
_operation_idempotency_lock = threading.Lock()
_operation_idempotency_cache: dict[str, str] = {}
_MAX_OPERATION_IDEMPOTENCY_ENTRIES = 256
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NATIVE_TIMING_KEYS = {
    "queue_wait_ms",
    "handler_ms",
    "native_total_ms",
    "total_native_ms",
    "transport_ms",
    "serialize_ms",
    "pump_interval_ms",
}


ObjectType = Literal["rect", "rectangle", "box", "circle", "oval", "line", "arc", "polygon", "polyline"]
BatchObjectType = Literal[
    "rect",
    "rectangle",
    "box",
    "circle",
    "oval",
    "line",
    "arc",
    "polygon",
    "polyline",
    "wall",
    "text",
    "dimension",
    "linear_dimension",
    "slab",
    "roof",
    "space",
    "door",
    "window",
]
DoorSwing = Literal["left", "right"]
PropertyName = Literal["name", "class", "fillColor", "penColor", "lineWeight", "opacity"]
PROPERTY_NAME_VALUES = {"name", "class", "fillColor", "penColor", "lineWeight", "opacity"}
MAX_PROPERTY_VALUE_CHARS = 1024
ClassAction = Literal["list", "create", "delete"]
WorksheetAction = Literal["list", "read", "write", "read_range"]
SymbolAction = Literal["list", "insert"]
ExportFormat = Literal["pdf", "dxf", "dwg", "image"]
ImportFormat = Literal["auto", "dxf", "dwg", "png", "jpg", "jpeg", "tif", "tiff", "bmp"]
FastNativeSelectionAction = Literal["get", "select", "clear", "delete"]
CompatSelectionAction = Literal["get", "select", "clear", "delete", "move", "duplicate"]
SelectionAction = (
    CompatSelectionAction
    if os.environ.get("VW_MCP_TOOL_PROFILE", "fast-native").strip().lower() == "compat"
    else FastNativeSelectionAction
)
AgentContextProfile = Literal["brief", "production", "full"]
GroupedStatusAction = Literal["health", "context"]
GroupedReadAction = Literal["document", "layers", "summary", "query", "selection"]
GroupedCatalogAction = Literal[
    "capabilities",
    "classes",
    "symbols",
    "parametric_schemas",
    "worksheets",
    "resources",
]
GroupedIOAction = Literal["import", "export", "capture"]
GroupedIOFormat = Literal["auto", "dwg", "pdf", "image", "png", "jpg", "jpeg", "tif", "tiff", "vwx"]
GroupedViewAction = Literal["get", "set", "capture"]
GroupedDocumentAction = Literal["info", "save", "export", "open", "new"]
LookupDetail = Literal["brief", "normal", "full"]
MAX_OBJECT_QUERY_LIMIT = 1000
ObjectQueryLimit = Annotated[int, Field(ge=1, le=MAX_OBJECT_QUERY_LIMIT)]
GroupedPageLimit = Annotated[int, Field(ge=1, le=200)]
GroupedCursor = Annotated[str, Field(max_length=10, pattern=r"^(?:0|[1-9][0-9]{0,9})?$")]
SummaryExampleLimit = Annotated[int, Field(ge=0, le=100)]
SummaryScanLimit = Annotated[int, Field(ge=1, le=100_000)]
ObjectFieldList = Annotated[list[str], Field(max_length=20)]
BatchPropertyEditList = Annotated[
    list[dict[str, Any]],
    Field(
        min_length=1,
        max_length=100,
        description=(
            "Property edits of the form "
            '{"ref":"uuid:...|name:...|handle:...","properties":{"name":"..."}}. '
            "Supported properties: name, class, fillColor, penColor, lineWeight, opacity."
        ),
        json_schema_extra={
            "items": {
                "type": "object",
                "required": ["ref", "properties"],
                "additionalProperties": False,
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Object reference: uuid:..., name:..., or handle:...",
                    },
                    "expected_type": {"type": "string"},
                    "expected_layer": {"type": "string"},
                    "expected_name": {"type": "string"},
                    "properties": {
                        "type": "object",
                        "minProperties": 1,
                        "maxProperties": 20,
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string", "maxLength": MAX_PROPERTY_VALUE_CHARS},
                            "class": {"type": "string", "minLength": 1, "maxLength": MAX_PROPERTY_VALUE_CHARS},
                            "fillColor": {"type": "string", "pattern": r"^\d{1,5},\d{1,5},\d{1,5}$"},
                            "penColor": {"type": "string", "pattern": r"^\d{1,5},\d{1,5},\d{1,5}$"},
                            "lineWeight": {"type": "integer", "minimum": 0, "maximum": 32767},
                            "opacity": {"type": "integer", "minimum": 0, "maximum": 100},
                        },
                    },
                },
            }
        },
    ),
]
WorksheetRow = Annotated[int, Field(ge=1, le=1_048_576)]
WorksheetColumn = Annotated[int, Field(ge=1, le=16_384)]
WorksheetRowCount = Annotated[int, Field(ge=1, le=500)]
NonEmptyPath = Annotated[str, Field(min_length=1)]
PositiveLength = Annotated[float, Field(gt=0)]
Point2D = Annotated[list[float], Field(min_length=2, max_length=2)]
PointList = Annotated[list[Point2D], Field(max_length=1000)]
PolygonPointList = Annotated[list[Point2D], Field(min_length=3, max_length=1000)]
PrimitiveObjectList = Annotated[list[dict[str, Any]], Field(min_length=1, max_length=250)]
ExecuteOperationList = Annotated[
    list[dict[str, Any]],
    Field(
        min_length=1,
        max_length=250,
        description=(
            "One atomic plan: create, set_properties, transform, duplicate, or delete. "
            "Targets are explicit uuid:/name:/handle: refs or $operation_id refs."
        ),
        json_schema_extra={
            "items": {
                "type": "object",
                "required": ["type", "params"],
                "additionalProperties": False,
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["create", "set_properties", "transform", "duplicate", "delete"],
                    },
                    "operation_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "params": {
                        "type": "object",
                        "description": "Parameters accepted by the corresponding typed operation.",
                    },
                },
            }
        },
    ),
]
IdempotencyKey = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
        description="Stable caller-generated key reused only for the identical operation plan.",
    ),
]
FloorPlanRoomList = Annotated[list[dict[str, Any]], Field(min_length=1, max_length=100)]
OptionalFloorPlanRoomList = Annotated[list[dict[str, Any]], Field(max_length=100)]
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
    "vw_agent_context": {
        "category": "metadata",
        "wire_action": None,
        "composes_actions": ["ping", "get_document_info", "get_layers", "get_objects"],
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
        "wire_action": "drawing_summary",
        "composes_actions": ["drawing_summary", "get_document_info", "get_layers", "get_objects"],
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_find_objects": {
        "category": "document-read",
        "wire_action": "find_objects",
        "composes_actions": ["get_objects"],
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_inspect_object": {
        "category": "document-write",
        "wire_action": "inspect_object",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
        "writesDocument": True,
        "confirmationRequired": True,
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
        "wire_action": "batch_create_objects",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_execute_operations": {
        "category": "document-write",
        "wire_action": "apply_operations",
        "composes_actions": ["apply_operations"],
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": True,
        "writesDocument": True,
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
    "vw_batch_set_object_properties": {
        "category": "document-write",
        "wire_action": None,
        "composes_actions": ["get_objects", "set_property"],
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
    "vw_lookup_objects": {
        "category": "document-read",
        "wire_action": None,
        "composes_actions": ["get_objects"],
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_create_text": {
        "category": "document-write",
        "wire_action": "create_text",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_create_linear_dimension": {
        "category": "document-write",
        "wire_action": "create_linear_dimension",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_create_bim_floor_plan": {
        "category": "bim-floor-plan",
        "wire_action": None,
        "composes_actions": ["batch_create_objects"],
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
    "vw_status": {
        "category": "grouped-status",
        "wire_action": None,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_read": {
        "category": "grouped-read",
        "wire_action": None,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_catalog": {
        "category": "grouped-catalog",
        "wire_action": None,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_apply": {
        "category": "grouped-atomic-write",
        "wire_action": None,
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
        "requires_cad_preflight": True,
        "writesDocument": True,
        "retryPolicy": "same_idempotency_key",
        "unknownCommitState": "query_or_inspect_before_retry",
    },
    "vw_io": {
        "category": "grouped-native-io",
        "wire_action": None,
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_view": {
        "category": "grouped-native-view",
        "wire_action": None,
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
    "vw_document": {
        "category": "grouped-native-document",
        "wire_action": None,
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
        "requires_cad_preflight": True,
    },
}

_GROUPED_VARIANT_SAFETY: dict[str, dict[str, dict[str, Any]]] = {
    "vw_status": {
        action: {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "writesDocument": False,
            "writesFiles": False,
            "confirmationRequired": False,
            "retryPolicy": "safe",
            "unknownCommitState": "not_applicable",
        }
        for action in ("health", "context")
    },
    "vw_read": {
        action: {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "writesDocument": False,
            "writesFiles": False,
            "confirmationRequired": False,
            "retryPolicy": "safe",
            "unknownCommitState": "not_applicable",
        }
        for action in ("document", "layers", "summary", "query", "selection")
    },
    "vw_catalog": {
        action: {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "writesDocument": False,
            "writesFiles": False,
            "confirmationRequired": False,
            "retryPolicy": "safe",
            "unknownCommitState": "not_applicable",
        }
        for action in (
            "capabilities",
            "classes",
            "symbols",
            "parametric_schemas",
            "worksheets",
            "resources",
        )
    },
    "vw_io": {
        "import": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "writesDocument": True,
            "writesFiles": False,
            "confirmationRequired": False,
            "retryPolicy": "never_after_send",
            "unknownCommitState": "possible",
        },
        "export": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "writesDocument": False,
            "writesFiles": True,
            "confirmationRequired": False,
            "retryPolicy": "never_after_send",
            "unknownCommitState": "possible",
        },
        "capture": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "writesDocument": False,
            "writesFiles": True,
            "confirmationRequired": False,
            "retryPolicy": "never_after_send",
            "unknownCommitState": "possible",
        },
    },
    "vw_view": {
        "get": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "writesDocument": False,
            "writesFiles": False,
            "confirmationRequired": False,
            "retryPolicy": "safe",
            "unknownCommitState": "not_applicable",
        },
        "set": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "writesDocument": False,
            "writesFiles": False,
            "writesViewState": True,
            "confirmationRequired": False,
            "retryPolicy": "never_after_send",
            "unknownCommitState": "possible",
        },
        "capture": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "writesDocument": False,
            "writesFiles": True,
            "confirmationRequired": False,
            "retryPolicy": "never_after_send",
            "unknownCommitState": "possible",
        },
    },
    "vw_document": {
        "info": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "writesDocument": False,
            "writesFiles": False,
            "confirmationRequired": False,
            "retryPolicy": "safe",
            "unknownCommitState": "not_applicable",
        },
        "save": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "writesDocument": False,
            "writesFiles": True,
            "confirmationRequired": False,
            "retryPolicy": "never_after_send",
            "unknownCommitState": "possible",
        },
        "export": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "writesDocument": False,
            "writesFiles": True,
            "confirmationRequired": False,
            "retryPolicy": "never_after_send",
            "unknownCommitState": "possible",
        },
        "open": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "writesDocument": True,
            "writesFiles": False,
            "confirmationRequired": True,
            "retryPolicy": "never_after_send",
            "unknownCommitState": "possible",
        },
        "new": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "writesDocument": True,
            "writesFiles": False,
            "confirmationRequired": True,
            "retryPolicy": "never_after_send",
            "unknownCommitState": "possible",
        },
    },
}

for _grouped_tool_name, _grouped_actions in _GROUPED_VARIANT_SAFETY.items():
    TOOL_SAFETY[_grouped_tool_name]["action_param"] = "action"
    TOOL_SAFETY[_grouped_tool_name]["actions"] = _grouped_actions

TOOL_SAFETY["vw_execute_operations"].update(
    {
        "retryPolicy": "same_idempotency_key",
        "unknownCommitState": "query_or_inspect_before_retry",
    }
)

FAST_NATIVE_TOOL_NAMES = frozenset(
    {
        "vw_apply",
        "vw_catalog",
        "vw_document",
        "vw_execute_operations",
        "vw_io",
        "vw_read",
        "vw_status",
        "vw_tool_safety",
        "vw_view",
    }
)
_SUPPORTED_TOOL_PROFILES = frozenset({"compat", "fast-native"})
_tool_profile_applied = False


def _configured_tool_profile() -> str:
    return (os.environ.get("VW_MCP_TOOL_PROFILE", "fast-native").strip().lower() or "fast-native")


def _visible_tool_names() -> set[str]:
    return set(TOOL_SAFETY if _configured_tool_profile() == "compat" else FAST_NATIVE_TOOL_NAMES)


def _visible_tool_safety_entry(tool_name: str) -> dict[str, Any]:
    metadata = TOOL_SAFETY[tool_name]
    if _configured_tool_profile() != "compat" and tool_name == "vw_selection":
        visible = dict(metadata)
        actions = metadata.get("actions", {})
        visible["actions"] = {
            action: actions[action]
            for action in ("get", "select", "clear", "delete")
            if action in actions
        }
        return visible
    return metadata


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
    def register(func):
        @functools.wraps(func)
        def mcp_adapter(*args, **kwargs):
            raw = func(*args, **kwargs)
            decoded = _decode_tool_result(raw)
            structured = decoded if isinstance(decoded, dict) else {"result": decoded}
            if ToolResult is None:
                return raw
            return ToolResult(
                content=raw,
                structured_content=structured,
                is_error=_tool_result_failed(raw, decoded),
            )

        mcp.tool(
            name=tool_name,
            output_schema=MCP_TOOL_OUTPUT_SCHEMA,
            annotations=_annotations_for(tool_name),
        )(mcp_adapter)
        return func

    return register


def _new_request_trace(tool: str, action: str) -> dict[str, Any]:
    return {
        "trace_id": uuid.uuid4().hex[:16],
        "tool": tool,
        "action": action,
        "started": time.perf_counter(),
        "preflight_ms": 0.0,
        "wire_ms": 0.0,
        "health_wire_ms": 0.0,
        "request_ids": [],
        "attempts": 0,
        "preflight_cache_hit": False,
    }


def _safe_native_timing_meta(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    timings = value.get("timings", value)
    if not isinstance(timings, dict):
        return {}
    safe: dict[str, float] = {}
    for key in _NATIVE_TIMING_KEYS:
        raw = timings.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(float(raw)):
            safe[key] = round(float(raw), 3)
    return safe


def _finish_request_trace(trace: dict[str, Any], outcome: str) -> dict[str, Any]:
    total_ms = max(0.0, (time.perf_counter() - float(trace.get("started", time.perf_counter()))) * 1000.0)
    request_ids = trace.get("request_ids")
    payload: dict[str, Any] = {
        "trace_id": str(trace.get("trace_id", "")),
        "request_id": request_ids[-1] if isinstance(request_ids, list) and request_ids else "",
        "total_ms": round(total_ms, 3),
        "preflight_ms": round(float(trace.get("preflight_ms", 0.0)), 3),
        "wire_ms": round(float(trace.get("wire_ms", 0.0)), 3),
        "health_wire_ms": round(float(trace.get("health_wire_ms", 0.0)), 3),
        "attempts": int(trace.get("attempts", 0)),
        "preflight_cache_hit": bool(trace.get("preflight_cache_hit", False)),
        "outcome": outcome,
    }
    native = _safe_native_timing_meta(trace.get("native"))
    if native:
        payload["native"] = native
    return payload


def _emit_request_trace(trace: dict[str, Any], timing: dict[str, Any]) -> None:
    if not TRACE_ENABLED:
        return
    event = {
        "schema_version": 1,
        "event": "vectorworks_mcp_request",
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "trace_id": timing.get("trace_id"),
        "request_id": timing.get("request_id"),
        "component": "mcp_host",
        "tool": trace.get("tool"),
        "action": trace.get("action"),
        "outcome": timing.get("outcome"),
        "total_ms": timing.get("total_ms"),
        "preflight_ms": timing.get("preflight_ms"),
        "wire_ms": timing.get("wire_ms"),
        "health_wire_ms": timing.get("health_wire_ms"),
        "attempts": timing.get("attempts"),
        "preflight_cache_hit": timing.get("preflight_cache_hit"),
    }
    if "native" in timing:
        event["native"] = timing["native"]
    print(json.dumps(event, ensure_ascii=True, separators=(",", ":"), sort_keys=True), file=sys.stderr, flush=True)


def _clear_operation_idempotency_cache() -> None:
    with _operation_idempotency_lock:
        _operation_idempotency_cache.clear()


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
    if _configured_tool_profile() != "compat":
        return (
            f"Connection error: {error}. Could not reach the Vectorworks native SDK bridge on {HOST}:{PORT}. "
            "The fast-native profile does not use vw_listener.py or vw_load_listener_2024.py. "
            "Close any VW MCP Listener Python dialog, then run "
            "py -3 .\\plugins\\vectorworks\\bin\\vectorworksctl doctor --repo-path . --json "
            "from the connector checkout. Ensure the compiled native plug-in is installed and loaded, "
            "restart Vectorworks if required, and require dispatch_mode=native_sdk, native_phase>=4, "
            "cad_api_safe=true, transport_only=false, and main_context_pump_ready=true before CAD work. "
            "If a stale listener still owns the port, create "
            "C:\\Users\\<you>\\.vectorworks-mcp\\STOP and restart Vectorworks."
        )
    return (
        f"Connection error: {error}. Could not reach the Vectorworks MCP listener on {HOST}:{PORT}. "
        "This compatibility path is available only after explicit Python dialog fallback opt-in and "
        "blocks parallel manual Vectorworks use. Start Vectorworks, run the generated "
        "vw_load_listener_2024.py from Resource Manager or the installed VW MCP Listener menu command, "
        "and verify VW_MCP_HOST/VW_MCP_PORT match on both sides. If the port is open but requests time out, run "
        "scripts\\test-vectorworks-listener.ps1 or scripts\\doctor-vectorworks-mcp.ps1, create "
        "C:\\Users\\<you>\\.vectorworks-mcp\\STOP, and restart Vectorworks if the stale listener "
        "does not recover."
    )


def _listener_failure_message(action: str, listener_error: str) -> str:
    message = f"VW Error ({action}): {listener_error}"
    if "vw_mcp_auth_token is required" not in listener_error.lower():
        return message
    if _configured_tool_profile() != "compat":
        return (
            message
            + " The process on the MCP port started without the shared authentication configuration. "
            "Do not disable authentication or run vw_listener.py/vw_load_listener_2024.py. Close any "
            "Python listener dialog, rerun install.ps1 to repair the token file and client registration, "
            "restart Vectorworks, then require a phase-4 native result from vectorworksctl doctor."
        )
    return (
        message
        + " The explicitly enabled modal fallback was started without its generated token-file environment. "
        "Regenerate vw_load_listener_2024.py with -EnablePythonDialogFallback and run only that loader; "
        "the fallback dialog blocks parallel manual Vectorworks use."
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


def _fast_native_readiness_errors(status: dict[str, Any]) -> list[str]:
    errors = _native_readiness_errors(status)
    if status.get("native_bridge") is not True:
        errors.append("native_bridge is not true")
    if str(status.get("dispatch_mode", "") or "").lower() != "native_sdk":
        errors.append("dispatch_mode is not native_sdk")
    native_phase = status.get("native_phase")
    if not isinstance(native_phase, int) or isinstance(native_phase, bool) or native_phase < 4:
        errors.append("native_phase is not >= 4")
    capability_revision = status.get("capability_revision")
    if (
        not isinstance(capability_revision, int)
        or isinstance(capability_revision, bool)
        or capability_revision < MIN_FAST_NATIVE_CAPABILITY_REVISION
    ):
        errors.append(
            "capability_revision is not >= {0}".format(MIN_FAST_NATIVE_CAPABILITY_REVISION)
        )
    capability_fingerprint = status.get("capability_fingerprint")
    if not isinstance(capability_fingerprint, str) or not capability_fingerprint.strip():
        errors.append("capability_fingerprint is missing")
    implemented_actions = status.get("implemented_actions")
    if not isinstance(implemented_actions, list) or "apply_operations" not in implemented_actions:
        errors.append("implemented_actions missing phase-4 action: apply_operations")
    return list(dict.fromkeys(errors))


def _native_phase(status: dict[str, Any]) -> int:
    native_phase = status.get("native_phase")
    if isinstance(native_phase, int) and not isinstance(native_phase, bool):
        return native_phase
    return 0


def _native_create_object_types(status: dict[str, Any]) -> set[str]:
    advertised = status.get("create_object_types")
    if isinstance(advertised, list) and all(isinstance(value, str) for value in advertised):
        return {value.strip().lower() for value in advertised if value.strip()}
    return set()


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
        if object_type and object_type not in _native_create_object_types(status):
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
        if _configured_tool_profile() == "compat":
            next_action = (
                "Do not call CAD handlers. After explicit Python dialog fallback opt-in, regenerate and run "
                "vw_load_listener_2024.py. This modal fallback blocks parallel manual Vectorworks use."
            )
        else:
            next_action = (
                "Do not call CAD handlers. The fast-native profile rejects this legacy Python listener. "
                "Close its dialog, run vectorworksctl doctor, and load a compiled phase-4 native SDK bridge. "
                "Do not run vw_listener.py or vw_load_listener_2024.py."
            )
        return _with_block_context(
            {
                "ok": False,
                "cad_api_safe": False,
                "bridge_kind": status.get("bridge_kind", "unknown"),
                "dispatch_mode": status.get("dispatch_mode", "unknown"),
                "transport_only": bool(status.get("transport_only")),
                "native_bridge": bool(status.get("native_bridge")),
                "reason": "foreground_diagnostic_bridge",
                "next_action": next_action,
                "raw_status": status,
            },
            blocked_action,
        )

    fast_native = _configured_tool_profile() != "compat"
    native_errors = _fast_native_readiness_errors(status) if fast_native else _native_readiness_errors(status)
    if native_errors:
        return _with_block_context(
            {
                "ok": False,
                "cad_api_safe": False,
                "bridge_kind": status.get("bridge_kind", "unknown"),
                "dispatch_mode": status.get("dispatch_mode", "unknown"),
                "transport_only": bool(status.get("transport_only")),
                "native_bridge": bool(status.get("native_bridge")),
                "handlers": status.get("handlers"),
                "version": status.get("version"),
                "main_context_pump": status.get("main_context_pump"),
                "main_context_pump_ready": status.get("main_context_pump_ready"),
                "reason": "fast_native_bridge_not_ready" if fast_native else "native_bridge_not_phase1_ready",
                "next_action": (
                    "Do not call CAD handlers or switch runtimes. Run vectorworksctl doctor and upgrade/restart "
                    "the compiled phase-4 native SDK bridge; do not run vw_listener.py or vw_load_listener_2024.py."
                    if fast_native
                    else "Do not call CAD handlers. Run scripts\\smoke-native-bridge.ps1 -Json and fix native bridge capabilities."
                ),
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
                "next_action": (
                    "Do not dispatch this CAD action or switch runtimes. "
                    "Upgrade/restart the phase-4 native bridge and retry only after "
                    "the required action appears in implemented_actions."
                ),
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


def _cad_preflight_block(
    action: str,
    params: Optional[dict[str, Any]] = None,
    trace: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    started = time.perf_counter()
    try:
        cached_status = _cached_cad_safe_status()
        if cached_status is not None:
            if trace is not None:
                trace["preflight_cache_hit"] = True
            payload = _evaluate_cad_preflight_status(cached_status, blocked_action=action, blocked_params=params)
            if payload["ok"]:
                return None
            return json.dumps(payload, indent=2, sort_keys=True)

        response = _request_once_health("ping", None, trace)
        if response.get("success") is not True:
            payload = _cad_preflight_ping_error_payload(response, blocked_action=action)
            return json.dumps(payload, indent=2, sort_keys=True)

        status = response.get("result")
        payload = _evaluate_cad_preflight_status(status, blocked_action=action, blocked_params=params)
        if payload["ok"] and isinstance(status, dict):
            _remember_cad_safe_status(status)
            return None
        return json.dumps(payload, indent=2, sort_keys=True)
    finally:
        if trace is not None:
            trace["preflight_ms"] = (
                float(trace.get("preflight_ms", 0.0)) + (time.perf_counter() - started) * 1000.0
            )


def _request_once(
    action: str,
    params: Optional[dict[str, Any]],
    trace: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    request_id = uuid.uuid4().hex[:8]
    if trace is not None:
        trace.setdefault("request_ids", []).append(request_id)
        trace["attempts"] = int(trace.get("attempts", 0)) + 1
    request = {"id": request_id, "action": action, "params": params or {}}
    if AUTH_TOKEN:
        request["auth_token"] = AUTH_TOKEN
    wire_started = time.perf_counter()
    try:
        _connect()
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
    finally:
        if trace is not None:
            trace["wire_ms"] = float(trace.get("wire_ms", 0.0)) + (time.perf_counter() - wire_started) * 1000.0
    _validate_response_envelope(response, request_id, action)
    if trace is not None and isinstance(response.get("meta"), dict):
        trace["native"] = response["meta"]
    return response


def _request_once_health(
    action: str,
    params: Optional[dict[str, Any]],
    trace: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    request_id = uuid.uuid4().hex[:8]
    if trace is not None:
        trace.setdefault("request_ids", []).append(request_id)
    request = {"id": request_id, "action": action, "params": params or {}}
    if AUTH_TOKEN:
        request["auth_token"] = AUTH_TOKEN
    try:
        payload = _json_bytes(request)
    except ProtocolError as exc:
        raise RequestNotSentError(action, exc) from exc

    wire_started = time.perf_counter()
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
    finally:
        if trace is not None:
            trace["health_wire_ms"] = (
                float(trace.get("health_wire_ms", 0.0)) + (time.perf_counter() - wire_started) * 1000.0
            )
    _validate_response_envelope(response, request_id, action)
    return response


def _send_health(
    action: str = "ping",
    params: Optional[dict[str, Any]] = None,
    trace: Optional[dict[str, Any]] = None,
) -> str:
    if _CONFIG_ERROR:
        return f"Configuration error: {_CONFIG_ERROR}"
    auth_error = _auth_configuration_error()
    if auth_error:
        return f"Configuration error: {auth_error}"
    try:
        response = _request_once_health(action, params, trace)
        if response.get("success") is True:
            return _format_result(response.get("result", "OK"))
        return _listener_failure_message(action, str(response.get("error", "Unknown listener error")))
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


def _send(
    action: str,
    params: Optional[dict[str, Any]] = None,
    require_cad_safe: bool = False,
    trace: Optional[dict[str, Any]] = None,
) -> str:
    if _CONFIG_ERROR:
        return f"Configuration error: {_CONFIG_ERROR}"
    auth_error = _auth_configuration_error()
    if auth_error:
        return f"Configuration error: {auth_error}"

    with _lock:
        for attempt in (0, 1):
            try:
                if require_cad_safe:
                    try:
                        blocked = _cad_preflight_block(action, params, trace)
                    except ProtocolError as exc:
                        _close()
                        return f"Protocol error: {exc}. Restart the Vectorworks listener if this persists."
                    if blocked:
                        return blocked
                response = _request_once(action, params, trace)
                if response.get("success") is True:
                    return _format_result(response.get("result", "OK"))
                listener_error = str(response.get("error", "Unknown listener error"))
                if "unknown_commit_state" in listener_error.lower():
                    return _unknown_commit_state_help(action, RuntimeError(listener_error))
                return _listener_failure_message(action, listener_error)
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


def _send_tool(
    tool_name: str,
    params: Optional[dict[str, Any]] = None,
    trace: Optional[dict[str, Any]] = None,
) -> str:
    safety = TOOL_SAFETY[tool_name]
    action = safety.get("wire_action")
    if not isinstance(action, str) or not action:
        return f"Configuration error: {tool_name} does not declare a wire_action"
    owns_trace = trace is None
    request_trace = trace or _new_request_trace(tool_name, action)
    result = _send(
        action,
        params,
        require_cad_safe=bool(safety["requires_cad_preflight"]),
        trace=request_trace,
    )
    if owns_trace:
        decoded = _decode_tool_result(result)
        if isinstance(decoded, dict) and isinstance(decoded.get("timing"), dict):
            request_trace["native"] = decoded["timing"]
        outcome = "error" if _tool_result_failed(result, decoded) else "ok"
        timing = _finish_request_trace(request_trace, outcome)
        _emit_request_trace(request_trace, timing)
    return result


@_tool("vw_tool_safety")
def vw_tool_safety() -> str:
    """Return structured safety metadata for tools visible in the active profile."""
    visible_tools = _visible_tool_names()
    return json.dumps(
        {name: _visible_tool_safety_entry(name) for name in sorted(visible_tools)},
        indent=2,
        sort_keys=True,
    )


@_tool("vw_capabilities")
def vw_capabilities(include_tools: bool = True) -> str:
    """Return current bridge capabilities and the MCP tool surface agents can safely plan against."""
    cached_status = _cached_cad_safe_status()
    if cached_status is not None:
        decoded_status = cached_status
        status_ok = True
    else:
        raw_status = _send_health("ping")
        decoded_status = _decode_tool_result(raw_status)
        status_ok = not _tool_result_failed(raw_status, decoded_status)
    native_ready = (
        isinstance(decoded_status, dict)
        and _evaluate_cad_preflight_status(decoded_status).get("ok") is True
    )
    implemented_actions = (
        set(decoded_status.get("implemented_actions") or [])
        if isinstance(decoded_status, dict) and isinstance(decoded_status.get("implemented_actions"), list)
        else set()
    )
    bridge_is_native = bool(decoded_status.get("native_bridge")) if isinstance(decoded_status, dict) else False
    property_editing_available = native_ready and (not bridge_is_native or "set_property" in implemented_actions)
    class_management_available = native_ready and (not bridge_is_native or "manage_classes" in implemented_actions)
    phase_two_ready = (
        native_ready
        and _native_phase(decoded_status) >= 2
        and NATIVE_PHASE_TWO_REQUIRED_ACTIONS <= implemented_actions
    )
    tool_profile = _configured_tool_profile()
    fast_native_ready = (
        native_ready
        and _native_phase(decoded_status) >= 4
        and "apply_operations" in implemented_actions
    )
    host_capabilities: dict[str, Any] = {
        "tool_profile": tool_profile,
        "fast_native_required": tool_profile == "fast-native",
        "execute_operations_fast_path": fast_native_ready,
        "native_apply_operations": fast_native_ready,
        "drawing_summary": native_ready and "drawing_summary" in implemented_actions,
        "compact_object_lookup": native_ready,
        "native_class_management": class_management_available,
    }
    if tool_profile == "compat":
        host_capabilities.update(
            {
                "batch_primitive_creation": True,
                "atomic_batch_primitive_creation": native_ready and "batch_create_objects" in implemented_actions,
                "atomic_mixed_production_batch_creation": phase_two_ready,
                "schematic_floor_plan_planning": True,
                "schematic_floor_plan_creation": True,
                "native_wall_creation": phase_two_ready,
                "native_text_creation": phase_two_ready,
                "native_linear_dimension_creation": phase_two_ready,
                "verified_batch_property_editing": property_editing_available,
                "true_bim_objects": phase_two_ready,
            }
        )
    payload: dict[str, Any] = {
        "ok": status_ok and (tool_profile != "fast-native" or fast_native_ready),
        "tool": "vw_capabilities",
        "profile_ready": tool_profile != "fast-native" or fast_native_ready,
        "bridge_status": decoded_status,
        "native_phase_one_required_actions": sorted(NATIVE_PHASE_ONE_REQUIRED_ACTIONS),
        "native_phase_two_required_actions": sorted(NATIVE_PHASE_TWO_REQUIRED_ACTIONS),
        "native_phase_one_create_object_types": sorted(NATIVE_PHASE_ONE_CREATE_OBJECT_TYPES),
        "native_phase_two_create_object_types": sorted(NATIVE_PHASE_TWO_CREATE_OBJECT_TYPES),
        "native_phase_four_create_object_types": sorted(NATIVE_PHASE_FOUR_CREATE_OBJECT_TYPES),
        "native_create_object_types": sorted(
            _native_create_object_types(decoded_status) if isinstance(decoded_status, dict) else set()
        ),
        "native_phase_one_selection_actions": sorted(NATIVE_PHASE_ONE_SELECTION_ACTIONS),
        "host_capabilities": host_capabilities,
        "notes": [
            "Normal agent work requires the fast-native profile and phase-4 apply_operations.",
            "Missing phase-4 support is a hard upgrade/restart failure; no legacy or modal fallback is selected.",
        ],
    }
    if include_tools:
        visible_tools = _visible_tool_names()
        payload["tools"] = sorted(visible_tools)
        payload["tool_safety"] = {
            name: _visible_tool_safety_entry(name)
            for name in sorted(visible_tools)
        }
    return json.dumps(payload, indent=2, sort_keys=True)


@_tool("vw_agent_context")
def vw_agent_context(
    profile: AgentContextProfile = "production",
    limit: ObjectQueryLimit = 1000,
    include_examples: bool = False,
    example_limit: SummaryExampleLimit = 5,
) -> str:
    """Return one compact Codex planning snapshot: preflight, key capabilities, and drawing summary."""
    cached_status = _cached_cad_safe_status()
    if cached_status is not None:
        decoded_status = cached_status
        status_ok = True
    else:
        raw_status = _send_health("ping")
        decoded_status = _decode_tool_result(raw_status)
        status_ok = not _tool_result_failed(raw_status, decoded_status)
    if isinstance(decoded_status, dict):
        preflight = _evaluate_cad_preflight_status(decoded_status)
        if preflight.get("ok"):
            _remember_cad_safe_status(decoded_status)
    else:
        preflight = _cad_preflight_ping_error_payload(decoded_status)
        preflight["reason"] = "ping_failed_or_non_json"

    implemented_actions = (
        set(decoded_status.get("implemented_actions") or [])
        if isinstance(decoded_status, dict) and isinstance(decoded_status.get("implemented_actions"), list)
        else set()
    )
    native_ready = bool(preflight.get("ok"))
    bridge_is_native = bool(decoded_status.get("native_bridge")) if isinstance(decoded_status, dict) else False
    property_editing_available = native_ready and (not bridge_is_native or "set_property" in implemented_actions)
    class_management_available = native_ready and (not bridge_is_native or "manage_classes" in implemented_actions)
    phase_two_ready = (
        native_ready
        and isinstance(decoded_status, dict)
        and _native_phase(decoded_status) >= 2
        and NATIVE_PHASE_TWO_REQUIRED_ACTIONS <= implemented_actions
    )
    tool_profile = _configured_tool_profile()
    fast_native_ready = (
        native_ready
        and isinstance(decoded_status, dict)
        and _native_phase(decoded_status) >= 4
        and "apply_operations" in implemented_actions
    )
    context_capabilities: dict[str, Any] = {
        "tool_profile": tool_profile,
        "fast_native_required": tool_profile == "fast-native",
        "compact_context": True,
        "drawing_summary": native_ready and "drawing_summary" in implemented_actions,
        "drawing_scan_performed": False,
        "execute_operations_fast_path": fast_native_ready,
        "native_apply_operations": fast_native_ready,
        "exact_name_lookup": True,
        "compact_object_lookup": native_ready,
        "native_class_management": class_management_available,
    }
    if tool_profile == "compat":
        context_capabilities.update(
            {
                "verified_batch_property_editing": property_editing_available,
                "batch_primitive_creation": True,
                "atomic_batch_primitive_creation": native_ready and "batch_create_objects" in implemented_actions,
                "atomic_mixed_production_batch_creation": phase_two_ready,
                "native_wall_creation": phase_two_ready,
                "native_text_creation": phase_two_ready,
                "native_linear_dimension_creation": phase_two_ready,
                "true_bim_wall_layouts": phase_two_ready,
                "native_doors_windows_spaces": False,
            }
        )
    bridge: dict[str, Any] = {
        "ok": status_ok,
        "cad_api_safe": preflight.get("cad_api_safe"),
        "transport_only": preflight.get("transport_only"),
        "native_bridge": decoded_status.get("native_bridge") if isinstance(decoded_status, dict) else None,
        "native_phase": _native_phase(decoded_status) if isinstance(decoded_status, dict) else 0,
        "bridge_kind": decoded_status.get("bridge_kind") if isinstance(decoded_status, dict) else None,
        "dispatch_mode": decoded_status.get("dispatch_mode") if isinstance(decoded_status, dict) else None,
        "main_context_pump_ready": (
            decoded_status.get("main_context_pump_ready") if isinstance(decoded_status, dict) else None
        ),
        "implemented_actions": sorted(implemented_actions),
    }

    summary: Any = None
    if preflight.get("ok") and profile != "brief":
        if isinstance(decoded_status, dict):
            _remember_cad_safe_status(decoded_status)
        summary = _decode_tool_result(
            vw_drawing_summary(
                limit=limit,
                include_examples=(include_examples or profile == "full"),
                example_limit=example_limit,
            )
        )
    context_capabilities["drawing_scan_performed"] = summary is not None

    payload: dict[str, Any] = {
        "ok": (
            status_ok
            and bool(preflight.get("ok"))
            and (tool_profile != "fast-native" or fast_native_ready)
        ),
        "tool": "vw_agent_context",
        "profile": profile,
        "profile_ready": tool_profile != "fast-native" or fast_native_ready,
        "bridge": bridge,
        "preflight": preflight,
        "host_capabilities": context_capabilities,
        "drawing_summary": summary,
        "recommended_workflow": [
            "Use vw_lookup_objects only when the requested edit depends on existing drawing state.",
            "Use vw_execute_operations with a stable idempotency key for every bounded write.",
            "If phase-4 apply_operations is missing, stop and upgrade/restart the native bridge; do not switch runtimes or decompose the write.",
        ],
    }
    if profile == "full":
        visible_tools = _visible_tool_names()
        payload["tool_safety"] = {
            name: _visible_tool_safety_entry(name)
            for name in sorted(visible_tools)
        }
        payload["bridge_status"] = decoded_status
    elif profile == "brief":
        payload.pop("recommended_workflow", None)

    return json.dumps(payload, indent=2, sort_keys=True)


@_tool("vw_run_script")
def vw_run_script(code: str, confirm: str = "") -> str:
    """Execute Python inside Vectorworks. The 'vs' module is available.
    Use print() to return output. Escape hatch for anything other tools do not cover.
    Disabled by default; requires VW_MCP_ENABLE_RUN_SCRIPT=1 and confirm='RUN_TRUSTED_CODE'. Example:
    vw_run_script("h = vs.FSActLayer()\\nprint(vs.GetName(h))", confirm="RUN_TRUSTED_CODE")"""
    if not ENABLE_RUN_SCRIPT:
        return json.dumps(
            {
                "ok": False,
                "tool": "vw_run_script",
                "blocked": True,
                "reason": "run_script_disabled",
                "message": "vw_run_script is disabled by default. Set VW_MCP_ENABLE_RUN_SCRIPT=1 only for trusted local debugging.",
                "writes_started": False,
            },
            indent=2,
            sort_keys=True,
        )
    if confirm != "RUN_TRUSTED_CODE":
        return _confirmation_error(
            "vw_run_script",
            "RUN_TRUSTED_CODE",
            "vw_run_script executes trusted code inside Vectorworks and requires explicit confirmation",
        )
    return _send_tool("vw_run_script", {"code": code, "confirm": confirm})


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


def _send_create_normalised_object(params: dict[str, Any]) -> str:
    object_type = str(params.get("object_type", "")).lower()
    if object_type == "wall":
        return _send_tool("vw_create_wall", params)
    if object_type == "text":
        return _send_tool("vw_create_text", params)
    if object_type in ("dimension", "linear_dimension"):
        return _send_tool("vw_create_linear_dimension", params)
    return _send_create_primitive(params)


_PRIMITIVE_COORD_KEYS = ("x1", "y1", "x2", "y2")
_PRIMITIVE_ALLOWED_KEYS = {
    "role",
    "object_type",
    "type",
    "x",
    "y",
    "x1",
    "y1",
    "x2",
    "y2",
    "start_x",
    "start_y",
    "end_x",
    "end_y",
    "radius",
    "start_angle",
    "sweep_angle",
    "points",
    "closed",
    "height",
    "sill_height",
    "thickness",
    "elevation",
    "bearing_height",
    "bearing_inset",
    "vertical_miter",
    "miter_type",
    "generate_gable_walls",
    "slope",
    "overhang",
    "style_name",
    "plugin_name",
    "definition_name",
    "descriptor_fingerprint",
    "wall_uuid",
    "require_wall_host",
    "parameters",
    "elevation",
    "bearing_height",
    "slope",
    "overhang",
    "bearing_inset",
    "vertical_miter",
    "miter_type",
    "generate_gable_walls",
    "room_id",
    "text",
    "width",
    "rotation",
    "text_size",
    "size",
    "fixed_size",
    "wrap",
    "offset",
    "dimension_offset",
    "text_offset",
    "direction_x",
    "direction_y",
    "dimension_type",
    "name",
    "room_id",
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


def _object_refs(obj: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("uuid", "name", "handle"):
        value = str(obj.get(key) or "").strip()
        if value:
            refs.append(f"{key}:{value}")
    return refs


def _compact_object_record(
    obj: Any,
    *,
    detail: str = "brief",
    include_refs: bool = True,
    fields: Optional[list[str]] = None,
) -> Any:
    if not isinstance(obj, dict):
        return obj
    if detail == "full":
        compact = dict(obj)
    else:
        base_fields = (
            ("type", "name", "layer")
            if detail == "brief"
            else ("handle", "uuid", "type", "type_id", "name", "layer", "class", "class_name", "bounds")
        )
        selected = tuple(fields or base_fields)
        compact = {key: obj.get(key) for key in selected if key in obj}

    if include_refs:
        refs = _object_refs(obj)
        compact["refs"] = refs
        compact["ref"] = refs[0] if refs else None
    return compact


def _parse_object_ref(ref: Any) -> tuple[Optional[str], Optional[str], Optional[str]]:
    text = str(ref or "").strip()
    if not text:
        return None, None, "ref is required"
    if ":" not in text:
        return "handle", text, None
    kind, value = text.split(":", 1)
    kind = kind.strip().lower()
    value = value.strip()
    if kind not in {"uuid", "name", "handle"}:
        return None, None, "ref must use uuid:, name:, or handle:"
    if not value:
        return None, None, "ref value is required"
    return kind, value, None


def _object_matches_expectations(
    obj: dict[str, Any],
    *,
    expected_type: str = "",
    expected_layer: str = "",
    expected_name: str = "",
) -> bool:
    if expected_type and str(obj.get("type") or "").lower() != expected_type.lower():
        return False
    if expected_layer and str(obj.get("layer") or "") != expected_layer:
        return False
    if expected_name and str(obj.get("name") or "") != expected_name:
        return False
    return True


def _resolve_object_target(
    ref: str,
    *,
    expected_type: str = "",
    expected_layer: str = "",
    expected_name: str = "",
    lookup_limit: int = MAX_OBJECT_QUERY_LIMIT,
) -> dict[str, Any]:
    ref_kind, ref_value, ref_error = _parse_object_ref(ref)
    if ref_error:
        return {"ok": False, "error": ref_error, "ref": ref}

    raw = _send(
        "get_objects",
        {"layer": expected_layer, "object_type": expected_type, "limit": lookup_limit},
        require_cad_safe=True,
    )
    objects = _decode_tool_result(raw)
    if _tool_result_failed(raw, objects):
        return {"ok": False, "error": "object lookup failed", "ref": ref, "result": objects}
    if not isinstance(objects, list):
        return {
            "ok": False,
            "error": f"get_objects returned {type(objects).__name__}, expected list",
            "ref": ref,
            "result": objects,
        }

    matches: list[dict[str, Any]] = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        if str(obj.get(ref_kind) or "") != ref_value:
            continue
        if not _object_matches_expectations(
            obj,
            expected_type=expected_type,
            expected_layer=expected_layer,
            expected_name=expected_name,
        ):
            continue
        matches.append(obj)

    if len(matches) != 1:
        return {
            "ok": False,
            "error": "object ref did not resolve to exactly one object",
            "ref": ref,
            "match_count": len(matches),
            "possibly_truncated": len(objects) >= lookup_limit,
            "matches": [
                _compact_object_record(obj, detail="normal", include_refs=True)
                for obj in matches[:10]
            ],
        }

    target = matches[0]
    handle = str(target.get("handle") or "").strip()
    if not handle:
        return {
            "ok": False,
            "error": "resolved object has no handle; property writes require a Vectorworks handle",
            "ref": ref,
            "target": _compact_object_record(target, detail="normal", include_refs=True),
        }
    return {
        "ok": True,
        "ref": ref,
        "ref_kind": ref_kind,
        "ref_value": ref_value,
        "target": target,
        "handle": handle,
        "possibly_truncated": len(objects) >= lookup_limit,
    }


def _readback_object_snapshot(target: dict[str, Any], *, lookup_limit: int) -> dict[str, Any]:
    for key in ("uuid", "handle", "name"):
        value = str(target.get(key) or "").strip()
        if value:
            return _resolve_object_target(
                f"{key}:{value}",
                expected_type=str(target.get("type") or ""),
                expected_layer=str(target.get("layer") or ""),
                lookup_limit=lookup_limit,
            )
    return {"ok": False, "error": "resolved object has no uuid, handle, or name for readback"}


def _object_property_value(obj: dict[str, Any], property_name: str) -> Any:
    if property_name == "class":
        return obj.get("class") if "class" in obj else obj.get("class_name")
    return obj.get(property_name)


def _normalize_property_value(property_name: str, value: Any) -> tuple[str | None, str | None]:
    if value is None or isinstance(value, (dict, list, tuple)):
        return None, "property value must be a scalar"

    value_text = str(value)
    if len(value_text) > MAX_PROPERTY_VALUE_CHARS:
        return None, f"property value is limited to {MAX_PROPERTY_VALUE_CHARS} characters"

    if property_name == "class":
        class_name = value_text.strip()
        if not class_name:
            return None, "class value is required"
        return class_name, None

    if property_name in {"lineWeight", "opacity"}:
        trimmed = value_text.strip()
        if not re.fullmatch(r"[+-]?\d+", trimmed):
            return None, f"{property_name} must be an integer"
        parsed = int(trimmed, 10)
        max_value = 32767 if property_name == "lineWeight" else 100
        if parsed < 0 or parsed > max_value:
            return None, f"{property_name} must be between 0 and {max_value}"
        return str(parsed), None

    if property_name in {"fillColor", "penColor"}:
        parts = [part.strip() for part in value_text.split(",")]
        if len(parts) != 3 or any(not re.fullmatch(r"\d+", part) for part in parts):
            return None, "color must be r,g,b with components in 0..65535"
        components = [int(part, 10) for part in parts]
        if any(component < 0 or component > 65535 for component in components):
            return None, "color must be r,g,b with components in 0..65535"
        return ",".join(str(component) for component in components), None

    return value_text, None


def _verify_property_changes(after: Any, properties: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(after, dict):
        return {"verified": False, "failures": ["readback object is not a JSON object"], "checks": []}

    failures: list[str] = []
    checks: list[dict[str, Any]] = []
    for property_name, expected in properties.items():
        actual = _object_property_value(after, property_name)
        present = actual is not None
        matched = present and str(actual) == str(expected)
        if not matched:
            if present:
                failures.append(f"{property_name} readback mismatch: expected {expected!r}, got {actual!r}")
            else:
                failures.append(f"{property_name} was not present in readback object")
        checks.append(
            {
                "property_name": property_name,
                "expected": expected,
                "actual": actual,
                "present": present,
                "matched": matched,
            }
        )
    return {"verified": not failures, "failures": failures, "checks": checks}


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


def _coerce_number_any(
    item: dict[str, Any],
    keys: tuple[str, ...],
    *,
    default: Optional[float] = None,
    required: bool = False,
    min_value: Optional[float] = None,
    label: str = "item",
) -> float:
    for key in keys:
        if key in item and item.get(key) is not None:
            return _coerce_number(item, key, required=True, min_value=min_value, label=label)
    if required:
        raise ValueError(f"{label}.{keys[0]} is required")
    if default is None:
        raise ValueError(f"{label}.{keys[0]} has no default")
    if min_value is not None and default < min_value:
        raise ValueError(f"{label}.{keys[0]} must be >= {min_value}")
    return float(default)


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


def _coerce_positive_number_any(
    item: dict[str, Any],
    keys: tuple[str, ...],
    *,
    default: Optional[float] = None,
    label: str = "item",
) -> float:
    result = _coerce_number_any(item, keys, default=default, required=default is None, min_value=0, label=label)
    if result <= 0:
        raise ValueError(f"{label}.{keys[0]} must be > 0")
    return result


def _coerce_bool(item: dict[str, Any], key: str, default: bool = False, *, label: str = "item") -> bool:
    value = item.get(key, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"{label}.{key} must be a boolean")


def _coerce_int(
    item: dict[str, Any],
    key: str,
    *,
    default: int,
    min_value: int,
    max_value: int,
    label: str = "item",
) -> int:
    value = item.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label}.{key} must be an integer")
    if value < min_value or value > max_value:
        raise ValueError(f"{label}.{key} must be between {min_value} and {max_value}")
    return value


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
    requested_object_type = object_type
    if object_type == "rectangle" or object_type == "box":
        object_type = "rect"
    if object_type == "polyline":
        object_type = "polygon"
    if object_type == "dimension":
        object_type = "linear_dimension"
    if object_type not in NATIVE_PHASE_FOUR_CREATE_OBJECT_TYPES:
        raise ValueError(
            f"{label}.object_type must be one of: {', '.join(sorted(NATIVE_PHASE_FOUR_CREATE_OBJECT_TYPES))}"
        )

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
    elif object_type in {"polygon", "slab", "roof", "space"}:
        raw_points = raw.get("points")
        if not isinstance(raw_points, list) or len(raw_points) > 1000:
            raise ValueError(f"{label}.points must be a list containing at most 1000 [x, y] points")
        closed = _coerce_bool(raw, "closed", requested_object_type != "polyline", label=label)
        if object_type != "polygon" and not closed:
            raise ValueError(f"{label}.{object_type} requires a closed point boundary")
        minimum = 3 if closed else 2
        if len(raw_points) < minimum:
            raise ValueError(f"{label}.points requires at least {minimum} points")
        points: list[list[float]] = []
        for point_index, point in enumerate(raw_points):
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError(f"{label}.points[{point_index}] must be [x, y]")
            try:
                x_value = float(point[0])
                y_value = float(point[1])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{label}.points[{point_index}] must contain finite numbers") from exc
            if not math.isfinite(x_value) or not math.isfinite(y_value):
                raise ValueError(f"{label}.points[{point_index}] must contain finite numbers")
            points.append([x_value, y_value])
        params["points"] = points
        params["closed"] = closed
        if object_type == "slab":
            params["thickness"] = _coerce_positive_number(raw, "thickness", default=200, label=label)
            params["elevation"] = _coerce_number(raw, "elevation", default=0, label=label)
            style_name = _optional_text(raw, "style_name", "")
            if style_name:
                params["style_name"] = style_name
        elif object_type == "roof":
            params["thickness"] = _coerce_positive_number(raw, "thickness", default=200, label=label)
            params["slope"] = _coerce_positive_number(raw, "slope", default=30, label=label)
            if params["slope"] > 89:
                raise ValueError(f"{label}.slope must be <= 89")
            params["overhang"] = _coerce_number(raw, "overhang", default=500, min_value=0, label=label)
            params["elevation"] = _coerce_number_any(
                raw,
                ("elevation", "bearing_height"),
                default=0,
                label=label,
            )
            params["bearing_inset"] = _coerce_number(
                raw,
                "bearing_inset",
                default=0,
                min_value=0,
                label=label,
            )
            params["vertical_miter"] = _coerce_number(
                raw,
                "vertical_miter",
                default=0,
                min_value=0,
                label=label,
            )
            params["miter_type"] = _coerce_int(
                raw,
                "miter_type",
                default=1,
                min_value=1,
                max_value=4,
                label=label,
            )
            params["generate_gable_walls"] = _coerce_bool(
                raw,
                "generate_gable_walls",
                False,
                label=label,
            )
        elif object_type == "space":
            params["height"] = _coerce_positive_number(raw, "height", default=3000, label=label)
            room_id = _optional_text(raw, "room_id", "")
            if room_id:
                params["room_id"] = room_id
    elif object_type == "wall":
        params["start_x"] = _coerce_number_any(raw, ("start_x", "x1"), required=True, label=label)
        params["start_y"] = _coerce_number_any(raw, ("start_y", "y1"), required=True, label=label)
        params["end_x"] = _coerce_number_any(raw, ("end_x", "x2"), required=True, label=label)
        params["end_y"] = _coerce_number_any(raw, ("end_y", "y2"), required=True, label=label)
        if params["start_x"] == params["end_x"] and params["start_y"] == params["end_y"]:
            raise ValueError(f"{label} wall endpoints must not be identical")
        params["height"] = _coerce_positive_number(raw, "height", default=3000, label=label)
        params["thickness"] = _coerce_positive_number(raw, "thickness", default=200, label=label)
        style_name = _optional_text(raw, "style_name", "")
        if style_name:
            params["style_name"] = style_name
    elif object_type in {"parametric", "door", "window"}:
        raw_plugin_name = raw.get("plugin_name")
        raw_descriptor_fingerprint = raw.get("descriptor_fingerprint")
        if not isinstance(raw_plugin_name, str):
            raise ValueError(f"{label}.plugin_name must be a non-empty string")
        if not isinstance(raw_descriptor_fingerprint, str):
            raise ValueError(f"{label}.descriptor_fingerprint must be a non-empty string")
        plugin_name = raw_plugin_name.strip()
        descriptor_fingerprint = raw_descriptor_fingerprint.strip()
        if not plugin_name:
            raise ValueError(f"{label}.plugin_name is required; discover it with vw_catalog parametric_schemas")
        if not descriptor_fingerprint:
            raise ValueError(f"{label}.descriptor_fingerprint is required from live schema discovery")
        if object_type in {"door", "window"}:
            expected_plugin_name = object_type.title()
            if plugin_name != expected_plugin_name:
                raise ValueError(
                    f"{label}.plugin_name must be exactly {expected_plugin_name!r} for {object_type}"
                )
        params["plugin_name"] = plugin_name
        params["descriptor_fingerprint"] = descriptor_fingerprint
        params["x1"] = _coerce_number_any(
            raw,
            ("x", "x1"),
            required=object_type in {"door", "window"},
            default=0,
            label=label,
        )
        params["y1"] = _coerce_number_any(
            raw,
            ("y", "y1"),
            required=object_type in {"door", "window"},
            default=0,
            label=label,
        )
        params["rotation"] = _coerce_number(raw, "rotation", default=0, label=label)
        if object_type in {"door", "window"} and "require_wall_host" in raw:
            if not _coerce_bool(raw, "require_wall_host", True, label=label):
                raise ValueError(f"{label}.require_wall_host cannot be false for {object_type}")
        require_wall_host = (
            True
            if object_type in {"door", "window"}
            else _coerce_bool(raw, "require_wall_host", False, label=label)
        )
        raw_wall_uuid = raw.get("wall_uuid", "")
        if raw_wall_uuid is not None and not isinstance(raw_wall_uuid, str):
            raise ValueError(f"{label}.wall_uuid must be a non-empty string")
        wall_uuid = str(raw_wall_uuid or "").strip()
        if require_wall_host and not wall_uuid:
            raise ValueError(f"{label}.wall_uuid is required for hosted {object_type} placement")
        params["require_wall_host"] = require_wall_host
        if wall_uuid:
            params["wall_uuid"] = wall_uuid
        if object_type in {"door", "window"}:
            params["width"] = _coerce_positive_number(
                raw,
                "width",
                label=label,
            )
            params["height"] = _coerce_positive_number(
                raw,
                "height",
                label=label,
            )
            if object_type == "window":
                params["sill_height"] = _coerce_number(
                    raw,
                    "sill_height",
                    required=True,
                    min_value=0,
                    label=label,
                )
            elif "sill_height" in raw:
                raise ValueError(f"{label}.sill_height is supported only for window")
        raw_parameters = raw.get("parameters", [])
        if not isinstance(raw_parameters, list) or len(raw_parameters) > 256:
            raise ValueError(f"{label}.parameters must be a list with at most 256 entries")
        params["parameter_count"] = len(raw_parameters)
        seen_parameters: set[str] = set()
        for parameter_index, parameter in enumerate(raw_parameters, start=1):
            parameter_label = f"{label}.parameters[{parameter_index}]"
            if not isinstance(parameter, dict):
                raise ValueError(f"{parameter_label} must be an object")
            unknown_parameter_keys = sorted(set(parameter) - {"id", "type", "value"})
            if unknown_parameter_keys:
                raise ValueError(f"{parameter_label} has unsupported key(s): {', '.join(unknown_parameter_keys)}")
            parameter_id = _optional_text(parameter, "id", "").strip()
            parameter_type = _optional_text(parameter, "type", "").strip().lower()
            if not parameter_id or parameter_id in seen_parameters:
                raise ValueError(f"{parameter_label}.id must be a unique universal parameter name")
            semantic_parameter_id = re.sub(r"[^a-z0-9]", "", parameter_id.lower())
            dedicated_semantics = {"width", "height"}
            if object_type == "window":
                dedicated_semantics.update({"elevation", "sillheight"})
            if object_type in {"door", "window"} and semantic_parameter_id in dedicated_semantics:
                raise ValueError(
                    f"{parameter_label}.id duplicates the dedicated {object_type} field {parameter_id!r}"
                )
            if parameter_type not in {"integer", "boolean", "real", "string"}:
                raise ValueError(f"{parameter_label}.type must be integer, boolean, real, or string")
            value = parameter.get("value")
            prefix = f"parameter_{parameter_index}_"
            params[prefix + "name"] = parameter_id
            params[prefix + "type"] = parameter_type
            if parameter_type == "integer":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"{parameter_label}.value must be an integer")
                params[prefix + "integer"] = value
            elif parameter_type == "boolean":
                if not isinstance(value, bool):
                    raise ValueError(f"{parameter_label}.value must be a boolean")
                params[prefix + "boolean"] = value
            elif parameter_type == "real":
                if not _is_real_number(value):
                    raise ValueError(f"{parameter_label}.value must be a finite number")
                params[prefix + "real"] = float(value)
            else:
                if not isinstance(value, str):
                    raise ValueError(f"{parameter_label}.value must be a string")
                params[prefix + "string"] = value
            seen_parameters.add(parameter_id)
    elif object_type == "symbol":
        definition_name = _optional_text(raw, "definition_name", "").strip()
        if not definition_name:
            raise ValueError(f"{label}.definition_name is required; discover it with vw_catalog(action='symbols')")
        params["definition_name"] = definition_name
        params["x1"] = _coerce_number_any(raw, ("x", "x1"), default=0, label=label)
        params["y1"] = _coerce_number_any(raw, ("y", "y1"), default=0, label=label)
        params["rotation"] = _coerce_number(raw, "rotation", default=0, label=label)
    elif object_type == "text":
        text = _optional_text(raw, "text", "")
        if not text:
            raise ValueError(f"{label}.text is required")
        params["text"] = text
        params["x1"] = _coerce_number_any(raw, ("x", "x1"), default=0, label=label)
        params["y1"] = _coerce_number_any(raw, ("y", "y1"), default=0, label=label)
        params["width"] = _coerce_number(raw, "width", default=0, min_value=0, label=label)
        params["rotation"] = _coerce_number(raw, "rotation", default=0, label=label)
        params["text_size"] = _coerce_number_any(raw, ("text_size", "size"), default=0, min_value=0, label=label)
        params["fixed_size"] = _coerce_bool(raw, "fixed_size", False, label=label)
        params["wrap"] = _coerce_bool(raw, "wrap", params["width"] > 0, label=label)
    elif object_type == "linear_dimension":
        params["start_x"] = _coerce_number_any(raw, ("start_x", "x1"), required=True, label=label)
        params["start_y"] = _coerce_number_any(raw, ("start_y", "y1"), required=True, label=label)
        params["end_x"] = _coerce_number_any(raw, ("end_x", "x2"), required=True, label=label)
        params["end_y"] = _coerce_number_any(raw, ("end_y", "y2"), required=True, label=label)
        if params["start_x"] == params["end_x"] and params["start_y"] == params["end_y"]:
            raise ValueError(f"{label} linear_dimension endpoints must not be identical")
        params["offset"] = _coerce_number_any(raw, ("offset", "dimension_offset"), default=300, label=label)
        params["text_offset"] = _coerce_number(raw, "text_offset", default=0, label=label)
        params["direction_x"] = _coerce_number(raw, "direction_x", default=0, label=label)
        params["direction_y"] = _coerce_number(raw, "direction_y", default=0, label=label)
        params["dimension_type"] = _coerce_int(raw, "dimension_type", default=1, min_value=0, max_value=2, label=label)

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


def _native_batch_params(primitives: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    params: dict[str, Any] = {"object_count": len(primitives)}
    roles: list[str] = []
    sent_primitives: list[dict[str, Any]] = []
    for index, primitive in enumerate(primitives, start=1):
        primitive_params = dict(primitive)
        roles.append(str(primitive_params.pop("role", "primitive")))
        sent_primitives.append(primitive_params)
        params[f"object_{index}_json"] = json.dumps(
            primitive_params,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    return params, roles, sent_primitives


def _atomic_batch_support_error(tool: str, object_types: Optional[set[str]] = None) -> Optional[str]:
    raw_status = _send_health("ping")
    decoded_status = _decode_tool_result(raw_status)
    if _tool_result_failed(raw_status, decoded_status) or not isinstance(decoded_status, dict):
        return json.dumps(
            {
                "ok": False,
                "tool": tool,
                "atomic": True,
                "native_batch": False,
                "error": "atomic batch creation requires a native bridge, but bridge status could not be verified",
                "bridge_status": decoded_status,
            },
            indent=2,
            sort_keys=True,
        )
    implemented_actions = decoded_status.get("implemented_actions")
    supports_batch = (
        decoded_status.get("native_bridge") is True
        and decoded_status.get("cad_api_safe") is True
        and isinstance(implemented_actions, list)
        and "batch_create_objects" in implemented_actions
    )
    if supports_batch:
        requested_types = object_types or set()
        unsupported_types = sorted(requested_types - _native_create_object_types(decoded_status))
        if unsupported_types:
            return json.dumps(
                {
                    "ok": False,
                    "tool": tool,
                    "atomic": True,
                    "native_batch": False,
                    "error": "atomic batch includes object types not implemented by this native bridge",
                    "unsupported_object_types": unsupported_types,
                    "next_action": "Install/restart the phase-2 native bridge, or remove unsupported object types from the batch.",
                    "bridge_status": decoded_status,
                },
                indent=2,
                sort_keys=True,
            )
        _remember_cad_safe_status(decoded_status)
        return None
    return json.dumps(
        {
            "ok": False,
            "tool": tool,
            "atomic": True,
            "native_batch": False,
            "error": "atomic batch creation requires the native Vectorworks bridge action 'batch_create_objects'",
            "next_action": "Install/restart the updated native bridge, or call the creation tool with atomic=False for non-atomic typed creation.",
            "bridge_status": decoded_status,
        },
        indent=2,
        sort_keys=True,
    )


def _create_primitives_native_batch(
    tool: str,
    primitives: list[dict[str, Any]],
    metadata: dict[str, Any],
    *,
    schematic: bool = False,
    bim_objects: bool = False,
) -> str:
    support_error = _atomic_batch_support_error(
        tool,
        {str(primitive.get("object_type", "")) for primitive in primitives},
    )
    if support_error is not None:
        return support_error
    params, roles, sent_primitives = _native_batch_params(primitives)
    raw = _send_tool("vw_batch_create_objects", params)
    decoded = _decode_tool_result(raw)
    if _tool_result_failed(raw, decoded):
        return json.dumps(
            {
                "ok": False,
                "tool": tool,
                "schematic": schematic,
                "bim_objects": bim_objects,
                "atomic": True,
                "native_batch": True,
                "attempted_count": len(primitives),
                "created_count": 0,
                "failed_count": len(primitives),
                "result": decoded,
                "warning": "Native atomic batch creation failed; the native bridge rolls back created primitives before returning ordinary handler errors. If transport failed after sending, inspect the document because commit state is unknown.",
                **metadata,
            },
            indent=2,
            sort_keys=True,
        )

    native_created = decoded.get("created", []) if isinstance(decoded, dict) else []
    if not isinstance(native_created, list) or len(native_created) != len(primitives):
        return json.dumps(
            {
                "ok": False,
                "tool": tool,
                "schematic": schematic,
                "bim_objects": bim_objects,
                "atomic": True,
                "native_batch": True,
                "attempted_count": len(primitives),
                "created_count": 0,
                "failed_count": len(primitives),
                "result": decoded,
                "error": "native batch result did not contain one created entry per requested primitive",
                **metadata,
            },
            indent=2,
            sort_keys=True,
        )

    created: list[dict[str, Any]] = []
    for index, native_entry in enumerate(native_created, start=1):
        role = roles[index - 1]
        created.append(
            {
                "index": index,
                "role": role,
                "params": sent_primitives[index - 1],
                "result": native_entry,
            }
        )

    return json.dumps(
        {
            "ok": True,
            "tool": tool,
            "schematic": schematic,
            "bim_objects": bim_objects,
            "atomic": True,
            "native_batch": True,
            "attempted_count": len(created),
            "created_count": len(created),
            "created": created,
            "native_result": decoded,
            **metadata,
        },
        indent=2,
        sort_keys=True,
    )


def _create_primitives_legacy(
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
        raw = _send_create_normalised_object(params)
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


def _create_primitives(
    tool: str,
    primitives: list[dict[str, Any]],
    metadata: dict[str, Any],
    *,
    schematic: bool = False,
    bim_objects: bool = False,
    stop_on_error: bool = True,
    atomic: bool = True,
) -> str:
    if atomic:
        return _create_primitives_native_batch(
            tool,
            primitives,
            metadata,
            schematic=schematic,
            bim_objects=bim_objects,
        )
    return _create_primitives_legacy(
        tool,
        primitives,
        metadata,
        schematic=schematic,
        bim_objects=bim_objects,
        stop_on_error=stop_on_error,
    )


def _create_floor_plan_primitives(
    tool: str,
    primitives: list[dict[str, Any]],
    metadata: dict[str, Any],
    *,
    atomic: bool = True,
) -> str:
    return _create_primitives(tool, primitives, metadata, schematic=True, bim_objects=False, atomic=atomic)


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


def _wall_object(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    thickness: float,
    height: float,
    *,
    name: str = "",
    class_name: str = "A-Wall",
    style_name: str = "",
    role: str = "wall",
) -> dict[str, Any]:
    if start_x == end_x and start_y == end_y:
        raise ValueError("wall endpoints must not be identical")
    if thickness <= 0:
        raise ValueError("wall thickness must be > 0")
    if height <= 0:
        raise ValueError("wall height must be > 0")
    wall = {
        "role": role,
        "object_type": "wall",
        "start_x": start_x,
        "start_y": start_y,
        "end_x": end_x,
        "end_y": end_y,
        "thickness": thickness,
        "height": height,
        "name": name,
        "class_name": class_name,
    }
    if style_name:
        wall["style_name"] = style_name
    return wall


def _room_wall_objects(
    x: float,
    y: float,
    width: float,
    depth: float,
    wall_thickness: float,
    wall_height: float,
    *,
    name: str = "",
    class_name: str = "A-Wall",
    style_name: str = "",
    role_prefix: str = "",
) -> list[dict[str, Any]]:
    if width <= 0 or depth <= 0:
        raise ValueError("room width and depth must be > 0")
    prefix = f"{role_prefix}_" if role_prefix else ""
    x2 = x + width
    y2 = y + depth
    return [
        _wall_object(x, y, x2, y, wall_thickness, wall_height, name=_named(name, "south wall"), class_name=class_name, style_name=style_name, role=f"{prefix}south_wall"),
        _wall_object(x2, y, x2, y2, wall_thickness, wall_height, name=_named(name, "east wall"), class_name=class_name, style_name=style_name, role=f"{prefix}east_wall"),
        _wall_object(x2, y2, x, y2, wall_thickness, wall_height, name=_named(name, "north wall"), class_name=class_name, style_name=style_name, role=f"{prefix}north_wall"),
        _wall_object(x, y2, x, y, wall_thickness, wall_height, name=_named(name, "west wall"), class_name=class_name, style_name=style_name, role=f"{prefix}west_wall"),
    ]


def _room_label_object(
    x: float,
    y: float,
    width: float,
    depth: float,
    text: str,
    *,
    text_size: float,
    class_name: str,
    role: str,
) -> dict[str, Any]:
    return {
        "role": role,
        "object_type": "text",
        "text": text,
        "x": x + width / 2,
        "y": y + depth / 2,
        "width": max(width * 0.8, 0),
        "text_size": text_size,
        "name": text,
        "class_name": class_name,
    }


def _room_dimension_objects(
    x: float,
    y: float,
    width: float,
    depth: float,
    *,
    offset: float,
    class_name: str,
    role_prefix: str,
    name: str,
) -> list[dict[str, Any]]:
    return [
        {
            "role": f"{role_prefix}_width_dimension",
            "object_type": "linear_dimension",
            "start_x": x,
            "start_y": y,
            "end_x": x + width,
            "end_y": y,
            "offset": -abs(offset),
            "name": _named(name, "width dimension"),
            "class_name": class_name,
        },
        {
            "role": f"{role_prefix}_depth_dimension",
            "object_type": "linear_dimension",
            "start_x": x,
            "start_y": y,
            "end_x": x,
            "end_y": y + depth,
            "offset": -abs(offset),
            "name": _named(name, "depth dimension"),
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


def _build_bim_floor_plan_objects(
    rooms: Optional[list[dict[str, Any]]],
    walls: Optional[list[dict[str, Any]]],
    *,
    wall_thickness: float,
    wall_height: float,
    name: str,
    wall_class: str,
    annotation_class: str,
    dimension_class: str,
    wall_style_name: str,
    label_rooms: bool,
    dimension_rooms: bool,
    label_text_size: float,
    dimension_offset: float,
) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    rooms = rooms or []
    if not rooms and not walls:
        raise ValueError("provide at least one room or wall")

    objects: list[dict[str, Any]] = []
    warnings: list[str] = []
    counts = {
        "rooms_count": len(rooms),
        "wall_segments_count": len(walls or []),
        "labels_count": 0,
        "dimensions_count": 0,
    }

    if label_text_size < 0:
        raise ValueError("label_text_size must be >= 0")
    if dimension_offset < 0:
        raise ValueError("dimension_offset must be >= 0")

    for index, room in enumerate(rooms, start=1):
        label = f"rooms[{index}]"
        if not isinstance(room, dict):
            raise ValueError(f"{label} must be an object")
        room_name = _prefixed_name(name, _optional_text(room, "name"), f"room {index}")
        room_class = _optional_text(room, "class_name", wall_class)
        room_style = _optional_text(room, "style_name", wall_style_name)
        x = _coerce_number(room, "x", required=True, label=label)
        y = _coerce_number(room, "y", required=True, label=label)
        width = _coerce_positive_number(room, "width", label=label)
        depth = _coerce_positive_number(room, "depth", label=label)
        room_thickness = _coerce_positive_number(room, "wall_thickness", default=wall_thickness, label=label)
        room_height = _coerce_positive_number(room, "wall_height", default=wall_height, label=label)
        objects.extend(
            _room_wall_objects(
                x,
                y,
                width,
                depth,
                room_thickness,
                room_height,
                name=room_name,
                class_name=room_class,
                style_name=room_style,
                role_prefix=f"room_{index}",
            )
        )
        if label_rooms:
            objects.append(
                _room_label_object(
                    x,
                    y,
                    width,
                    depth,
                    room_name,
                    text_size=label_text_size,
                    class_name=annotation_class,
                    role=f"room_{index}_label",
                )
            )
            counts["labels_count"] += 1
        if dimension_rooms:
            room_dimensions = _room_dimension_objects(
                x,
                y,
                width,
                depth,
                offset=dimension_offset,
                class_name=dimension_class,
                role_prefix=f"room_{index}",
                name=room_name,
            )
            objects.extend(room_dimensions)
            counts["dimensions_count"] += len(room_dimensions)

    for index, wall in enumerate(walls or [], start=1):
        label = f"walls[{index}]"
        if not isinstance(wall, dict):
            raise ValueError(f"{label} must be an object")
        wall_name = _prefixed_name(name, _optional_text(wall, "name"), f"wall segment {index}")
        objects.append(
            _wall_object(
                _coerce_number_any(wall, ("start_x", "x1"), required=True, label=label),
                _coerce_number_any(wall, ("start_y", "y1"), required=True, label=label),
                _coerce_number_any(wall, ("end_x", "x2"), required=True, label=label),
                _coerce_number_any(wall, ("end_y", "y2"), required=True, label=label),
                _coerce_positive_number(wall, "thickness", default=wall_thickness, label=label),
                _coerce_positive_number(wall, "height", default=wall_height, label=label),
                name=wall_name,
                class_name=_optional_text(wall, "class_name", wall_class),
                style_name=_optional_text(wall, "style_name", wall_style_name),
                role=f"wall_segment_{index}",
            )
        )

    if wall_style_name:
        warnings.append("wall_style_name is applied only when the native bridge can resolve an existing Wall Style resource")
    warnings.append("native doors/windows/spaces are intentionally excluded until plugin parameter inspection and wall-hosting smoke tests are implemented")
    return objects, warnings, counts


def _normalise_operation_property_edits(value: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise ValueError(f"{label} must be a list containing 1 to 100 edits")
    normalised: list[dict[str, Any]] = []
    for index, edit in enumerate(value, start=1):
        edit_label = f"{label}[{index}]"
        if not isinstance(edit, dict):
            raise ValueError(f"{edit_label} must be an object")
        unknown = sorted(set(edit) - {"ref", "expected_type", "expected_layer", "expected_name", "properties"})
        if unknown:
            raise ValueError(f"{edit_label} has unsupported key(s): {', '.join(unknown)}")
        ref = str(edit.get("ref", "") or "").strip()
        if not ref or not ref.startswith(("uuid:", "name:", "handle:", "$")):
            raise ValueError(f"{edit_label}.ref must start with uuid:, name:, handle:, or $ for a prior operation_id")
        properties = edit.get("properties")
        if not isinstance(properties, dict) or not properties or len(properties) > 20:
            raise ValueError(f"{edit_label}.properties must contain 1 to 20 supported properties")
        normalised_properties: dict[str, str] = {}
        for property_name, value in properties.items():
            property_name = str(property_name)
            if property_name not in PROPERTY_NAME_VALUES:
                raise ValueError(
                    f"{edit_label}.properties.{property_name} is unsupported; "
                    f"use one of: {', '.join(sorted(PROPERTY_NAME_VALUES))}"
                )
            normalised_value, value_error = _normalize_property_value(property_name, value)
            if value_error is not None:
                raise ValueError(f"{edit_label}.properties.{property_name}: {value_error}")
            normalised_properties[property_name] = str(normalised_value)
        item: dict[str, Any] = {"ref": ref, "properties": normalised_properties}
        for guard in ("expected_type", "expected_layer", "expected_name"):
            guard_value = str(edit.get(guard, "") or "")
            if guard_value:
                item[guard] = guard_value
        normalised.append(item)
    return normalised


def _normalise_atomic_target(value: Any, *, label: str) -> str:
    target = str(value or "").strip()
    if not target.startswith(("uuid:", "name:", "handle:", "$")):
        raise ValueError(f"{label} must start with uuid:, name:, handle:, or $ for a prior operation_id")
    if target == "$" or target.endswith(":"):
        raise ValueError(f"{label} has an empty reference value")
    if len(target) > 512:
        raise ValueError(f"{label} is limited to 512 characters")
    return target


def _normalise_execute_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalised: list[dict[str, Any]] = []
    for index, operation in enumerate(operations, start=1):
        label = f"operations[{index}]"
        if not isinstance(operation, dict):
            raise ValueError(f"{label} must be an object")
        unknown = sorted(set(operation) - {"type", "operation_id", "params"})
        if unknown:
            raise ValueError(f"{label} has unsupported key(s): {', '.join(unknown)}")
        operation_type = str(operation.get("type", "") or "").strip().lower()
        if operation_type not in {"create", "set_properties", "transform", "duplicate", "delete"}:
            raise ValueError(
                f"{label}.type must be create, set_properties, transform, duplicate, or delete"
            )
        params = operation.get("params")
        if not isinstance(params, dict):
            raise ValueError(f"{label}.params must be an object")
        operation_id = str(operation.get("operation_id", "") or "").strip()
        if operation_id and (len(operation_id) > 128 or not _IDEMPOTENCY_KEY_RE.fullmatch(operation_id)):
            raise ValueError(
                f"{label}.operation_id must match {_IDEMPOTENCY_KEY_RE.pattern} and be at most 128 characters"
            )

        if operation_id and operation_type not in {"create", "duplicate"}:
            raise ValueError(f"{label}.operation_id is supported only for create and duplicate")

        if operation_type == "create":
            canonical_params = _normalise_create_primitive(params, label=f"{label}.params")
        elif operation_type == "set_properties":
            unknown_params = sorted(set(params) - {"edits"})
            if unknown_params:
                raise ValueError(f"{label}.params has unsupported key(s): {', '.join(unknown_params)}")
            canonical_params = {
                "edits": _normalise_operation_property_edits(params.get("edits"), label=f"{label}.params.edits")
            }
        elif operation_type == "transform":
            unknown_params = sorted(
                set(params) - {
                    "target", "ref", "dx", "dy", "rotation_deg",
                    "scale_x", "scale_y", "pivot_x", "pivot_y",
                }
            )
            if unknown_params:
                raise ValueError(f"{label}.params has unsupported key(s): {', '.join(unknown_params)}")
            target = _normalise_atomic_target(
                params.get("target", params.get("ref")),
                label=f"{label}.params.target",
            )
            dx = _coerce_number(params, "dx", default=0, label=f"{label}.params")
            dy = _coerce_number(params, "dy", default=0, label=f"{label}.params")
            rotation_deg = _coerce_number(params, "rotation_deg", default=0, label=f"{label}.params")
            scale_x = _coerce_positive_number(params, "scale_x", default=1, label=f"{label}.params")
            scale_y = _coerce_positive_number(params, "scale_y", default=1, label=f"{label}.params")
            has_pivot_x = "pivot_x" in params
            has_pivot_y = "pivot_y" in params
            if has_pivot_x != has_pivot_y:
                raise ValueError(f"{label}.params.pivot_x and pivot_y must be provided together")
            if dx == 0 and dy == 0 and rotation_deg == 0 and scale_x == 1 and scale_y == 1:
                raise ValueError(f"{label}.params transform must change translation, rotation, or scale")
            canonical_params = {
                "target": target,
                "dx": dx,
                "dy": dy,
                "rotation_deg": rotation_deg,
                "scale_x": scale_x,
                "scale_y": scale_y,
            }
            if has_pivot_x:
                canonical_params["pivot_x"] = _coerce_number(
                    params, "pivot_x", required=True, label=f"{label}.params"
                )
                canonical_params["pivot_y"] = _coerce_number(
                    params, "pivot_y", required=True, label=f"{label}.params"
                )
        elif operation_type == "duplicate":
            unknown_params = sorted(set(params) - {"target", "ref", "dx", "dy"})
            if unknown_params:
                raise ValueError(f"{label}.params has unsupported key(s): {', '.join(unknown_params)}")
            canonical_params = {
                "target": _normalise_atomic_target(
                    params.get("target", params.get("ref")),
                    label=f"{label}.params.target",
                ),
                "dx": _coerce_number(params, "dx", default=0, label=f"{label}.params"),
                "dy": _coerce_number(params, "dy", default=0, label=f"{label}.params"),
            }
        else:
            unknown_params = sorted(set(params) - {"target", "ref"})
            if unknown_params:
                raise ValueError(f"{label}.params has unsupported key(s): {', '.join(unknown_params)}")
            canonical_params = {
                "target": _normalise_atomic_target(
                    params.get("target", params.get("ref")),
                    label=f"{label}.params.target",
                )
            }
            if canonical_params["target"].startswith("$"):
                raise ValueError(f"{label}.params.target delete requires an external uuid:, name:, or handle: ref")

        item: dict[str, Any] = {"type": operation_type, "params": canonical_params}
        if operation_id:
            item["operation_id"] = operation_id
        normalised.append(item)
    return normalised


def _operation_plan_hash(operations: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        operations,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _operation_idempotency_lookup(
    idempotency_key: str,
    plan_hash: str,
) -> str:
    with _operation_idempotency_lock:
        cached_hash = _operation_idempotency_cache.get(idempotency_key)
        if cached_hash is None:
            return "miss"
        if cached_hash != plan_hash:
            return "conflict"
        return "same"


def _remember_operation_result(
    idempotency_key: str,
    plan_hash: str,
) -> None:
    with _operation_idempotency_lock:
        _operation_idempotency_cache[idempotency_key] = plan_hash
        while len(_operation_idempotency_cache) > _MAX_OPERATION_IDEMPOTENCY_ENTRIES:
            oldest = next(iter(_operation_idempotency_cache))
            del _operation_idempotency_cache[oldest]


def _fast_native_status_error(status: dict[str, Any]) -> Optional[str]:
    errors = _fast_native_readiness_errors(status)
    if not errors:
        return None
    return (
        "required phase-4 capability manifest is not ready ({0}); upgrade/restart the native bridge "
        "instead of using a compatibility fallback"
    ).format("; ".join(errors))


def _fast_execution_bridge_status(trace: dict[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    cached = _cached_cad_safe_status()
    if cached is not None:
        trace["preflight_cache_hit"] = True
        fast_error = _fast_native_status_error(cached)
        if fast_error:
            return cached, fast_error
        return cached, None
    started = time.perf_counter()
    try:
        response = _request_once_health("ping", None, trace)
    except Exception as exc:
        return None, str(exc)
    finally:
        trace["preflight_ms"] = float(trace.get("preflight_ms", 0.0)) + (time.perf_counter() - started) * 1000.0
    if response.get("success") is not True or not isinstance(response.get("result"), dict):
        return None, str(response.get("error", "bridge status was not an object"))
    status = response["result"]
    evaluated = _evaluate_cad_preflight_status(status)
    if not evaluated.get("ok"):
        return status, str(evaluated.get("reason", "bridge is not CAD-safe"))
    fast_error = _fast_native_status_error(status)
    if fast_error:
        return status, fast_error
    _remember_cad_safe_status(status)
    return status, None


def _compact_operation_receipts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    receipts: list[dict[str, Any]] = []
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            continue
        receipt: dict[str, Any] = {"index": entry.get("index", index)}
        for key in (
            "type",
            "op",
            "operation_id",
            "local_ref",
            "ref",
            "target",
            "target_ref",
            "uuid",
            "handle",
            "verified",
        ):
            field = entry.get(key)
            if isinstance(field, (str, bool)) and field != "":
                receipt[key] = field
        # Native duplicate receipts carry the newly created object's stable
        # identity in the nested semantic snapshot. Promote only the compact
        # identity fields so callers can safely target the duplicate in a
        # later transaction without requiring a drawing scan.
        duplicate = entry.get("duplicate")
        if isinstance(duplicate, dict):
            for key in ("uuid", "handle", "type"):
                field = duplicate.get(key)
                if key not in receipt and isinstance(field, str) and field:
                    receipt[key] = field
        receipts.append(receipt)
    return receipts


def _execute_operations_response(
    core: dict[str, Any],
    trace: dict[str, Any],
    outcome: str,
) -> str:
    timing = _finish_request_trace(trace, outcome)
    payload = dict(core)
    payload["timing"] = timing
    _emit_request_trace(trace, timing)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@_tool("vw_execute_operations")
def vw_execute_operations(
    operations: ExecuteOperationList,
    idempotency_key: IdempotencyKey,
) -> str:
    """Preferred native write path with internal preflight and compact receipts.

    Requires native apply_operations. Create, set-properties, transform,
    duplicate, and delete operations are forwarded together without fallback.
    Reuse idempotency_key only for the identical plan.
    """
    trace = _new_request_trace("vw_execute_operations", "execute_operations")
    try:
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
            raise ValueError(
                f"idempotency_key must match {_IDEMPOTENCY_KEY_RE.pattern} and be at most 128 characters"
            )
        normalised = _normalise_execute_operations(operations)
    except ValueError as exc:
        return _execute_operations_response(
            {
                "ok": False,
                "tool": "vw_execute_operations",
                "error": str(exc),
                "idempotency_key": idempotency_key,
            },
            trace,
            "validation_error",
        )

    plan_hash = _operation_plan_hash(normalised)
    cache_state = _operation_idempotency_lookup(idempotency_key, plan_hash)
    if cache_state == "conflict":
        return _execute_operations_response(
            {
                "ok": False,
                "tool": "vw_execute_operations",
                "error": "idempotency_key was already used for a different operation plan",
                "idempotency_key": idempotency_key,
                "plan_hash": plan_hash,
            },
            trace,
            "idempotency_conflict",
        )
    status, status_error = _fast_execution_bridge_status(trace)
    if status_error or not isinstance(status, dict):
        return _execute_operations_response(
            {
                "ok": False,
                "tool": "vw_execute_operations",
                "error": f"CAD preflight failed: {status_error or 'bridge status unavailable'}",
                "idempotency_key": idempotency_key,
                "plan_hash": plan_hash,
            },
            trace,
            "preflight_error",
        )

    implemented_actions = set(status.get("implemented_actions") or [])
    primitives = [dict(operation["params"]) for operation in normalised if operation["type"] == "create"]
    requested_types = {str(primitive.get("object_type", "")) for primitive in primitives}
    unsupported_types = sorted(requested_types - _native_create_object_types(status))
    if unsupported_types:
        return _execute_operations_response(
            {
                "ok": False,
                "tool": "vw_execute_operations",
                "error": "operation plan includes create types not implemented by this native bridge",
                "unsupported_object_types": unsupported_types,
                "idempotency_key": idempotency_key,
                "plan_hash": plan_hash,
            },
            trace,
            "unsupported",
        )
    if status.get("native_bridge") is not True:
        return _execute_operations_response(
            {
                "ok": False,
                "tool": "vw_execute_operations",
                "error": "vw_execute_operations requires the native SDK bridge",
                "idempotency_key": idempotency_key,
                "plan_hash": plan_hash,
            },
            trace,
            "unsupported",
        )

    if "apply_operations" in implemented_actions:
        execution_path = "apply_operations"
        trace["action"] = execution_path
        wire_operations: list[dict[str, Any]] = []
        for operation in normalised:
            operation_type = operation["type"]
            if operation_type == "create":
                wire_item = {"op": "create", **dict(operation["params"])}
                wire_item.pop("role", None)
                operation_id = str(operation.get("operation_id", "") or "")
                if operation_id:
                    wire_item["local_ref"] = operation_id
                wire_operations.append(wire_item)
                continue

            if operation_type in {"transform", "duplicate", "delete"}:
                canonical = dict(operation["params"])
                if operation_type == "transform":
                    wire_item = {
                        "op": "object.transform",
                        "target": canonical["target"],
                        "delta_x": canonical["dx"],
                        "delta_y": canonical["dy"],
                        "rotation_degrees": canonical["rotation_deg"],
                        "scale_x": canonical["scale_x"],
                        "scale_y": canonical["scale_y"],
                    }
                    if "pivot_x" in canonical:
                        wire_item["pivot_x"] = canonical["pivot_x"]
                        wire_item["pivot_y"] = canonical["pivot_y"]
                elif operation_type == "duplicate":
                    wire_item = {
                        "op": "object.duplicate",
                        "target": canonical["target"],
                        "delta_x": canonical["dx"],
                        "delta_y": canonical["dy"],
                    }
                else:
                    wire_item = {
                        "op": "object.delete",
                        "target": canonical["target"],
                        "confirm": "DELETE_OBJECT",
                    }
                operation_id = str(operation.get("operation_id", "") or "")
                if operation_type == "duplicate" and operation_id:
                    wire_item["local_ref"] = operation_id
                wire_operations.append(wire_item)
                continue

            for edit in operation["params"]["edits"]:
                target = str(edit["ref"])
                if any(edit.get(guard) for guard in ("expected_type", "expected_layer", "expected_name")):
                    return _execute_operations_response(
                        {
                            "ok": False,
                            "tool": "vw_execute_operations",
                            "error": (
                                "expected_type/expected_layer/expected_name guards require the verified property-edit "
                                "workflow; focused native apply_operations does not scan targets"
                            ),
                            "idempotency_key": idempotency_key,
                            "plan_hash": plan_hash,
                        },
                        trace,
                        "validation_error",
                    )
                for property_name, value in edit["properties"].items():
                    wire_operations.append(
                        {
                            "op": "set_property",
                            "target": target,
                            "property_name": property_name,
                            "value": value,
                        }
                    )
        wire_params = {"operation_count": len(wire_operations), "idempotency_key": idempotency_key}
        for index, wire_operation in enumerate(wire_operations, start=1):
            wire_params[f"operation_{index}_json"] = json.dumps(
                wire_operation,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        raw = _send("apply_operations", wire_params, require_cad_safe=True, trace=trace)
    else:
        return _execute_operations_response(
            {
                "ok": False,
                "tool": "vw_execute_operations",
                "error": (
                    "native bridge does not advertise required phase-4 action: apply_operations; "
                    "upgrade/restart the native bridge instead of using a compatibility fallback"
                ),
                "idempotency_key": idempotency_key,
                "plan_hash": plan_hash,
            },
            trace,
            "unsupported",
        )

    decoded = _decode_tool_result(raw)
    if _tool_result_failed(raw, decoded) or not isinstance(decoded, dict):
        error = decoded.get("error") if isinstance(decoded, dict) else str(decoded)
        return _execute_operations_response(
            {
                "ok": False,
                "tool": "vw_execute_operations",
                "error": str(error or "native operation request failed"),
                "execution_path": execution_path,
                "idempotency_key": idempotency_key,
                "plan_hash": plan_hash,
            },
            trace,
            "error",
        )

    if isinstance(decoded.get("timing"), dict):
        trace["native"] = decoded["timing"]
    transaction = decoded.get("transaction") if isinstance(decoded.get("transaction"), dict) else decoded
    receipt_source = transaction.get("operations")
    if not isinstance(receipt_source, list):
        receipt_source = transaction.get("results")
    if not isinstance(receipt_source, list):
        receipt_source = transaction.get("created")
    receipts = _compact_operation_receipts(receipt_source)
    native_wire_count = transaction.get(
        "operation_count",
        transaction.get("applied_count", transaction.get("created_count", len(receipts))),
    )
    if not isinstance(native_wire_count, int) or isinstance(native_wire_count, bool):
        native_wire_count = len(receipts)
    expected_wire_count = len(wire_operations)
    committed = transaction.get("committed") is True
    self_verified = (
        committed
        and native_wire_count == expected_wire_count
        and len(receipts) == expected_wire_count
        and all(
            receipt.get("verified") is True
            or receipt.get("uuid")
            or receipt.get("handle")
            or receipt.get("target")
            or receipt.get("target_ref")
            for receipt in receipts
        )
    )
    if decoded.get("verified") is True and native_wire_count == expected_wire_count:
        self_verified = True

    core = {
        "ok": committed and native_wire_count == expected_wire_count,
        "tool": "vw_execute_operations",
        "execution_path": execution_path,
        "atomic": True,
        "operation_count": len(normalised),
        "wire_operation_count": expected_wire_count,
        "applied_count": len(normalised) if committed and native_wire_count == expected_wire_count else 0,
        "applied_wire_count": native_wire_count,
        "idempotency_key": idempotency_key,
        "idempotency_replay": bool(decoded.get("replayed", False)),
        "idempotency_scope": "native_active_document",
        "plan_hash": plan_hash,
        "verification": {
            "ok": self_verified,
            "method": "native_atomic_receipt",
            "receipt_count": len(receipts),
            "receipts": receipts,
            "drawing_scan_performed": False,
        },
    }
    if core["ok"]:
        _remember_operation_result(idempotency_key, plan_hash)
    return _execute_operations_response(core, trace, "ok" if core["ok"] else "error")


@_tool("vw_batch_create_objects")
def vw_batch_create_objects(
    objects: PrimitiveObjectList,
    default_class_name: str = "",
    name_prefix: str = "",
    stop_on_error: bool = True,
    atomic: bool = True,
) -> str:
    """Create many native objects in one MCP call.
    Supported object_type values are rect/rectangle/box, circle, oval, line, arc,
    polygon/polyline, wall, text, and linear_dimension when advertised by the bridge.
    By default this uses the native atomic batch action so either all objects are created or none are."""
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
            "atomic": atomic,
        },
        schematic=False,
        bim_objects=False,
        stop_on_error=stop_on_error,
        atomic=atomic,
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
    atomic: bool = True,
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
            "atomic": atomic,
            **counts,
        },
        schematic=True,
        bim_objects=False,
        stop_on_error=stop_on_error,
        atomic=atomic,
    )


@_tool("vw_create_bim_floor_plan")
def vw_create_bim_floor_plan(
    rooms: Optional[OptionalFloorPlanRoomList] = None,
    walls: Optional[FloorPlanItemList] = None,
    wall_thickness: PositiveLength = 200,
    wall_height: PositiveLength = 3000,
    name: str = "",
    wall_class: str = "A-Wall",
    annotation_class: str = "A-Annotation",
    dimension_class: str = "Dimension",
    wall_style_name: str = "",
    label_rooms: bool = True,
    dimension_rooms: bool = True,
    label_text_size: float = 10,
    dimension_offset: float = 500,
    atomic: bool = True,
) -> str:
    """Create a native wall-based floor plan from structured rectangular rooms and wall segments.
    This creates true Vectorworks wall objects plus optional text labels and linear dimensions, but not native doors/windows/spaces yet."""
    try:
        objects, warnings, counts = _build_bim_floor_plan_objects(
            rooms,
            walls,
            wall_thickness=wall_thickness,
            wall_height=wall_height,
            name=name,
            wall_class=wall_class,
            annotation_class=annotation_class,
            dimension_class=dimension_class,
            wall_style_name=wall_style_name,
            label_rooms=label_rooms,
            dimension_rooms=dimension_rooms,
            label_text_size=label_text_size,
            dimension_offset=dimension_offset,
        )
    except ValueError as exc:
        return _json_error("vw_create_bim_floor_plan", str(exc), schematic=False, bim_objects=True)

    return _create_primitives(
        "vw_create_bim_floor_plan",
        objects,
        {
            "object_count": len(objects),
            "warnings": warnings,
            "atomic": atomic,
            **counts,
        },
        schematic=False,
        bim_objects=True,
        atomic=atomic,
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
    atomic: bool = True,
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
        {"origin": [x, y], "width": width, "depth": depth, "wall_thickness": wall_thickness, "atomic": atomic},
        atomic=atomic,
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
    atomic: bool = True,
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
            "atomic": atomic,
        },
        atomic=atomic,
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
    atomic: bool = True,
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
            "atomic": atomic,
        },
        atomic=atomic,
    )


@_tool("vw_get_layers")
def vw_get_layers() -> str:
    """List all layers with name and visibility."""
    return _send_tool("vw_get_layers")


@_tool("vw_get_objects")
def vw_get_objects(layer: str = "", object_type: str = "", limit: ObjectQueryLimit = 100) -> str:
    """List objects. Filter by layer name and type such as rect, line, or wall."""
    return _send_tool("vw_get_objects", {"layer": layer, "object_type": object_type, "limit": limit})


def _status_supports_direct_action(status: Any, action: str) -> bool:
    if not isinstance(status, dict):
        return False
    if status.get("native_bridge") is True:
        implemented_actions = status.get("implemented_actions")
        return isinstance(implemented_actions, list) and action in set(str(item) for item in implemented_actions)
    return status.get("cad_api_safe") is True and status.get("transport_only") is not True


@_tool("vw_lookup_objects")
def vw_lookup_objects(
    criteria: str = "",
    layer: str = "",
    object_type: str = "",
    name: str = "",
    class_name: str = "",
    limit: ObjectQueryLimit = 100,
    detail: LookupDetail = "brief",
    include_refs: bool = True,
    fields: Optional[ObjectFieldList] = None,
) -> str:
    """Token-efficient object lookup with stable agent refs for follow-up planning."""
    parsed = _parse_simple_find_criteria(criteria) if criteria else None
    if criteria and parsed is None:
        return _json_error(
            "vw_lookup_objects",
            "criteria must be ALL, T=TYPE, C=Class, N=Name, or exact-name ((N='Name')); use vw_find_objects for complex criteria",
            criteria=criteria,
        )

    parsed_field, parsed_value = parsed or ("", "")
    effective_type = object_type or (parsed_value if parsed_field == "type" else "")
    effective_name = name or (parsed_value if parsed_field == "name" else "")
    effective_class = class_name or (parsed_value if parsed_field == "class" else "")

    raw = _send(
        "get_objects",
        {"layer": layer, "object_type": effective_type, "limit": limit},
        require_cad_safe=True,
    )
    objects = _decode_tool_result(raw)
    if _tool_result_failed(raw, objects):
        return json.dumps(
            {
                "ok": False,
                "tool": "vw_lookup_objects",
                "query": {
                    "criteria": criteria,
                    "layer": layer,
                    "object_type": object_type,
                    "name": name,
                    "class_name": class_name,
                    "limit": limit,
                },
                "result": objects,
            },
            indent=2,
            sort_keys=True,
        )
    if not isinstance(objects, list):
        return _json_error(
            "vw_lookup_objects",
            f"get_objects returned {type(objects).__name__}, expected list",
            result=objects,
        )

    matches = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        if effective_name and str(obj.get("name") or "") != effective_name:
            continue
        if effective_class and str(obj.get("class") or obj.get("class_name") or "") != effective_class:
            continue
        if effective_type and str(obj.get("type") or "").lower() != effective_type.lower():
            continue
        matches.append(obj)

    compact_objects = [
        _compact_object_record(obj, detail=detail, include_refs=include_refs, fields=fields)
        for obj in matches[:limit]
    ]
    return json.dumps(
        {
            "ok": True,
            "tool": "vw_lookup_objects",
            "query": {
                "criteria": criteria,
                "layer": layer,
                "object_type": object_type,
                "name": name,
                "class_name": class_name,
                "limit": limit,
                "detail": detail,
                "include_refs": include_refs,
                "fields": fields or [],
            },
            "matched": len(matches),
            "returned": len(compact_objects),
            "possibly_truncated": len(objects) >= limit,
            "objects": compact_objects,
        },
        indent=2,
        sort_keys=True,
    )


@_tool("vw_drawing_summary")
def vw_drawing_summary(
    layer: str = "",
    object_type: str = "",
    limit: ObjectQueryLimit = 1000,
    include_examples: bool = True,
    example_limit: SummaryExampleLimit = 20,
    scan_limit: SummaryScanLimit = 50_000,
) -> str:
    """Summarize document, layers, and a bounded object inventory for production planning/verification."""
    direct_params = {
        "layer": layer,
        "object_type": object_type,
        "limit": limit,
        "include_examples": include_examples,
        "example_limit": example_limit,
        "scan_limit": scan_limit,
    }
    cached_status = _cached_cad_safe_status()
    if cached_status is not None:
        decoded_status = cached_status
        status_ok = True
    else:
        raw_status = _send_health("ping")
        decoded_status = _decode_tool_result(raw_status)
        status_ok = not _tool_result_failed(raw_status, decoded_status)
    if status_ok and isinstance(decoded_status, dict) and _evaluate_cad_preflight_status(decoded_status).get("ok") is True:
        _remember_cad_safe_status(decoded_status)
    if status_ok and _status_supports_direct_action(decoded_status, "drawing_summary"):
        preflight = _evaluate_cad_preflight_status(
            decoded_status,
            blocked_action="drawing_summary",
            blocked_params=direct_params,
        )
        if preflight.get("ok") and isinstance(decoded_status, dict):
            _remember_cad_safe_status(decoded_status)
            raw_summary = _send("drawing_summary", direct_params, require_cad_safe=True)
            direct_summary = _decode_tool_result(raw_summary)
            if not _tool_result_failed(raw_summary, direct_summary) and isinstance(direct_summary, dict):
                return json.dumps(direct_summary, indent=2, sort_keys=True)

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
        if include_examples and len(examples) < example_limit:
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

    payload = {
        "ok": True,
        "tool": "vw_drawing_summary",
        "query": {
            "layer": layer,
            "object_type": object_type,
            "limit": limit,
            "include_examples": include_examples,
            "example_limit": example_limit,
            "scan_limit": scan_limit,
            "source": "composed_get_objects",
        },
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
    }
    if include_examples:
        payload["examples"] = examples

    return json.dumps(payload, indent=2, sort_keys=True)


@_tool("vw_set_object_property")
def vw_set_object_property(handle: str, property_name: PropertyName, value: str) -> str:
    """Set one object property after resolving the handle and verifying readback."""
    return vw_batch_set_object_properties(
        [{"ref": f"handle:{handle}", "properties": {property_name: value}}],
        verify=True,
    )


@_tool("vw_batch_set_object_properties")
def vw_batch_set_object_properties(
    edits: BatchPropertyEditList,
    verify: bool = True,
    lookup_limit: ObjectQueryLimit = MAX_OBJECT_QUERY_LIMIT,
    stop_on_failure: bool = True,
) -> str:
    """Resolve object refs and set multiple object properties with optional readback verification.

    Each edit is {"ref": "uuid:...|name:...|handle:...", "properties": {"name": "..."}}.
    Optional guards per edit: expected_type, expected_layer, expected_name.
    """
    validation_failures: list[dict[str, Any]] = []
    normalized_edits: list[dict[str, Any]] = []
    for index, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            validation_failures.append({"index": index, "error": "edit must be a JSON object"})
            continue

        ref = str(edit.get("ref") or "").strip()
        properties = edit.get("properties")
        if not ref:
            validation_failures.append({"index": index, "error": "ref is required"})
        if not isinstance(properties, dict) or not properties:
            validation_failures.append({"index": index, "ref": ref, "error": "properties must be a non-empty object"})
            continue
        if len(properties) > 20:
            validation_failures.append({"index": index, "ref": ref, "error": "properties is limited to 20 keys"})
            continue

        normalized_properties: dict[str, str] = {}
        for property_name, value in properties.items():
            property_name = str(property_name)
            if property_name not in PROPERTY_NAME_VALUES:
                validation_failures.append(
                    {
                        "index": index,
                        "ref": ref,
                        "error": "unsupported property",
                        "property_name": property_name,
                        "allowed_properties": sorted(PROPERTY_NAME_VALUES),
                    }
                )
                continue
            normalized_value, value_error = _normalize_property_value(property_name, value)
            if value_error is not None:
                validation_failures.append(
                    {
                        "index": index,
                        "ref": ref,
                        "error": value_error,
                        "property_name": property_name,
                    }
                )
                continue
            normalized_properties[property_name] = str(normalized_value)

        if normalized_properties:
            normalized_edits.append(
                {
                    "index": index,
                    "ref": ref,
                    "expected_type": str(edit.get("expected_type") or ""),
                    "expected_layer": str(edit.get("expected_layer") or ""),
                    "expected_name": str(edit.get("expected_name") or ""),
                    "properties": normalized_properties,
                }
            )

    if validation_failures:
        return json.dumps(
            {
                "ok": False,
                "tool": "vw_batch_set_object_properties",
                "phase": "validate",
                "writes_started": False,
                "failures": validation_failures,
            },
            indent=2,
            sort_keys=True,
        )

    raw_status = _send_health("ping")
    decoded_status = _decode_tool_result(raw_status)
    if _tool_result_failed(raw_status, decoded_status):
        return json.dumps(
            {
                "ok": False,
                "tool": "vw_batch_set_object_properties",
                "phase": "write_preflight",
                "writes_started": False,
                "result": decoded_status,
            },
            indent=2,
            sort_keys=True,
        )
    if not isinstance(decoded_status, dict):
        return _json_error(
            "vw_batch_set_object_properties",
            f"ping returned {type(decoded_status).__name__}, expected object",
            phase="write_preflight",
        )

    preflight = _evaluate_cad_preflight_status(
        decoded_status,
        blocked_action="set_property",
        blocked_params={"handle": "", "property_name": "name", "value": ""},
    )
    if preflight.get("ok") and isinstance(decoded_status, dict):
        _remember_cad_safe_status(decoded_status)
    else:
        return json.dumps(
            {
                "ok": False,
                "tool": "vw_batch_set_object_properties",
                "phase": "write_preflight",
                "writes_started": False,
                "preflight": preflight,
            },
            indent=2,
            sort_keys=True,
        )

    resolution_failures: list[dict[str, Any]] = []
    prepared_edits: list[dict[str, Any]] = []
    for edit in normalized_edits:
        resolved = _resolve_object_target(
            edit["ref"],
            expected_type=edit["expected_type"],
            expected_layer=edit["expected_layer"],
            expected_name=edit["expected_name"],
            lookup_limit=lookup_limit,
        )
        if resolved.get("ok") is not True:
            resolution_failures.append({"index": edit["index"], **resolved})
            continue
        prepared_edits.append({**edit, "resolved": resolved})

    if resolution_failures:
        return json.dumps(
            {
                "ok": False,
                "tool": "vw_batch_set_object_properties",
                "phase": "resolve",
                "writes_started": False,
                "failures": resolution_failures,
            },
            indent=2,
            sort_keys=True,
        )

    edit_results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    unknown_commit_state = False
    write_attempts = 0
    stop_batch = False

    for edit in prepared_edits:
        resolved = edit["resolved"]
        before = resolved["target"]
        edit_result: dict[str, Any] = {
            "index": edit["index"],
            "ref": edit["ref"],
            "target_ref": _compact_object_record(before, detail="normal", include_refs=True).get("ref"),
            "before": _compact_object_record(before, detail="normal", include_refs=True),
            "properties": edit["properties"],
            "property_results": [],
            "verified": None,
        }

        edit_failed = False
        for property_name, value in edit["properties"].items():
            write_attempts += 1
            raw = _send(
                "set_property",
                {"handle": resolved["handle"], "property_name": property_name, "value": value},
                require_cad_safe=True,
            )
            decoded = _decode_tool_result(raw)
            property_failed = _tool_result_failed(raw, decoded)
            property_result = {
                "property_name": property_name,
                "value": value,
                "ok": not property_failed,
                "result": decoded,
            }
            if property_failed:
                property_result["unknown_commit_state"] = raw.startswith("Unknown commit state")
                if property_result["unknown_commit_state"]:
                    unknown_commit_state = True
                failure = {
                    "index": edit["index"],
                    "ref": edit["ref"],
                    "property_name": property_name,
                    "result": decoded,
                    "unknown_commit_state": property_result["unknown_commit_state"],
                }
                failures.append(failure)
                edit_failed = True
                if stop_on_failure:
                    stop_batch = True
            edit_result["property_results"].append(property_result)
            if edit_failed and stop_on_failure:
                break

        if verify and not edit_failed:
            readback = _readback_object_snapshot(before, lookup_limit=lookup_limit)
            if readback.get("ok") is True:
                after = readback["target"]
                verification = _verify_property_changes(after, edit["properties"])
                edit_result["after"] = _compact_object_record(after, detail="normal", include_refs=True)
                edit_result["verification"] = verification
                edit_result["verified"] = bool(verification["verified"])
                if not verification["verified"]:
                    failures.append(
                        {
                            "index": edit["index"],
                            "ref": edit["ref"],
                            "error": "readback verification failed",
                            "verification": verification,
                        }
                    )
            else:
                edit_result["readback_error"] = readback
                edit_result["verified"] = False
                failures.append(
                    {
                        "index": edit["index"],
                        "ref": edit["ref"],
                        "error": "readback lookup failed",
                        "readback": readback,
                    }
                )
        elif not verify:
            edit_result["verified"] = None

        edit_results.append(edit_result)
        if stop_batch:
            break

    return json.dumps(
        {
            "ok": not failures and not unknown_commit_state,
            "tool": "vw_batch_set_object_properties",
            "verify": verify,
            "lookup_limit": lookup_limit,
            "edits_requested": len(edits),
            "edits_prepared": len(prepared_edits),
            "edits_completed": len(edit_results),
            "write_attempts": write_attempts,
            "unknown_commit_state": unknown_commit_state,
            "failures": failures,
            "edits": edit_results,
        },
        indent=2,
        sort_keys=True,
    )


@_tool("vw_find_objects")
def vw_find_objects(criteria: str, limit: ObjectQueryLimit = 100) -> str:
    """Find objects using VW criteria such as 'T=RECT', 'T=WALL', 'C=Furniture', or 'ALL'."""
    parsed = _parse_simple_find_criteria(criteria)
    if parsed is None:
        return _send_tool("vw_find_objects", {"criteria": criteria, "limit": limit})

    field, value = parsed
    if field in {"name", "class"}:
        raw_status = _send_health("ping")
        decoded_status = _decode_tool_result(raw_status)
        status_ok = not _tool_result_failed(raw_status, decoded_status)
        if status_ok and isinstance(decoded_status, dict) and _evaluate_cad_preflight_status(decoded_status).get("ok") is True:
            _remember_cad_safe_status(decoded_status)
        if status_ok and _status_supports_direct_action(decoded_status, "find_objects"):
            preflight = _evaluate_cad_preflight_status(
                decoded_status,
                blocked_action="find_objects",
                blocked_params={"criteria": criteria, "limit": limit},
            )
            if preflight.get("ok") and isinstance(decoded_status, dict):
                _remember_cad_safe_status(decoded_status)
                return _send_tool("vw_find_objects", {"criteria": criteria, "limit": limit})

    object_type = value if field == "type" else ""
    raw = _send("get_objects", {"layer": "", "object_type": object_type, "limit": limit}, require_cad_safe=True)
    objects = _decode_tool_result(raw)
    if _tool_result_failed(raw, objects):
        return json.dumps(
            {
                "ok": False,
                "tool": "vw_find_objects",
                "criteria": criteria,
                "fallback_action": "get_objects",
                "result": objects,
            },
            indent=2,
            sort_keys=True,
        )
    if not isinstance(objects, list):
        return _json_error(
            "vw_find_objects",
            f"get_objects fallback returned {type(objects).__name__}, expected list",
            criteria=criteria,
            fallback_action="get_objects",
        )

    if field == "all":
        matches = objects
    elif field == "type":
        matches = [obj for obj in objects if isinstance(obj, dict) and str(obj.get("type") or "").lower() == value]
    elif field == "name":
        matches = [obj for obj in objects if isinstance(obj, dict) and str(obj.get("name") or "") == value]
    elif field == "class":
        matches = [
            obj
            for obj in objects
            if isinstance(obj, dict)
            and str(obj.get("class") or obj.get("class_name") or "") == value
        ]
    else:
        matches = []

    return json.dumps(
        {
            "ok": True,
            "tool": "vw_find_objects",
            "criteria": criteria,
            "fallback_action": "get_objects",
            "matched": len(matches),
            "truncated": len(objects) >= limit,
            "objects": matches[:limit],
        },
        indent=2,
        sort_keys=True,
    )


@_tool("vw_manage_classes")
def vw_manage_classes(action: ClassAction, class_name: str = "", confirm: str = "") -> str:
    """List, create, or delete classes. class_name is ignored for list. Delete requires confirm='DELETE_CLASS'."""
    normalized_class_name = str(class_name or "").strip()
    if action in {"create", "delete"}:
        if not normalized_class_name:
            return _json_error("vw_manage_classes", "class_name is required", phase="validate", writes_started=False)
        if len(normalized_class_name) > MAX_PROPERTY_VALUE_CHARS:
            return _json_error(
                "vw_manage_classes",
                f"class_name is limited to {MAX_PROPERTY_VALUE_CHARS} characters",
                phase="validate",
                writes_started=False,
            )
    if action == "delete" and confirm != "DELETE_CLASS":
        return _confirmation_error(
            "vw_manage_classes",
            "DELETE_CLASS",
            "class deletion is destructive and requires explicit confirmation",
        )
    return _send_tool("vw_manage_classes", {"action": action, "class_name": normalized_class_name, "confirm": confirm})


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
    # Keep the legacy fallback field names while also supplying the native
    # bridge's explicit resource/angle names. This lets one public tool
    # contract address both transports without either side guessing aliases.
    return _send_tool(
        "vw_symbol",
        {
            "action": action,
            "symbol_name": symbol_name,
            "definition_name": symbol_name,
            "x": x,
            "y": y,
            "rotation": rotation,
            "rotation_deg": rotation,
        },
    )


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
    """Selection ops. For select, criteria is a VW criteria string.
    Delete of the current selection requires confirm='DELETE_SELECTED'.
    Criteria delete is restricted to exact-name criteria and requires confirm='DELETE_EXACT_NAME'."""
    if action == "delete":
        if criteria:
            if not _is_exact_name_criteria(criteria):
                return json.dumps(
                    {
                        "ok": False,
                        "blocked": True,
                        "blocked_action": "selection",
                        "reason": "unsafe_delete_criteria",
                        "message": "selection delete with criteria is restricted to exact object-name criteria like ((N='Name')).",
                    },
                    sort_keys=True,
                )
            if confirm != "DELETE_EXACT_NAME":
                return _confirmation_error(
                    "vw_selection",
                    "DELETE_EXACT_NAME",
                    "criteria-based selection delete is restricted to exact-name cleanup and requires explicit confirmation",
                )
        elif confirm != "DELETE_SELECTED":
            return _confirmation_error(
                "vw_selection",
                "DELETE_SELECTED",
                "selection delete is destructive and requires explicit confirmation",
            )
    return _send_tool("vw_selection", {"action": action, "criteria": criteria, "confirm": confirm, "limit": limit})


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


@_tool("vw_create_text")
def vw_create_text(
    text: str,
    x: float = 0,
    y: float = 0,
    width: float = 0,
    text_size: float = 0,
    rotation: float = 0,
    fixed_size: bool = False,
    wrap: bool = False,
    name: str = "",
    class_name: str = "A-Annotation",
) -> str:
    """Create a native Vectorworks text block. text_size is in page points; 0 keeps the document default."""
    return _send_tool(
        "vw_create_text",
        {
            "text": text,
            "x1": x,
            "y1": y,
            "width": width,
            "text_size": text_size,
            "rotation": rotation,
            "fixed_size": fixed_size,
            "wrap": wrap,
            "name": name,
            "class_name": class_name,
        },
    )


@_tool("vw_create_linear_dimension")
def vw_create_linear_dimension(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    offset: float = 300,
    text_offset: float = 0,
    direction_x: float = 0,
    direction_y: float = 0,
    dimension_type: Annotated[int, Field(ge=0, le=2)] = 1,
    name: str = "",
    class_name: str = "Dimension",
) -> str:
    """Create a native linear dimension between two points."""
    return _send_tool(
        "vw_create_linear_dimension",
        {
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
            "offset": offset,
            "text_offset": text_offset,
            "direction_x": direction_x,
            "direction_y": direction_y,
            "dimension_type": dimension_type,
            "name": name,
            "class_name": class_name,
        },
    )


@_tool("vw_insert_door")
def vw_insert_door(x: float, y: float, width: PositiveLength = 900, height: PositiveLength = 2100, rotation: float = 0) -> str:
    """Disabled compatibility entry; use vw_apply with a native hosted Door and exact wall UUID."""
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
    """Disabled compatibility entry; use vw_apply with a native hosted Window and exact wall UUID."""
    return _send_tool(
        "vw_insert_window",
        {"x": x, "y": y, "width": width, "height": height, "sill_height": sill_height, "rotation": rotation},
    )


@_tool("vw_create_slab")
def vw_create_slab(points: PolygonPointList, thickness: PositiveLength = 200, elevation: float = 0) -> str:
    """Disabled compatibility entry; use vw_apply with the native true-Slab SDK object type."""
    return _send_tool("vw_create_slab", {"points": points, "thickness": thickness, "elevation": elevation})


@_tool("vw_create_roof")
def vw_create_roof(
    points: PolygonPointList,
    bearing_height: float = 3000,
    slope: float = 30,
    overhang: float = 500,
    thickness: PositiveLength = 200,
) -> str:
    """Disabled compatibility entry; use vw_apply with the native true-Roof SDK object type."""
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
def vw_inspect_object(handle: str = "", plugin_name: str = "", confirm: str = "") -> str:
    """Inspect an existing object only; use vw_catalog for non-mutating native parametric schemas."""
    if plugin_name and confirm != "PROBE_PLUGIN":
        return _confirmation_error(
            "vw_inspect_object",
            "PROBE_PLUGIN",
            "plugin probing creates and deletes a temporary Vectorworks object and requires explicit confirmation",
        )
    return _send_tool("vw_inspect_object", {"handle": handle, "plugin_name": plugin_name, "confirm": confirm})


def _grouped_bridge_summary(status: Any) -> dict[str, Any]:
    if not isinstance(status, dict):
        return {}
    return {
        key: status.get(key)
        for key in (
            "version",
            "bridge_kind",
            "dispatch_mode",
            "native_bridge",
            "native_phase",
            "cad_api_safe",
            "transport_only",
            "main_context_pump_ready",
            "capability_revision",
            "capability_fingerprint",
        )
        if key in status
    }


def _grouped_finish(
    tool: str,
    action: str,
    payload: dict[str, Any],
    trace: dict[str, Any],
    outcome: str,
) -> str:
    result = {"tool": tool, "action": action, **payload}
    result["timing"] = _finish_request_trace(trace, outcome)
    _emit_request_trace(trace, result["timing"])
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _grouped_error(
    tool: str,
    action: str,
    code: str,
    message: str,
    trace: dict[str, Any],
    *,
    status: Any = None,
    required_action: str = "",
    detail: Any = None,
    commit_state: str = "",
) -> str:
    safety = TOOL_SAFETY.get(tool, {})
    action_param = safety.get("action_param")
    actions = safety.get("actions")
    if isinstance(action_param, str) and isinstance(actions, dict):
        variant = actions.get(action)
        if isinstance(variant, dict):
            safety = {**safety, **variant}

    retry_policy = str(safety.get("retryPolicy", "never_after_send"))
    retryable = retry_policy == "safe"
    writes_started: Optional[bool] = None
    if code in {
        "capability_unavailable",
        "capability_manifest_mismatch",
        "validation_error",
        "preflight_failed",
        "request_not_sent",
    }:
        writes_started = False
        commit_state = commit_state or "not_started"
    if code in {"preflight_failed", "capability_manifest_mismatch"}:
        retryable = True
        retry_policy = "after_preflight_repair"
    elif code == "request_not_sent":
        retryable = True
        retry_policy = "safe"
    elif code == "unknown_commit_state":
        retryable = False
        retry_policy = "never_after_send"
        commit_state = "unknown"

    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "retry_policy": retry_policy,
        "writes_started": writes_started,
    }
    if commit_state:
        error["commit_state"] = commit_state
    if required_action:
        error["required_native_action"] = required_action
    if detail is not None:
        error["detail"] = detail
    return _grouped_finish(
        tool,
        action,
        {
            "ok": False,
            "error": error,
            "bridge": _grouped_bridge_summary(status),
        },
        trace,
        code,
    )


def _grouped_preflight(
    tool: str,
    action: str,
    required_native_action: str,
    trace: dict[str, Any],
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    status, status_error = _fast_execution_bridge_status(trace)
    if status_error or not isinstance(status, dict):
        return status, _grouped_error(
            tool,
            action,
            "preflight_failed",
            "The phase-4 native bridge is not ready for this grouped call.",
            trace,
            status=status,
            required_action=required_native_action,
            detail=status_error or "bridge status unavailable",
        )
    implemented = status.get("implemented_actions")
    implemented_actions = set(implemented) if isinstance(implemented, list) else set()
    if required_native_action and required_native_action not in implemented_actions:
        return status, _grouped_error(
            tool,
            action,
            "capability_unavailable",
            "The native bridge does not advertise this action; no compatibility fallback was attempted.",
            trace,
            status=status,
            required_action=required_native_action,
            detail={"implemented_actions": sorted(str(item) for item in implemented_actions)},
        )
    return status, None


def _grouped_native_call(
    tool: str,
    action: str,
    native_action: str,
    params: Optional[dict[str, Any]],
    trace: dict[str, Any],
) -> tuple[Any, Optional[dict[str, Any]], Optional[str]]:
    status, preflight_error = _grouped_preflight(tool, action, native_action, trace)
    if preflight_error is not None:
        return None, status, preflight_error
    raw = _send(native_action, params, require_cad_safe=False, trace=trace)
    decoded = _decode_tool_result(raw)
    if _tool_result_failed(raw, decoded):
        error_text = decoded if isinstance(decoded, str) else raw
        if isinstance(error_text, str) and error_text.startswith("Unknown commit state"):
            return None, status, _grouped_error(
                tool,
                action,
                "unknown_commit_state",
                "Vectorworks accepted the non-retryable native action, but the host did not receive a reliable result.",
                trace,
                status=status,
                required_action=native_action,
                detail=decoded,
                commit_state="unknown",
            )
        if isinstance(error_text, str) and error_text.startswith("Request was not sent"):
            return None, status, _grouped_error(
                tool,
                action,
                "request_not_sent",
                "The native request did not cross the transport boundary and is safe to retry.",
                trace,
                status=status,
                required_action=native_action,
                detail=decoded,
                commit_state="not_started",
            )
        return None, status, _grouped_error(
            tool,
            action,
            "native_action_failed",
            "The advertised native action failed; no fallback was attempted.",
            trace,
            status=status,
            required_action=native_action,
            detail=decoded,
        )
    return decoded, status, None


def _grouped_page_offset(cursor: str) -> int:
    text = str(cursor or "").strip()
    if not text:
        return 0
    if not re.fullmatch(r"0|[1-9][0-9]{0,9}", text):
        raise ValueError("cursor must be empty or a non-negative decimal offset")
    return int(text)


def _grouped_project_record(record: Any, fields: list[str]) -> Any:
    if not fields or not isinstance(record, dict):
        return record
    return {field: record.get(field) for field in fields if field in record}


def _grouped_page_data(
    value: Any,
    *,
    limit: int,
    offset: int,
    fields: list[str],
) -> tuple[Any, Optional[dict[str, Any]]]:
    collection: Optional[list[Any]] = value if isinstance(value, list) else None
    collection_key = ""
    if isinstance(value, dict):
        for candidate in ("items", "objects", "layers", "classes", "symbols", "worksheets", "resources", "schemas"):
            if isinstance(value.get(candidate), list):
                collection = value[candidate]
                collection_key = candidate
                break
    if collection is None:
        return _grouped_project_record(value, fields), None

    page_items = [_grouped_project_record(item, fields) for item in collection[offset : offset + limit]]
    has_more = len(collection) > offset + limit
    page = {
        "limit": limit,
        "cursor": str(offset) if offset else "",
        "next_cursor": str(offset + limit) if has_more else None,
        "returned": len(page_items),
    }
    if collection_key and isinstance(value, dict):
        paged = dict(value)
        paged[collection_key] = page_items
        return paged, page
    return page_items, page


def _grouped_options(options: Optional[dict[str, Any]]) -> dict[str, Any]:
    if options is None:
        return {}
    if not isinstance(options, dict) or len(options) > 32:
        raise ValueError("options must be an object with at most 32 scalar entries")
    normalised: dict[str, Any] = {}
    for key, value in options.items():
        key = str(key)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key):
            raise ValueError(f"invalid option name: {key}")
        if value is not None and not isinstance(value, (str, bool, int, float)):
            raise ValueError(f"option '{key}' must be a JSON scalar")
        normalised[key] = value
    return normalised


def _grouped_io_native_action(action: str, format_name: str, file_path: str = "") -> str:
    canonical_format = str(format_name or "auto").lower()
    if canonical_format == "auto":
        extension = Path(str(file_path or "")).suffix.lower().lstrip(".")
        canonical_format = {"jpeg": "jpg", "tiff": "tif"}.get(extension, extension)
        if canonical_format not in {"dwg", "pdf", "vwx", "png", "jpg", "tif"}:
            raise ValueError(
                "format='auto' requires a .dwg, .pdf, .vwx, .png, .jpg/.jpeg, or .tif/.tiff file path"
            )
    if action == "import":
        return "import_dwg" if canonical_format == "dwg" else f"import_{canonical_format}"
    if action == "capture":
        return "capture_view"
    return {
        "dwg": "export_dwg",
        "pdf": "export_pdf",
        "vwx": "export_vectorworks_document",
        "image": "export_image",
        "png": "export_image",
        "jpg": "export_image",
        "jpeg": "export_image",
        "tif": "export_image",
        "tiff": "export_image",
    }[canonical_format]


@_tool("vw_status")
def vw_status(
    action: GroupedStatusAction = "context",
    limit: GroupedPageLimit = 100,
    include_examples: bool = False,
) -> str:
    """Native health or a compact phase-4 drawing context."""
    trace = _new_request_trace("vw_status", action)
    if action == "health":
        status, error = _grouped_preflight("vw_status", action, "", trace)
        if error is not None:
            return error
        return _grouped_finish(
            "vw_status",
            action,
            {"ok": True, "bridge": _grouped_bridge_summary(status)},
            trace,
            "ok",
        )

    data, status, error = _grouped_native_call(
        "vw_status",
        action,
        "drawing_summary",
        {
            "limit": limit,
            "scan_limit": limit,
            "include_examples": include_examples,
            "example_limit": min(limit, 5) if include_examples else 0,
        },
        trace,
    )
    if error is not None:
        return error
    return _grouped_finish(
        "vw_status",
        action,
        {"ok": True, "bridge": _grouped_bridge_summary(status), "data": data},
        trace,
        "ok",
    )


@_tool("vw_read")
def vw_read(
    action: GroupedReadAction,
    criteria: str = "ALL",
    layer: str = "",
    object_type: str = "",
    limit: GroupedPageLimit = 100,
    cursor: GroupedCursor = "",
    fields: Optional[ObjectFieldList] = None,
) -> str:
    """Read document, layers, summary, query, or selection with compact paging."""
    trace = _new_request_trace("vw_read", action)
    try:
        offset = _grouped_page_offset(cursor)
        projection = list(fields or [])
    except ValueError as exc:
        return _grouped_error("vw_read", action, "validation_error", str(exc), trace)

    requested = min(MAX_OBJECT_QUERY_LIMIT, offset + limit + 1)
    native_action, params = {
        "document": ("get_document_info", {}),
        "layers": ("get_layers", {}),
        "summary": (
            "drawing_summary",
            {
                "layer": layer,
                "object_type": object_type,
                "limit": requested,
                "scan_limit": requested,
                "include_examples": False,
                "example_limit": 0,
            },
        ),
        "query": ("find_objects", {"criteria": criteria or "ALL", "limit": requested}),
        "selection": ("selection", {"action": "get", "limit": requested}),
    }[action]
    if action == "query":
        params["layer"] = layer
        params["object_type"] = object_type
    data, status, error = _grouped_native_call("vw_read", action, native_action, params, trace)
    if error is not None:
        return error
    page: Optional[dict[str, Any]] = None
    if action in {"layers", "query", "selection"}:
        data, page = _grouped_page_data(data, limit=limit, offset=offset, fields=projection)
    elif projection:
        data = _grouped_project_record(data, projection)
    payload: dict[str, Any] = {
        "ok": True,
        "bridge": _grouped_bridge_summary(status),
        "data": data,
    }
    if page is not None:
        payload["page"] = page
    return _grouped_finish("vw_read", action, payload, trace, "ok")


@_tool("vw_catalog")
def vw_catalog(
    action: GroupedCatalogAction,
    query: str = "",
    limit: GroupedPageLimit = 100,
    cursor: GroupedCursor = "",
    fields: Optional[ObjectFieldList] = None,
) -> str:
    """List native capabilities, classes, symbols, schemas, worksheets, or resources."""
    trace = _new_request_trace("vw_catalog", action)
    try:
        offset = _grouped_page_offset(cursor)
        projection = list(fields or [])
    except ValueError as exc:
        return _grouped_error("vw_catalog", action, "validation_error", str(exc), trace)

    if action == "capabilities":
        capability_data, status, error = _grouped_native_call(
            "vw_catalog", action, "capabilities", {}, trace
        )
        if error is not None:
            return error
        capability_revision = (
            capability_data.get("capability_revision")
            if isinstance(capability_data, dict)
            else None
        )
        capability_fingerprint = (
            capability_data.get("capability_fingerprint")
            if isinstance(capability_data, dict)
            else None
        )
        if (
            capability_revision != status.get("capability_revision")
            or capability_fingerprint != status.get("capability_fingerprint")
        ):
            return _grouped_error(
                "vw_catalog",
                action,
                "capability_manifest_mismatch",
                "The ping and capability manifest identities differ. Restart or upgrade the native bridge before CAD work.",
                trace,
                status=status,
                required_action="capabilities",
                detail={
                    "status_revision": status.get("capability_revision"),
                    "manifest_revision": capability_revision,
                    "status_fingerprint": status.get("capability_fingerprint"),
                    "manifest_fingerprint": capability_fingerprint,
                },
            )
        data = {
            "capability_revision": capability_revision,
            "capability_fingerprint": capability_fingerprint,
            "native_phase": _native_phase(status or {}),
            "implemented_actions": sorted(status.get("implemented_actions") or []) if status else [],
            "capabilities": capability_data,
            "create_object_types": sorted(_native_create_object_types(status or {})),
        }
        return _grouped_finish(
            "vw_catalog",
            action,
            {"ok": True, "bridge": _grouped_bridge_summary(status), "data": data},
            trace,
            "ok",
        )

    native_action, params = {
        "classes": ("manage_classes", {"action": "list", "query": query}),
        "symbols": ("symbol", {"action": "list", "query": query, "limit": min(1000, offset + limit + 1)}),
        "parametric_schemas": ("describe_parametric_schema", {"plugin_name": query}),
        "worksheets": ("worksheet", {"action": "list", "query": query, "limit": min(1000, offset + limit + 1)}),
        "resources": ("resources", {"action": "list", "query": query, "limit": min(1000, offset + limit + 1)}),
    }[action]
    data, status, error = _grouped_native_call("vw_catalog", action, native_action, params, trace)
    if error is not None:
        return error
    data, page = _grouped_page_data(data, limit=limit, offset=offset, fields=projection)
    payload: dict[str, Any] = {"ok": True, "bridge": _grouped_bridge_summary(status), "data": data}
    if page is not None:
        payload["page"] = page
    return _grouped_finish("vw_catalog", action, payload, trace, "ok")


@_tool("vw_apply")
def vw_apply(operations: ExecuteOperationList, idempotency_key: IdempotencyKey) -> str:
    """Apply one canonical atomic native mutation plan; never decomposes or falls back."""
    raw = vw_execute_operations(operations, idempotency_key)
    decoded = _decode_tool_result(raw)
    if not isinstance(decoded, dict):
        return json.dumps(
            {
                "ok": False,
                "tool": "vw_apply",
                "action": "apply",
                "error": {
                    "code": "native_action_failed",
                    "message": "The atomic native operation failed; no fallback was attempted.",
                    "detail": decoded,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    payload = dict(decoded)
    if payload.get("ok") is False:
        original_error = payload.get("error")
        error_text = str(original_error or "atomic native operation failed")
        execution_outcome = str((payload.get("timing") or {}).get("outcome", ""))
        if execution_outcome == "unsupported" or payload.get("unsupported_object_types"):
            code = "capability_unavailable"
        elif execution_outcome in {"validation_error", "idempotency_conflict"}:
            code = "validation_error"
        elif execution_outcome == "preflight_error":
            code = "preflight_failed"
        elif "does not advertise required phase-4 action" in error_text or "requires the native SDK bridge" in error_text:
            code = "capability_unavailable"
        elif "preflight failed" in error_text.lower():
            code = "preflight_failed"
        elif "idempotency_key" in error_text or "must " in error_text:
            code = "validation_error"
        else:
            code = "native_action_failed"
        payload["error"] = {
            "code": code,
            "message": error_text,
            "required_native_action": "apply_operations",
            "writes_started": False if code in {"capability_unavailable", "preflight_failed", "validation_error"} else None,
        }
    payload["delegated_tool"] = "vw_execute_operations"
    payload["tool"] = "vw_apply"
    payload["action"] = "apply"
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@_tool("vw_io")
def vw_io(
    action: GroupedIOAction,
    file_path: NonEmptyPath,
    format: GroupedIOFormat = "auto",
    options: Optional[dict[str, Any]] = None,
) -> str:
    """Run an advertised native import, export, or capture without automatic retry."""
    trace = _new_request_trace("vw_io", action)
    try:
        native_action = _grouped_io_native_action(action, format, file_path)
        params = {"file_path": file_path, "format": format, **_grouped_options(options)}
    except (KeyError, ValueError) as exc:
        return _grouped_error("vw_io", action, "validation_error", str(exc), trace)
    data, status, error = _grouped_native_call("vw_io", action, native_action, params, trace)
    if error is not None:
        return error
    return _grouped_finish(
        "vw_io",
        action,
        {"ok": True, "bridge": _grouped_bridge_summary(status), "data": data},
        trace,
        "ok",
    )


@_tool("vw_view")
def vw_view(
    action: GroupedViewAction,
    file_path: str = "",
    options: Optional[dict[str, Any]] = None,
) -> str:
    """Get/set the active view or capture it through advertised native actions."""
    trace = _new_request_trace("vw_view", action)
    try:
        params = {"file_path": file_path, **_grouped_options(options)}
    except ValueError as exc:
        return _grouped_error("vw_view", action, "validation_error", str(exc), trace)
    native_action = {"get": "get_view", "set": "set_view", "capture": "capture_view"}[action]
    data, status, error = _grouped_native_call("vw_view", action, native_action, params, trace)
    if error is not None:
        return error
    return _grouped_finish(
        "vw_view",
        action,
        {"ok": True, "bridge": _grouped_bridge_summary(status), "data": data},
        trace,
        "ok",
    )


@_tool("vw_document")
def vw_document(
    action: GroupedDocumentAction,
    file_path: str = "",
    format: GroupedIOFormat = "auto",
    options: Optional[dict[str, Any]] = None,
) -> str:
    """Inspect or change a document through an advertised native action without automatic retry."""
    trace = _new_request_trace("vw_document", action)
    try:
        params = {"file_path": file_path, "format": format, **_grouped_options(options)}
        if action == "export":
            native_action = _grouped_io_native_action("export", format, file_path)
        else:
            native_action = {
                "info": "get_document_info",
                "save": "save_document",
                "open": "open_document",
                "new": "new_document",
            }[action]
    except (KeyError, ValueError) as exc:
        return _grouped_error("vw_document", action, "validation_error", str(exc), trace)
    data, status, error = _grouped_native_call("vw_document", action, native_action, params, trace)
    if error is not None:
        return error
    if action == "open" and isinstance(data, dict) and data.get("commit_state") == "accepted":
        requested_path = os.path.normcase(os.path.abspath(file_path))
        deadline = time.monotonic() + 45.0
        last_readback: Any = None
        _clear_operation_idempotency_cache()
        while time.monotonic() < deadline:
            _clear_cad_safe_cache()
            raw_readback = _send("get_document_info", {}, require_cad_safe=False, trace=trace)
            decoded_readback = _decode_tool_result(raw_readback)
            last_readback = decoded_readback
            if not _tool_result_failed(raw_readback, decoded_readback) and isinstance(decoded_readback, dict):
                active_path = str(decoded_readback.get("filepath", "") or "")
                if active_path and os.path.normcase(os.path.abspath(active_path)) == requested_path:
                    data = {
                        **data,
                        "active_path": active_path,
                        "commit_state": "committed",
                        "readback": decoded_readback,
                    }
                    break
            time.sleep(0.2)
        else:
            return _grouped_error(
                "vw_document",
                action,
                "unknown_commit_state",
                "Vectorworks accepted the deferred open, but the requested document was not confirmed by readback.",
                trace,
                status=status,
                required_action="open_document",
                detail={"accepted": data, "last_readback": last_readback},
                commit_state="unknown",
            )
    return _grouped_finish(
        "vw_document",
        action,
        {"ok": True, "bridge": _grouped_bridge_summary(status), "data": data},
        trace,
        "ok",
    )


def _apply_tool_profile() -> str:
    """Apply the startup-only MCP tool profile and return its canonical name."""
    global _tool_profile_applied
    profile = _configured_tool_profile()
    if profile not in _SUPPORTED_TOOL_PROFILES:
        raise ConfigError(
            "VW_MCP_TOOL_PROFILE must be one of: {0}".format(
                ", ".join(sorted(_SUPPORTED_TOOL_PROFILES))
            )
        )
    if _tool_profile_applied:
        return profile
    if profile == "fast-native":
        unknown = sorted(FAST_NATIVE_TOOL_NAMES - set(TOOL_SAFETY))
        if unknown:
            raise ConfigError(
                "fast-native profile references unknown tools: {0}".format(", ".join(unknown))
            )
        for tool_name in sorted(set(TOOL_SAFETY) - FAST_NATIVE_TOOL_NAMES):
            mcp.local_provider.remove_tool(tool_name)
    _tool_profile_applied = True
    return profile


def main() -> int:
    if _CONFIG_ERROR:
        print(f"Vectorworks MCP configuration error: {_CONFIG_ERROR}", file=sys.stderr)
        return 2
    try:
        _apply_tool_profile()
        mcp.run(transport="stdio", show_banner=False)
        return 0
    except (ConfigError, RuntimeError) as exc:
        print(f"Vectorworks MCP startup error: {exc}", file=sys.stderr)
        return 1
    finally:
        _close()


if __name__ == "__main__":
    raise SystemExit(main())
