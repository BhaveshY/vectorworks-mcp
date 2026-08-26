"""Run the destructive end-to-end acceptance fixture through MCP only.

This script deliberately has no GUI automation and no Python-listener fallback.
It requires an explicit disposable Vectorworks document and output directory. By
default, the disposable document must already be active and contain no objects.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "vw_status",
    "vw_read",
    "vw_catalog",
    "vw_apply",
    "vw_execute_operations",
    "vw_io",
    "vw_view",
    "vw_document",
    "vw_tool_safety",
}
EXPECTED_NATIVE_ACTIONS = {
    "ping",
    "stop",
    "capabilities",
    "describe_parametric_schema",
    "export_image",
    "capture_view",
    "export_pdf",
    "export_vectorworks_document",
    "import_dwg",
    "export_dwg",
    "resources",
    "symbol",
    "worksheet",
    "get_view",
    "set_view",
    "save_document",
    "open_document",
    "get_document_info",
    "get_layers",
    "get_objects",
    "selection",
    "create_object",
    "batch_create_objects",
    "create_wall",
    "create_text",
    "create_linear_dimension",
    "set_property",
    "manage_classes",
    "find_objects",
    "drawing_summary",
    "apply_operations",
}
EXPECTED_CREATE_TYPES = {
    "arc",
    "box",
    "circle",
    "line",
    "oval",
    "polygon",
    "polyline",
    "rect",
    "rectangle",
    "wall",
    "door",
    "window",
    "text",
    "dimension",
    "linear_dimension",
    "slab",
    "roof",
    "space",
    "parametric",
    "symbol",
}
EXPECTED_APPLY_OPERATION_TYPES = {
    "create",
    "transform",
    "duplicate",
    "set_properties",
    "delete",
}

# Vectorworks object names are limited to 63 characters. Keep the run prefix
# short enough for the longest acceptance suffix used below so exact-name
# readback remains a valid verifier instead of observing a truncated name.
VECTORWORKS_OBJECT_NAME_LIMIT = 63
LONGEST_FIXTURE_OBJECT_SUFFIX = "_TYPE_LINEAR_DIMENSION"


def fixture_prefix(run_id: str) -> str:
    prefix = f"VW_MCP_P4_{run_id}"
    if len(prefix + LONGEST_FIXTURE_OBJECT_SUFFIX) > VECTORWORKS_OBJECT_NAME_LIMIT:
        raise ValueError("acceptance fixture prefix exceeds the Vectorworks object-name limit")
    return prefix


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the full native Vectorworks MCP acceptance fixture. The fixture "
            "creates BIM objects and must only target a disposable document."
        )
    )
    parser.add_argument(
        "--source-document",
        required=True,
        type=Path,
        help="Existing disposable .vwx file to verify and modify in memory.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Directory for the saved VWX, PNG files, and JSON report.",
    )
    parser.add_argument(
        "--allow-write-fixture",
        required=True,
        action="store_true",
        help="Required acknowledgement that the disposable document will be modified.",
    )
    parser.add_argument(
        "--open-document",
        action="store_true",
        help=(
            "Open --source-document when it is not already active. The command does "
            "not authorize discarding an unsaved active document; Vectorworks must "
            "accept the open operation without a dirty-document override."
        ),
    )
    parser.add_argument(
        "--allow-nonempty-source",
        action="store_true",
        help="Permit a disposable source that already contains objects.",
    )
    parser.add_argument(
        "--auth-token-file",
        type=Path,
        default=Path.home() / ".vectorworks-mcp" / "auth-token",
        help="Authentication token file path. Its contents are never logged.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9877)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    source = args.source_document.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    token_file = args.auth_token_file.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".vwx":
        raise ValueError("--source-document must be an existing .vwx file")
    if not token_file.is_file():
        raise ValueError(f"authentication token file does not exist: {token_file}")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the production connector must use a loopback host")
    if not 1 <= args.port <= 65535:
        raise ValueError("--port must be between 1 and 65535")
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    return source, output_dir, token_file


def payload_data(payload: dict[str, Any]) -> Any:
    return payload.get("data") if isinstance(payload, dict) else None


def collection(payload: dict[str, Any], key: str = "objects") -> list[dict[str, Any]]:
    data = payload_data(payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return data[key]
    return []


def summary_total(payload: dict[str, Any]) -> int | None:
    data = payload_data(payload)
    if not isinstance(data, dict):
        return None
    document = data.get("document")
    if isinstance(document, dict) and isinstance(document.get("total_objects"), int):
        return document["total_objects"]
    value = data.get("objects_scanned")
    return value if isinstance(value, int) else None


def require_exactly_one(payload: dict[str, Any], description: str) -> dict[str, Any]:
    objects = collection(payload)
    if len(objects) != 1:
        raise RuntimeError(f"{description} was not found exactly once")
    return objects[0]


def first_receipt_uuid(payload: dict[str, Any], description: str) -> str:
    verification = payload.get("verification")
    receipts = verification.get("receipts") if isinstance(verification, dict) else None
    if not isinstance(receipts, list):
        raise RuntimeError(f"{description} omitted native operation receipts")
    for receipt in receipts:
        if isinstance(receipt, dict) and receipt.get("uuid"):
            return str(receipt["uuid"])
    raise RuntimeError(f"{description} omitted its created-object UUID")


async def run_acceptance(
    args: argparse.Namespace,
    source: Path,
    output_dir: Path,
    token_file: Path,
    report: dict[str, Any],
) -> None:
    run_id = report["run_id"]
    prefix = report["prefix"]
    env = os.environ.copy()
    env.update(
        {
            "VW_MCP_HOST": args.host,
            "VW_MCP_PORT": str(args.port),
            "VW_MCP_TOOL_PROFILE": "fast-native",
            "VW_MCP_AUTH_TOKEN_FILE": str(token_file),
        }
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "server.py")],
        cwd=ROOT,
        env=env,
    )

    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=args.timeout_seconds),
            ) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                tool_names = sorted(tool.name for tool in listed.tools)
                if set(tool_names) != EXPECTED_TOOLS:
                    raise RuntimeError(f"unexpected MCP tool surface: {tool_names}")
                report["server"] = {
                    "name": initialized.serverInfo.name,
                    "version": initialized.serverInfo.version,
                    "tools": tool_names,
                }

                async def call(
                    tool: str,
                    arguments: dict[str, Any],
                    *,
                    expect_ok: bool = True,
                    label: str | None = None,
                ) -> dict[str, Any]:
                    started = time.perf_counter()
                    result = await session.call_tool(tool, arguments)
                    key = label or f"{tool}:{arguments.get('action', 'call')}"
                    report["timings_ms"][key] = round(
                        (time.perf_counter() - started) * 1000.0, 3
                    )
                    payload = result.structuredContent or {}
                    ok = (
                        isinstance(payload, dict)
                        and not result.isError
                        and payload.get("ok") is not False
                    )
                    if expect_ok and not ok:
                        excerpt = json.dumps(payload, ensure_ascii=False)[:4000]
                        raise RuntimeError(f"{key} failed: {excerpt}")
                    if not expect_ok and ok:
                        raise RuntimeError(f"{key} unexpectedly succeeded")
                    return payload

                async def query_named(name: str, *, label: str) -> dict[str, Any]:
                    return await call(
                        "vw_read",
                        {
                            "action": "query",
                            "criteria": f"((N='{name}'))",
                            "limit": 20,
                        },
                        label=label,
                    )

                health = await call("vw_status", {"action": "health"}, label="health")
                bridge = health.get("bridge", {})
                required_bridge = {
                    "native_bridge": True,
                    "native_phase": 4,
                    "capability_revision": 4,
                    "cad_api_safe": True,
                    "transport_only": False,
                    "main_context_pump_ready": True,
                }
                for key, expected in required_bridge.items():
                    if bridge.get(key) != expected:
                        raise RuntimeError(
                            f"bridge preflight mismatch for {key}: {bridge.get(key)!r}"
                        )
                if not bridge.get("capability_fingerprint"):
                    raise RuntimeError("bridge did not report a capability fingerprint")
                report["bridge"] = bridge

                capabilities = await call(
                    "vw_catalog", {"action": "capabilities"}, label="capabilities"
                )
                cap_data = payload_data(capabilities) or {}
                if cap_data.get("capability_fingerprint") != bridge.get(
                    "capability_fingerprint"
                ):
                    raise RuntimeError("health and manifest fingerprints differ")
                advertised_actions = set(cap_data.get("implemented_actions") or [])
                advertised_types = set(cap_data.get("create_object_types") or [])
                if advertised_actions != EXPECTED_NATIVE_ACTIONS:
                    raise RuntimeError(
                        "native action manifest mismatch: missing={0}, unexpected={1}".format(
                            sorted(EXPECTED_NATIVE_ACTIONS - advertised_actions),
                            sorted(advertised_actions - EXPECTED_NATIVE_ACTIONS),
                        )
                    )
                if advertised_types != EXPECTED_CREATE_TYPES:
                    raise RuntimeError(
                        "create-type manifest mismatch: missing={0}, unexpected={1}".format(
                            sorted(EXPECTED_CREATE_TYPES - advertised_types),
                            sorted(advertised_types - EXPECTED_CREATE_TYPES),
                        )
                    )
                report["advertised_native_actions"] = sorted(advertised_actions)
                report["advertised_create_types"] = sorted(advertised_types)

                active = await call(
                    "vw_read", {"action": "document"}, label="document_before_open"
                )
                active_data = payload_data(active) or {}
                active_path_text = str(active_data.get("filepath", "")).strip()
                active_matches = bool(active_path_text) and (
                    Path(active_path_text).resolve() == source
                )
                if not active_matches:
                    if not args.open_document:
                        raise RuntimeError(
                            "the disposable source is not active; open it manually or pass "
                            "--open-document after saving/closing any active work"
                        )
                    await call(
                        "vw_document",
                        {
                            "action": "open",
                            "file_path": str(source),
                            "options": {
                                "replace_dirty_confirmation": "REPLACE_DIRTY_DOCUMENT"
                            },
                        },
                        label="document_open",
                    )

                document = await call(
                    "vw_read", {"action": "document"}, label="document_after_open"
                )
                document_data = payload_data(document) or {}
                if Path(str(document_data.get("filepath", ""))).resolve() != source:
                    raise RuntimeError("active document does not match --source-document")
                if int(document_data.get("layer_count", 0)) < 1:
                    raise RuntimeError("disposable document has no design layer")

                baseline = await call(
                    "vw_read", {"action": "summary", "limit": 200}, label="baseline_summary"
                )
                baseline_total = summary_total(baseline)
                if baseline_total is None:
                    raise RuntimeError("baseline summary omitted total object count")
                if baseline_total != 0 and not args.allow_nonempty_source:
                    raise RuntimeError(
                        f"disposable source contains {baseline_total} objects; use an empty "
                        "document or explicitly pass --allow-nonempty-source"
                    )

                door_schema = await call(
                    "vw_catalog",
                    {"action": "parametric_schemas", "query": "Door"},
                    label="door_schema",
                )
                window_schema = await call(
                    "vw_catalog",
                    {"action": "parametric_schemas", "query": "Window"},
                    label="window_schema",
                )
                generic_parametric_schema = await call(
                    "vw_catalog",
                    {"action": "parametric_schemas", "query": "Stake Object"},
                    label="generic_parametric_schema",
                )
                door_fingerprint = str(
                    (payload_data(door_schema) or {}).get("descriptor_fingerprint", "")
                )
                window_fingerprint = str(
                    (payload_data(window_schema) or {}).get("descriptor_fingerprint", "")
                )
                generic_parametric_fingerprint = str(
                    (payload_data(generic_parametric_schema) or {}).get(
                        "descriptor_fingerprint", ""
                    )
                )
                if (
                    not door_fingerprint
                    or not window_fingerprint
                    or not generic_parametric_fingerprint
                ):
                    raise RuntimeError(
                        "Door/Window/Stake Object schema discovery omitted fingerprints"
                    )

                symbol_catalog = await call(
                    "vw_catalog",
                    {"action": "symbols", "limit": 100},
                    label="symbol_catalog_for_create_type",
                )
                symbol_records = collection(symbol_catalog, "symbols")
                if not symbol_records or not symbol_records[0].get("name"):
                    raise RuntimeError(
                        "strict full acceptance requires one symbol definition in the disposable source"
                    )
                symbol_definition_name = str(symbol_records[0]["name"])

                rollback_name = f"{prefix}_ROLLBACK_SPACE"
                rollback_payload = await call(
                    "vw_execute_operations",
                    {
                        "operations": [
                            {
                                "type": "create",
                                "operation_id": "rollback-space",
                                "params": {
                                    "object_type": "space",
                                    "points": [
                                        [12000, 0],
                                        [14000, 0],
                                        [14000, 2000],
                                        [12000, 2000],
                                    ],
                                    "closed": True,
                                    "height": 3000,
                                    "name": rollback_name,
                                    "room_id": "ROLLBACK",
                                },
                            },
                            {
                                "type": "create",
                                "operation_id": "forced-failure",
                                "params": {
                                    "object_type": "parametric",
                                    "plugin_name": "__VW_MCP_INTENTIONAL_MISSING_PLUGIN__",
                                    "descriptor_fingerprint": "intentional-mismatch",
                                    "x": 13000,
                                    "y": 1000,
                                },
                            },
                        ],
                        "idempotency_key": f"{prefix}-rollback",
                    },
                    expect_ok=False,
                    label="compound_rollback",
                )
                after_rollback = await call(
                    "vw_read",
                    {"action": "summary", "limit": 200},
                    label="after_rollback_summary",
                )
                if summary_total(after_rollback) != baseline_total:
                    raise RuntimeError("compound rollback changed the object count")
                rollback_query = await call(
                    "vw_read",
                    {
                        "action": "query",
                        "criteria": f"((N='{rollback_name}'))",
                        "limit": 20,
                    },
                    label="rollback_name_query",
                )
                if collection(rollback_query):
                    raise RuntimeError("compound rollback left its Space behind")
                report["checks"]["compound_rollback"] = {
                    "passed": True,
                    "baseline_total": baseline_total,
                    "after_total": summary_total(after_rollback),
                    "error": rollback_payload.get("error"),
                }

                wall_specs = [
                    ("WALL_SOUTH", 0, 0, 10000, 0),
                    ("WALL_EAST", 10000, 0, 10000, 7000),
                    ("WALL_NORTH", 10000, 7000, 0, 7000),
                    ("WALL_WEST", 0, 7000, 0, 0),
                    ("WALL_HALL_BED", 0, 4000, 10000, 4000),
                    ("WALL_BED_DIVIDER", 5000, 4000, 5000, 7000),
                    ("WALL_KITCHEN", 6000, 0, 6000, 4000),
                    ("WALL_SERVICE", 6000, 3000, 10000, 3000),
                    ("WALL_BATH", 8000, 3000, 8000, 4000),
                ]
                room_specs = [
                    (
                        "LIVING_DINING",
                        "Living / Dining",
                        "LIVING",
                        [[200, 200], [5800, 200], [5800, 3800], [200, 3800]],
                    ),
                    (
                        "BEDROOM_1",
                        "Bedroom 1",
                        "BED-01",
                        [[200, 4200], [4800, 4200], [4800, 6800], [200, 6800]],
                    ),
                    (
                        "BEDROOM_2",
                        "Bedroom 2",
                        "BED-02",
                        [[5200, 4200], [9800, 4200], [9800, 6800], [5200, 6800]],
                    ),
                    (
                        "KITCHEN",
                        "Kitchen",
                        "KITCHEN",
                        [[6200, 200], [9800, 200], [9800, 2800], [6200, 2800]],
                    ),
                    (
                        "BATH",
                        "Bath",
                        "BATH",
                        [[6200, 3200], [7800, 3200], [7800, 3800], [6200, 3800]],
                    ),
                    (
                        "PASSAGE",
                        "Passage",
                        "PASSAGE",
                        [[8200, 3200], [9800, 3200], [9800, 3800], [8200, 3800]],
                    ),
                ]
                operations: list[dict[str, Any]] = []
                for suffix, x1, y1, x2, y2 in wall_specs:
                    operations.append(
                        {
                            "type": "create",
                            "operation_id": suffix.lower(),
                            "params": {
                                "object_type": "wall",
                                "x1": x1,
                                "y1": y1,
                                "x2": x2,
                                "y2": y2,
                                "height": 3000,
                                "thickness": 200,
                                "name": f"{prefix}_{suffix}",
                            },
                        }
                    )
                for suffix, display_name, room_id, points in room_specs:
                    operations.append(
                        {
                            "type": "create",
                            "operation_id": suffix.lower(),
                            "params": {
                                "object_type": "space",
                                "points": points,
                                "closed": True,
                                "height": 3000,
                                "name": f"{prefix}_{suffix}",
                                "room_id": room_id,
                            },
                        }
                    )
                    cx = sum(point[0] for point in points) / len(points)
                    cy = sum(point[1] for point in points) / len(points)
                    operations.append(
                        {
                            "type": "create",
                            "operation_id": f"label-{suffix.lower()}",
                            "params": {
                                "object_type": "text",
                                "x": cx,
                                "y": cy,
                                "text": display_name,
                                "text_size": 14,
                                "name": f"{prefix}_LABEL_{suffix}",
                            },
                        }
                    )
                operations.extend(
                    [
                        {
                            "type": "create",
                            "operation_id": "floor-slab",
                            "params": {
                                "object_type": "slab",
                                "points": [[0, 0], [10000, 0], [10000, 7000], [0, 7000]],
                                "closed": True,
                                "thickness": 200,
                                "elevation": -200,
                                "name": f"{prefix}_SLAB",
                            },
                        },
                        {
                            "type": "create",
                            "operation_id": "roof",
                            "params": {
                                "object_type": "roof",
                                "points": [[0, 0], [10000, 0], [10000, 7000], [0, 7000]],
                                "closed": True,
                                "thickness": 200,
                                "bearing_height": 3000,
                                "slope": 25,
                                "overhang": 500,
                                "name": f"{prefix}_ROOF",
                            },
                        },
                        {
                            "type": "create",
                            "operation_id": "dim-width",
                            "params": {
                                "object_type": "linear_dimension",
                                "x1": 0,
                                "y1": 0,
                                "x2": 10000,
                                "y2": 0,
                                "offset": -700,
                                "name": f"{prefix}_DIM_WIDTH",
                            },
                        },
                        {
                            "type": "create",
                            "operation_id": "dim-depth",
                            "params": {
                                "object_type": "linear_dimension",
                                "x1": 10000,
                                "y1": 0,
                                "x2": 10000,
                                "y2": 7000,
                                "offset": 700,
                                "name": f"{prefix}_DIM_DEPTH",
                            },
                        },
                        {
                            "type": "create",
                            "operation_id": "marker",
                            "params": {
                                "object_type": "polygon",
                                "points": [[11000, 0], [11500, 0], [11500, 500], [11000, 500]],
                                "closed": True,
                                "name": f"{prefix}_MARKER",
                            },
                        },
                        {
                            "type": "transform",
                            "params": {
                                "target": "$marker",
                                "dx": 250,
                                "dy": 250,
                                "rotation_deg": 30,
                                "scale_x": 1.2,
                                "scale_y": 1.2,
                            },
                        },
                        {
                            "type": "duplicate",
                            "operation_id": "marker-copy",
                            "params": {"target": "$marker", "dx": 1000, "dy": 0},
                        },
                        {
                            "type": "set_properties",
                            "params": {
                                "edits": [
                                    {
                                        "ref": "$marker-copy",
                                        "properties": {"name": f"{prefix}_MARKER_COPY"},
                                    }
                                ]
                            },
                        },
                    ]
                )
                main_args = {
                    "operations": operations,
                    "idempotency_key": f"{prefix}-main",
                }
                main_payload = await call("vw_apply", main_args, label="main_2bhk_apply")
                replay_payload = await call("vw_apply", main_args, label="main_2bhk_replay")
                if not replay_payload.get("idempotency_replay"):
                    raise RuntimeError("identical idempotent replay was not recognized")
                await call(
                    "vw_apply",
                    {
                        "operations": [
                            {
                                "type": "create",
                                "operation_id": "conflict",
                                "params": {
                                    "object_type": "rect",
                                    "x1": 0,
                                    "y1": 0,
                                    "x2": 10,
                                    "y2": 10,
                                },
                            }
                        ],
                        "idempotency_key": f"{prefix}-main",
                    },
                    expect_ok=False,
                    label="idempotency_conflict",
                )

                south_wall = await call(
                    "vw_read",
                    {
                        "action": "query",
                        "criteria": f"((N='{prefix}_WALL_SOUTH'))",
                        "object_type": "wall",
                        "limit": 20,
                    },
                    label="south_wall_query",
                )
                wall = require_exactly_one(south_wall, "south wall")
                wall_uuid = wall.get("uuid")
                if not wall_uuid:
                    raise RuntimeError("south wall readback omitted its UUID")

                strict_class_name = f"{prefix}_CLASS"
                simple_type_specs: list[tuple[str, dict[str, Any], str]] = [
                    (
                        "arc",
                        {"x1": 30000, "y1": 10000, "radius": 500, "start_angle": 15, "sweep_angle": 120},
                        "arc",
                    ),
                    (
                        "box",
                        {"x1": 32000, "y1": 9500, "x2": 33000, "y2": 10500},
                        "rect",
                    ),
                    (
                        "circle",
                        {"x1": 34500, "y1": 10000, "radius": 500},
                        "oval",
                    ),
                    (
                        "line",
                        {"x1": 36000, "y1": 9500, "x2": 37000, "y2": 10500},
                        "line",
                    ),
                    (
                        "oval",
                        {"x1": 38000, "y1": 9500, "x2": 39500, "y2": 10500},
                        "oval",
                    ),
                    (
                        "polygon",
                        {"points": [[40500, 9500], [41500, 9500], [41250, 10500]], "closed": True},
                        "polygon",
                    ),
                    (
                        "polyline",
                        {"points": [[42500, 9500], [43500, 10000], [42500, 10500]], "closed": False},
                        "polygon",
                    ),
                    (
                        "rect",
                        {
                            "x1": 44500,
                            "y1": 9500,
                            "x2": 45500,
                            "y2": 10500,
                            "class_name": strict_class_name,
                        },
                        "rect",
                    ),
                    (
                        "rectangle",
                        {"x1": 46500, "y1": 9500, "x2": 47500, "y2": 10500},
                        "rect",
                    ),
                    (
                        "wall",
                        {"x1": 48500, "y1": 9500, "x2": 50500, "y2": 9500, "height": 3000, "thickness": 200},
                        "wall",
                    ),
                    (
                        "text",
                        {"x": 51500, "y": 10000, "text": "Native text type", "text_size": 12},
                        "text",
                    ),
                    (
                        "dimension",
                        {"x1": 53000, "y1": 9500, "x2": 54500, "y2": 9500, "offset": 400},
                        "dimension",
                    ),
                    (
                        "linear_dimension",
                        {"x1": 55500, "y1": 9500, "x2": 57000, "y2": 9500, "offset": 400},
                        "dimension",
                    ),
                    (
                        "slab",
                        {
                            "points": [[58000, 9500], [59400, 9500], [59400, 10600], [58000, 10600]],
                            "closed": True,
                            "thickness": 180,
                            "elevation": -180,
                        },
                        "parametric",
                    ),
                    (
                        "roof",
                        {
                            "points": [[60500, 9500], [61900, 9500], [61900, 10600], [60500, 10600]],
                            "closed": True,
                            "thickness": 180,
                            "bearing_height": 3000,
                            "slope": 20,
                            "overhang": 250,
                        },
                        "roof",
                    ),
                    (
                        "space",
                        {
                            "points": [[63000, 9500], [64400, 9500], [64400, 10600], [63000, 10600]],
                            "closed": True,
                            "height": 3000,
                            "room_id": "TYPE-SPACE",
                        },
                        "parametric",
                    ),
                    (
                        "parametric",
                        {
                            "plugin_name": "Stake Object",
                            "descriptor_fingerprint": generic_parametric_fingerprint,
                            "x": 65500,
                            "y": 10000,
                        },
                        "parametric",
                    ),
                    (
                        "symbol",
                        {
                            "definition_name": symbol_definition_name,
                            "x": 67500,
                            "y": 10000,
                            "rotation": 12,
                        },
                        "symbol",
                    ),
                ]
                individual_type_results: dict[str, dict[str, Any]] = {}
                individual_type_objects: dict[str, dict[str, Any]] = {}
                for object_type, raw_params, native_type in simple_type_specs:
                    object_name = f"{prefix}_TYPE_{object_type.upper()}"
                    create_params = {
                        "object_type": object_type,
                        "name": object_name,
                        **raw_params,
                    }
                    create_payload = await call(
                        "vw_apply",
                        {
                            "operations": [
                                {
                                    "type": "create",
                                    "operation_id": f"type-{object_type}",
                                    "params": create_params,
                                }
                            ],
                            "idempotency_key": f"{prefix}-type-{object_type}",
                        },
                        label=f"create_type_{object_type}",
                    )
                    created_query = await query_named(
                        object_name,
                        label=f"verify_type_{object_type}",
                    )
                    created_object = require_exactly_one(
                        created_query, f"individually created {object_type}"
                    )
                    if created_object.get("type") != native_type:
                        raise RuntimeError(
                            f"{object_type} read back as {created_object.get('type')!r}, "
                            f"expected native type {native_type!r}"
                        )
                    individual_type_results[object_type] = create_payload
                    individual_type_objects[object_type] = created_object

                door_name = f"{prefix}_TYPE_DOOR"
                door_payload = await call(
                    "vw_apply",
                    {
                        "operations": [
                            {
                                "type": "create",
                                "operation_id": "type-door",
                                "params": {
                                    "object_type": "door",
                                    "plugin_name": "Door",
                                    "descriptor_fingerprint": door_fingerprint,
                                    "wall_uuid": wall_uuid,
                                    "x": 1000,
                                    "y": 0,
                                    "rotation": 0,
                                    "width": 900,
                                    "height": 2100,
                                    "name": door_name,
                                },
                            }
                        ],
                        "idempotency_key": f"{prefix}-type-door",
                    },
                    label="create_type_door_hosted",
                )
                window_name = f"{prefix}_TYPE_WINDOW"
                window_payload = await call(
                    "vw_apply",
                    {
                        "operations": [
                            {
                                "type": "create",
                                "operation_id": "type-window",
                                "params": {
                                    "object_type": "window",
                                    "plugin_name": "Window",
                                    "descriptor_fingerprint": window_fingerprint,
                                    "wall_uuid": wall_uuid,
                                    "x": 3500,
                                    "y": 0,
                                    "rotation": 0,
                                    "width": 1500,
                                    "height": 1200,
                                    "sill_height": 900,
                                    "name": window_name,
                                },
                            },
                        ],
                        "idempotency_key": f"{prefix}-type-window",
                    },
                    label="create_type_window_hosted",
                )
                for object_type, object_name, payload in (
                    ("door", door_name, door_payload),
                    ("window", window_name, window_payload),
                ):
                    created = require_exactly_one(
                        await query_named(object_name, label=f"verify_type_{object_type}"),
                        f"hosted {object_type}",
                    )
                    if created.get("type") != "parametric":
                        raise RuntimeError(f"hosted {object_type} is not a parametric node")
                    individual_type_results[object_type] = payload
                    individual_type_objects[object_type] = created
                if set(individual_type_results) != EXPECTED_CREATE_TYPES:
                    raise RuntimeError(
                        "individual create-type execution mismatch: missing={0}, unexpected={1}".format(
                            sorted(EXPECTED_CREATE_TYPES - set(individual_type_results)),
                            sorted(set(individual_type_results) - EXPECTED_CREATE_TYPES),
                        )
                    )

                operation_coverage: dict[str, dict[str, Any]] = {
                    "create": individual_type_results["rect"]
                }
                rect_object = individual_type_objects["rect"]
                rect_uuid = str(rect_object.get("uuid") or "")
                if not rect_uuid:
                    raise RuntimeError("individual rect omitted its UUID")
                transform_payload = await call(
                    "vw_apply",
                    {
                        "operations": [
                            {
                                "type": "transform",
                                "params": {
                                    "target": f"uuid:{rect_uuid}",
                                    "dx": 250,
                                    "dy": 125,
                                    "rotation_deg": 10,
                                    "scale_x": 1.1,
                                    "scale_y": 1.1,
                                },
                            }
                        ],
                        "idempotency_key": f"{prefix}-operation-transform",
                    },
                    label="operation_transform_individual",
                )
                transformed = require_exactly_one(
                    await query_named(
                        f"{prefix}_TYPE_RECT",
                        label="operation_transform_readback",
                    ),
                    "transformed rect",
                )
                if transformed.get("uuid") != rect_uuid:
                    raise RuntimeError("transform changed the rect identity")
                operation_coverage["transform"] = transform_payload

                duplicate_payload = await call(
                    "vw_apply",
                    {
                        "operations": [
                            {
                                "type": "duplicate",
                                "operation_id": "operation-duplicate",
                                "params": {
                                    "target": f"uuid:{rect_uuid}",
                                    "dx": 1800,
                                    "dy": 0,
                                },
                            }
                        ],
                        "idempotency_key": f"{prefix}-operation-duplicate",
                    },
                    label="operation_duplicate_individual",
                )
                duplicate_uuid = first_receipt_uuid(duplicate_payload, "duplicate operation")
                operation_coverage["duplicate"] = duplicate_payload
                duplicate_name = f"{prefix}_OPERATION_DUPLICATE"
                set_duplicate_payload = await call(
                    "vw_apply",
                    {
                        "operations": [
                            {
                                "type": "set_properties",
                                "params": {
                                    "edits": [
                                        {
                                            "ref": f"uuid:{duplicate_uuid}",
                                            "properties": {"name": duplicate_name},
                                        }
                                    ]
                                },
                            }
                        ],
                        "idempotency_key": f"{prefix}-operation-set-properties",
                    },
                    label="operation_set_properties_individual",
                )
                renamed_duplicate = require_exactly_one(
                    await query_named(
                        duplicate_name,
                        label="operation_set_properties_readback",
                    ),
                    "renamed duplicate",
                )
                if renamed_duplicate.get("uuid") != duplicate_uuid:
                    raise RuntimeError("set_properties changed the duplicate identity")
                operation_coverage["set_properties"] = set_duplicate_payload

                delete_payload = await call(
                    "vw_apply",
                    {
                        "operations": [
                            {
                                "type": "delete",
                                "params": {"target": f"uuid:{rect_uuid}"},
                            }
                        ],
                        "idempotency_key": f"{prefix}-operation-delete",
                    },
                    label="operation_delete_individual",
                )
                if collection(
                    await query_named(
                        f"{prefix}_TYPE_RECT",
                        label="operation_delete_readback",
                    )
                ):
                    raise RuntimeError("delete operation left its exact-name target behind")
                operation_coverage["delete"] = delete_payload
                if set(operation_coverage) != EXPECTED_APPLY_OPERATION_TYPES:
                    raise RuntimeError("not every apply operation type was executed individually")

                space_object = individual_type_objects["space"]
                space_uuid = str(space_object.get("uuid") or "")
                if not space_uuid:
                    raise RuntimeError("individual Space omitted its UUID")
                revised_space_name = f"{prefix}_TYPE_SPACE_REVISED"
                space_revision_args = {
                    "operations": [
                        {
                            "type": "set_properties",
                            "params": {
                                "edits": [
                                    {
                                        "ref": f"uuid:{space_uuid}",
                                        "properties": {"name": revised_space_name},
                                    }
                                ]
                            },
                        }
                    ],
                    "idempotency_key": f"{prefix}-space-revision",
                }
                space_revision = await call(
                    "vw_apply",
                    space_revision_args,
                    label="space_external_mutation",
                )
                space_revision_replay = await call(
                    "vw_apply",
                    space_revision_args,
                    label="space_external_mutation_replay",
                )
                if not space_revision_replay.get("idempotency_replay"):
                    raise RuntimeError("Space external-mutation replay was not recognized")
                revised_space = require_exactly_one(
                    await query_named(
                        revised_space_name,
                        label="space_external_mutation_readback",
                    ),
                    "revised Space",
                )
                if revised_space.get("uuid") != space_uuid:
                    raise RuntimeError("Space external mutation changed object identity")

                rejected_space_name = f"{prefix}_SPACE_SHOULD_ROLLBACK"
                space_rollback = await call(
                    "vw_apply",
                    {
                        "operations": [
                            {
                                "type": "set_properties",
                                "params": {
                                    "edits": [
                                        {
                                            "ref": f"uuid:{space_uuid}",
                                            "properties": {"name": rejected_space_name},
                                        }
                                    ]
                                },
                            },
                            {
                                "type": "create",
                                "operation_id": "space-rollback-forced-failure",
                                "params": {
                                    "object_type": "parametric",
                                    "plugin_name": "__VW_MCP_INTENTIONAL_MISSING_PLUGIN__",
                                    "descriptor_fingerprint": "intentional-mismatch",
                                    "x": 70000,
                                    "y": 10000,
                                },
                            },
                        ],
                        "idempotency_key": f"{prefix}-space-rollback",
                    },
                    expect_ok=False,
                    label="space_external_mutation_rollback",
                )
                rolled_back_space = require_exactly_one(
                    await query_named(
                        revised_space_name,
                        label="space_external_rollback_readback",
                    ),
                    "rolled-back Space",
                )
                if rolled_back_space.get("uuid") != space_uuid:
                    raise RuntimeError("Space rollback restored the wrong object identity")
                if collection(
                    await query_named(
                        rejected_space_name,
                        label="space_external_rollback_residue_query",
                    )
                ):
                    raise RuntimeError("Space rollback left the rejected name in the document")
                report["checks"]["space_external_mutation_regression"] = {
                    "passed": True,
                    "uuid": space_uuid,
                    "replayed": bool(space_revision_replay.get("idempotency_replay")),
                    "rollback_error": space_rollback.get("error"),
                    "successful_mutation_verified": (space_revision.get("verification") or {}).get("ok"),
                }

                for suffix, _display_name, room_id, _points in room_specs:
                    space = await call(
                        "vw_read",
                        {
                            "action": "query",
                            "criteria": f"((N='{prefix}_{suffix}'))",
                            "limit": 10,
                        },
                        label=f"verify_space_{room_id}",
                    )
                    verified_space = require_exactly_one(space, f"Space {room_id}")
                    if verified_space.get("type") != "parametric":
                        raise RuntimeError(f"Space {room_id} did not read back as a parametric node")
                for name, logical_type, native_type in (
                    # Vectorworks 2024 exposes its built-in Slab plug-in as a
                    # kParametricNode (type 86), while roofs retain kRoofNode.
                    (f"{prefix}_SLAB", "slab", "parametric"),
                    (f"{prefix}_ROOF", "roof", "roof"),
                    (door_name, "door", "parametric"),
                    (window_name, "window", "parametric"),
                ):
                    item = await call(
                        "vw_read",
                        {
                            "action": "query",
                            "criteria": f"((N='{name}'))",
                            "limit": 10,
                        },
                        label=f"verify_{logical_type}",
                    )
                    verified_item = require_exactly_one(item, f"{logical_type} {name}")
                    if verified_item.get("type") != native_type:
                        raise RuntimeError(
                            f"{logical_type} {name} read back as {verified_item.get('type')!r}, "
                            f"expected native type {native_type!r}"
                        )

                final_summary = await call(
                    "vw_read", {"action": "summary", "limit": 200}, label="final_summary"
                )
                await call("vw_read", {"action": "layers", "limit": 100}, label="layers_read")
                await call(
                    "vw_read", {"action": "selection", "limit": 100}, label="selection_read"
                )
                await call("vw_document", {"action": "info"}, label="document_info_grouped")
                catalog_payloads: dict[str, dict[str, Any]] = {}
                for action in ("classes", "symbols", "worksheets", "resources"):
                    catalog_payloads[action] = await call(
                        "vw_catalog",
                        {"action": action, "limit": 20},
                        label=f"catalog_{action}",
                    )
                if strict_class_name not in (payload_data(catalog_payloads["classes"]) or []):
                    raise RuntimeError("native class creation was not visible in the class catalog")
                resource_symbols = collection(catalog_payloads["resources"], "symbols")
                if not any(item.get("name") == symbol_definition_name for item in resource_symbols):
                    raise RuntimeError("resource catalog omitted the symbol used by strict acceptance")
                safety = await call("vw_tool_safety", {}, label="tool_safety")
                view = await call("vw_view", {"action": "get"}, label="view_get")
                view_data = payload_data(view) or {}
                await call(
                    "vw_view",
                    {
                        "action": "set",
                        "options": {
                            "standard_view": int(view_data.get("standard_view", 0)),
                            "projection": int(view_data.get("projection", 0)),
                            "render_mode": int(view_data.get("render_mode", 0)),
                        },
                    },
                    label="view_semantic_noop",
                )

                export_path = output_dir / f"{prefix}.png"
                capture_path = output_dir / f"{prefix}-capture.png"
                pdf_path = output_dir / f"{prefix}.pdf"
                exported_vwx_path = output_dir / f"{prefix}-exported.vwx"
                dwg_path = output_dir / f"{prefix}.dwg"
                save_path = output_dir / f"{prefix}.vwx"
                output_paths = (
                    export_path,
                    capture_path,
                    pdf_path,
                    exported_vwx_path,
                    dwg_path,
                    save_path,
                )
                if source in {path.resolve() for path in output_paths}:
                    raise RuntimeError("an output path resolves to the disposable source")
                await call(
                    "vw_io",
                    {"action": "export", "file_path": str(export_path), "format": "png"},
                    label="native_png_export",
                )
                await call(
                    "vw_view",
                    {"action": "capture", "file_path": str(capture_path)},
                    label="native_view_capture",
                )
                await call(
                    "vw_io",
                    {
                        "action": "export",
                        "file_path": str(pdf_path),
                        "format": "pdf",
                        "options": {"current_view_only": True, "resolution_dpi": 150},
                    },
                    label="native_pdf_export",
                )
                await call(
                    "vw_document",
                    {
                        "action": "export",
                        "file_path": str(exported_vwx_path),
                        "format": "vwx",
                        "options": {"target_file_version": 29},
                    },
                    label="native_vectorworks_export",
                )
                await call(
                    "vw_io",
                    {"action": "export", "file_path": str(dwg_path), "format": "dwg"},
                    label="native_dwg_export",
                )
                await call(
                    "vw_io",
                    {"action": "import", "file_path": str(dwg_path), "format": "dwg"},
                    label="native_dwg_import",
                )
                await call(
                    "vw_document",
                    {"action": "save", "file_path": str(save_path)},
                    label="native_document_save",
                )
                if not all(path.is_file() and path.stat().st_size > 0 for path in output_paths):
                    raise RuntimeError("one or more native output files were not created")

                saved_document = await call(
                    "vw_read", {"action": "document"}, label="saved_document_identity"
                )
                if Path(str((payload_data(saved_document) or {}).get("filepath", ""))).resolve() != save_path.resolve():
                    raise RuntimeError("save_document did not make the output document active")
                await call(
                    "vw_document",
                    {
                        "action": "open",
                        "file_path": str(source),
                        "options": {
                            "replace_dirty_confirmation": "REPLACE_DIRTY_DOCUMENT"
                        },
                    },
                    label="document_open_source_roundtrip",
                )
                reopened_source = await call(
                    "vw_read", {"action": "document"}, label="document_reopened_source_identity"
                )
                if Path(str((payload_data(reopened_source) or {}).get("filepath", ""))).resolve() != source:
                    raise RuntimeError("open_document did not reopen the disposable source")
                await call(
                    "vw_document",
                    {
                        "action": "open",
                        "file_path": str(save_path),
                        "options": {
                            "replace_dirty_confirmation": "REPLACE_DIRTY_DOCUMENT"
                        },
                    },
                    label="document_open_output_roundtrip",
                )
                reopened_output = await call(
                    "vw_read", {"action": "document"}, label="document_reopened_output_identity"
                )
                if Path(str((payload_data(reopened_output) or {}).get("filepath", ""))).resolve() != save_path.resolve():
                    raise RuntimeError("open_document did not reopen the saved acceptance output")
                accepted_summary = await call(
                    "vw_read",
                    {"action": "summary", "limit": 200},
                    label="accepted_output_summary",
                )

                report["checks"].update(
                    {
                        "main_2bhk": {
                            "passed": True,
                            "operations": len(operations),
                            "replayed": bool(replay_payload.get("idempotency_replay")),
                            "created_count": main_payload.get("created_count"),
                            "atomic": main_payload.get("atomic"),
                            "verified": main_payload.get("verified"),
                        },
                        "hosted_openings": {
                            "passed": True,
                            "wall_uuid": wall_uuid,
                            "door_fingerprint": door_fingerprint,
                            "window_fingerprint": window_fingerprint,
                            "door_atomic": door_payload.get("atomic"),
                            "window_atomic": window_payload.get("atomic"),
                            "door_verified": (door_payload.get("verification") or {}).get("ok"),
                            "window_verified": (window_payload.get("verification") or {}).get("ok"),
                        },
                        "individual_create_types": {
                            "passed": True,
                            "advertised": sorted(EXPECTED_CREATE_TYPES),
                            "executed": sorted(individual_type_results),
                            "native_types": {
                                object_type: item.get("type")
                                for object_type, item in sorted(individual_type_objects.items())
                            },
                            "symbol_definition": symbol_definition_name,
                        },
                        "individual_apply_operations": {
                            "passed": True,
                            "advertised": sorted(EXPECTED_APPLY_OPERATION_TYPES),
                            "executed": sorted(operation_coverage),
                            "duplicate_uuid": duplicate_uuid,
                        },
                        "catalogs_and_safety": {
                            "passed": True,
                            "response_fields": sorted(safety),
                            "class_created": strict_class_name,
                            "selection_read": True,
                            "layers_read": True,
                            "worksheet_action_listed": True,
                        },
                        "document_and_io_workflows": {
                            "passed": True,
                            "formats": ["png", "pdf", "vwx", "dwg"],
                            "dwg_roundtrip_imported": True,
                            "document_saved": True,
                            "source_reopened": True,
                            "output_reopened": True,
                            "view_get_set_capture": True,
                        },
                        "native_files": {
                            "passed": True,
                            "export": str(export_path),
                            "capture": str(capture_path),
                            "pdf": str(pdf_path),
                            "vectorworks_export": str(exported_vwx_path),
                            "dwg": str(dwg_path),
                            "document": str(save_path),
                            "sizes": {
                                path.name: path.stat().st_size for path in output_paths
                            },
                        },
                    }
                )
                report["baseline_total"] = baseline_total
                report["pre_import_total"] = summary_total(final_summary)
                report["final_total"] = summary_total(accepted_summary)
                report["final_counts_by_type"] = (
                    payload_data(accepted_summary) or {}
                ).get("counts_by_type")
                report["passed"] = True


def main() -> int:
    args = parse_args()
    source, output_dir, token_file = validate_args(args)
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    prefix = fixture_prefix(run_id)
    report: dict[str, Any] = {
        "run_id": run_id,
        "prefix": prefix,
        "source_document": str(source),
        "output_dir": str(output_dir),
        "checks": {},
        "timings_ms": {},
        "passed": False,
    }
    report_path = output_dir / f"{prefix}-report.json"
    exit_code = 0
    try:
        anyio.run(run_acceptance, args, source, output_dir, token_file, report)
    except Exception as exc:  # A failed acceptance must still leave a portable report.
        def serialize_exception(error: BaseException) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            nested = getattr(error, "exceptions", None)
            if nested:
                payload["exceptions"] = [serialize_exception(item) for item in nested]
            return payload

        report["error"] = serialize_exception(exc)
        exit_code = 1
    report["report_path"] = str(report_path)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    stream = sys.stdout if exit_code == 0 else sys.stderr
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), file=stream)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
