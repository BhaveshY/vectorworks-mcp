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
                        and payload.get("ok") is True
                        and not result.isError
                    )
                    if expect_ok and not ok:
                        excerpt = json.dumps(payload, ensure_ascii=False)[:4000]
                        raise RuntimeError(f"{key} failed: {excerpt}")
                    if not expect_ok and ok:
                        raise RuntimeError(f"{key} unexpectedly succeeded")
                    return payload

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
                required_types = {
                    "wall",
                    "space",
                    "slab",
                    "roof",
                    "door",
                    "window",
                    "text",
                    "linear_dimension",
                }
                missing_types = sorted(
                    required_types - set(cap_data.get("create_object_types") or [])
                )
                if missing_types:
                    raise RuntimeError(
                        f"manifest is missing required create types: {missing_types}"
                    )

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
                        {"action": "open", "file_path": str(source)},
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
                door_fingerprint = str(
                    (payload_data(door_schema) or {}).get("descriptor_fingerprint", "")
                )
                window_fingerprint = str(
                    (payload_data(window_schema) or {}).get("descriptor_fingerprint", "")
                )
                if not door_fingerprint or not window_fingerprint:
                    raise RuntimeError("Door/Window schema discovery omitted fingerprints")

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
                if not replay_payload.get("replayed"):
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
                openings_payload = await call(
                    "vw_apply",
                    {
                        "operations": [
                            {
                                "type": "create",
                                "operation_id": "entry-door",
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
                                    "name": f"{prefix}_ENTRY_DOOR",
                                },
                            },
                            {
                                "type": "create",
                                "operation_id": "living-window",
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
                                    "name": f"{prefix}_LIVING_WINDOW",
                                },
                            },
                        ],
                        "idempotency_key": f"{prefix}-openings",
                    },
                    label="hosted_openings_apply",
                )

                for suffix, _display_name, room_id, _points in room_specs:
                    space = await call(
                        "vw_read",
                        {
                            "action": "query",
                            "criteria": f"((N='{prefix}_{suffix}'))",
                            "object_type": "space",
                            "limit": 10,
                        },
                        label=f"verify_space_{room_id}",
                    )
                    require_exactly_one(space, f"Space {room_id}")
                for name, object_type in (
                    (f"{prefix}_SLAB", "slab"),
                    (f"{prefix}_ROOF", "roof"),
                    (f"{prefix}_ENTRY_DOOR", "door"),
                    (f"{prefix}_LIVING_WINDOW", "window"),
                ):
                    item = await call(
                        "vw_read",
                        {
                            "action": "query",
                            "criteria": f"((N='{name}'))",
                            "object_type": object_type,
                            "limit": 10,
                        },
                        label=f"verify_{object_type}",
                    )
                    require_exactly_one(item, f"{object_type} {name}")

                final_summary = await call(
                    "vw_read", {"action": "summary", "limit": 500}, label="final_summary"
                )
                for action in ("classes", "symbols", "worksheets", "resources"):
                    await call(
                        "vw_catalog",
                        {"action": action, "limit": 20},
                        label=f"catalog_{action}",
                    )
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
                save_path = output_dir / f"{prefix}.vwx"
                if source in {export_path.resolve(), capture_path.resolve(), save_path.resolve()}:
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
                    "vw_document",
                    {"action": "save", "file_path": str(save_path)},
                    label="native_document_save",
                )
                output_paths = (export_path, capture_path, save_path)
                if not all(path.is_file() and path.stat().st_size > 0 for path in output_paths):
                    raise RuntimeError("one or more native output files were not created")

                report["checks"].update(
                    {
                        "main_2bhk": {
                            "passed": True,
                            "operations": len(operations),
                            "replayed": bool(replay_payload.get("replayed")),
                            "created_count": main_payload.get("created_count"),
                            "atomic": main_payload.get("atomic"),
                            "verified": main_payload.get("verified"),
                        },
                        "hosted_openings": {
                            "passed": True,
                            "wall_uuid": wall_uuid,
                            "door_fingerprint": door_fingerprint,
                            "window_fingerprint": window_fingerprint,
                            "created_count": openings_payload.get("created_count"),
                            "atomic": openings_payload.get("atomic"),
                            "verified": openings_payload.get("verified"),
                        },
                        "catalogs_and_safety": {
                            "passed": True,
                            "response_fields": sorted(safety),
                        },
                        "native_files": {
                            "passed": True,
                            "export": str(export_path),
                            "capture": str(capture_path),
                            "document": str(save_path),
                            "sizes": {
                                path.name: path.stat().st_size for path in output_paths
                            },
                        },
                    }
                )
                report["baseline_total"] = baseline_total
                report["final_total"] = summary_total(final_summary)
                report["final_counts_by_type"] = (
                    payload_data(final_summary) or {}
                ).get("counts_by_type")
                report["passed"] = True


def main() -> int:
    args = parse_args()
    source, output_dir, token_file = validate_args(args)
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    prefix = f"VW_MCP_FINAL_2BHK_{run_id}"
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
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
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
