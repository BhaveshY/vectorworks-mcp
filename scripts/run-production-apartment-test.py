"""Create and verify a production-style apartment plan through the native MCP surface."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "test-artifacts" / "production-apartment-20260828"
TOKEN_FILE = Path.home() / ".vectorworks-mcp" / "auth-token"
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


def data(payload: dict[str, Any]) -> Any:
    return payload.get("data") if isinstance(payload, dict) else None


def objects(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = data(payload)
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("objects"), list):
        return value["objects"]
    return []


def create(operation_id: str, object_type: str, **params: Any) -> dict[str, Any]:
    return {
        "type": "create",
        "operation_id": operation_id,
        "params": {"object_type": object_type, **params},
    }


async def main() -> None:
    if not TOKEN_FILE.is_file():
        raise SystemExit("Vectorworks MCP authentication token is missing")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = f"APT_{run_id}"
    save_path = OUTPUT_ROOT / f"production-apartment-{run_id}.vwx"
    pdf_path = OUTPUT_ROOT / f"production-apartment-{run_id}.pdf"
    report_path = OUTPUT_ROOT / f"production-apartment-{run_id}-report.json"
    report: dict[str, Any] = {
        "run_id": run_id,
        "units": "millimetres",
        "program": "79 m2 two-bedroom apartment",
        "timings_ms": {},
        "outputs": {"vwx": str(save_path), "pdf": str(pdf_path)},
    }

    env = os.environ.copy()
    env.update(
        {
            "VW_MCP_HOST": "127.0.0.1",
            "VW_MCP_PORT": "9877",
            "VW_MCP_TOOL_PROFILE": "fast-native",
            "VW_MCP_AUTH_TOKEN_FILE": str(TOKEN_FILE),
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
                read_timeout_seconds=timedelta(seconds=120),
            ) as session:
                await session.initialize()
                listed = await session.list_tools()
                tool_names = {tool.name for tool in listed.tools}
                if tool_names != EXPECTED_TOOLS:
                    raise RuntimeError(f"unexpected MCP tool surface: {sorted(tool_names)}")

                async def call(
                    tool: str, arguments: dict[str, Any], *, label: str
                ) -> dict[str, Any]:
                    started = time.perf_counter()
                    result = await session.call_tool(tool, arguments)
                    report["timings_ms"][label] = round(
                        (time.perf_counter() - started) * 1000.0, 3
                    )
                    payload = result.structuredContent or {}
                    if result.isError or not isinstance(payload, dict) or payload.get("ok") is False:
                        raise RuntimeError(
                            f"{label} failed: {json.dumps(payload, ensure_ascii=False)[:4000]}"
                        )
                    return payload

                health = await call("vw_status", {"action": "health"}, label="health")
                bridge = health.get("bridge") or {}
                required = {
                    "native_bridge": True,
                    "native_phase": 4,
                    "capability_revision": 4,
                    "cad_api_safe": True,
                    "transport_only": False,
                    "main_context_pump_ready": True,
                }
                for key, expected in required.items():
                    if bridge.get(key) != expected:
                        raise RuntimeError(f"bridge preflight failed for {key}")
                report["bridge"] = bridge

                capabilities = await call(
                    "vw_catalog", {"action": "capabilities"}, label="capabilities"
                )
                cap_data = data(capabilities) or {}
                required_types = {
                    "wall",
                    "space",
                    "slab",
                    "door",
                    "window",
                    "rect",
                    "oval",
                    "line",
                    "polygon",
                    "text",
                    "linear_dimension",
                }
                missing = required_types - set(cap_data.get("create_object_types") or [])
                if missing:
                    raise RuntimeError(f"bridge lacks required native types: {sorted(missing)}")

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
                door_fingerprint = str((data(door_schema) or {}).get("descriptor_fingerprint", ""))
                window_fingerprint = str(
                    (data(window_schema) or {}).get("descriptor_fingerprint", "")
                )
                if not door_fingerprint or not window_fingerprint:
                    raise RuntimeError("Door/Window schema fingerprint is missing")

                existing = await call(
                    "vw_read",
                    {
                        "action": "query",
                        "criteria": "ALL",
                        "limit": 200,
                        "fields": ["uuid", "name", "type"],
                    },
                    label="inspect_existing_document",
                )
                existing_objects = objects(existing)
                missing_uuids = [item for item in existing_objects if not item.get("uuid")]
                if missing_uuids:
                    raise RuntimeError("cannot clear document safely: an object omitted its UUID")
                report["cleared_object_count"] = len(existing_objects)

                clear_operations: list[dict[str, Any]] = [
                    {"type": "delete", "params": {"target": f"uuid:{item['uuid']}"}}
                    for item in existing_objects
                ]
                if clear_operations:
                    await call(
                        "vw_apply",
                        {
                            "operations": clear_operations,
                            "idempotency_key": f"{prefix}-clear",
                        },
                        label="atomic_clear_document",
                    )

                operations: list[dict[str, Any]] = []

                # Floor and semantic room program. Spaces are inset from wall centre lines.
                operations.append(
                    create(
                        "floor-slab",
                        "slab",
                        points=[[0, 0], [12000, 0], [12000, 8000], [0, 8000]],
                        closed=True,
                        thickness=200,
                        elevation=-200,
                        name=f"{prefix}_SLAB",
                        class_name="A-Slab",
                    )
                )
                operations.append(
                    {
                        "type": "set_properties",
                        "params": {
                            "edits": [
                                {
                                    "ref": "$floor-slab",
                                    "properties": {"fillPattern": 0},
                                }
                            ]
                        },
                    }
                )
                await call(
                    "vw_apply",
                    {
                        "operations": operations,
                        "idempotency_key": f"{prefix}-slab",
                    },
                    label="atomic_slab",
                )
                operations = []
                room_specs = [
                    ("living", "Living / Dining", "01", 27.9, [[200, 200], [6400, 200], [6400, 4700], [200, 4700]], 1400, 2700),
                    ("kitchen", "Kitchen", "02", 18.0, [[200, 4900], [6400, 4900], [6400, 7800], [200, 7800]], 2300, 6400),
                    ("bath", "Bathroom", "03", 3.8, [[6800, 200], [8600, 200], [8600, 2300], [6800, 2300]], 7000, 1450),
                    ("hall", "Entrance / Hall", "04", 2.5, [[6800, 2700], [8600, 2700], [8600, 4100], [6800, 4100]], 6900, 3450),
                    ("bed2", "Bedroom 2", "05", 10.9, [[9000, 200], [11800, 200], [11800, 4100], [9000, 4100]], 9500, 3550),
                    ("bed1", "Bedroom 1", "06", 16.5, [[6800, 4500], [11800, 4500], [11800, 7800], [6800, 7800]], 8350, 7000),
                ]
                for room_id, label, number, area, points, label_x, label_y in room_specs:
                    operations.append(
                        create(
                            f"space-{room_id}",
                            "space",
                            points=points,
                            closed=True,
                            height=2700,
                            name=f"{prefix}_SPACE_{room_id.upper()}",
                            room_id=number,
                            class_name="A-Space",
                        )
                    )
                operations.append(
                    {
                        "type": "set_properties",
                        "params": {
                            "edits": [
                                {
                                    "ref": f"$space-{room_id}",
                                    "properties": {"fillPattern": 0},
                                }
                                for room_id, *_ in room_specs
                            ]
                        },
                    }
                )
                report["space_operation_count"] = len(operations)
                await call(
                    "vw_apply",
                    {
                        "operations": operations,
                        "idempotency_key": f"{prefix}-spaces",
                    },
                    label="atomic_spaces",
                )
                operations = []
                for room_id, label, number, area, points, label_x, label_y in room_specs:
                    operations.append(
                        create(
                            f"label-{room_id}",
                            "text",
                            x=label_x,
                            y=label_y,
                            text=f"{number}  {label}\n{area:.1f} m2",
                            text_size=12,
                            name=f"{prefix}_LABEL_{room_id.upper()}",
                            class_name="A-Anno-Room",
                        )
                    )
                await call(
                    "vw_apply",
                    {
                        "operations": operations,
                        "idempotency_key": f"{prefix}-room-labels",
                    },
                    label="atomic_room_labels",
                )
                operations = []

                wall_specs = [
                    ("south", 0, 0, 12000, 0, 200, "A-Wall-External"),
                    ("east", 12000, 0, 12000, 8000, 200, "A-Wall-External"),
                    ("north", 12000, 8000, 0, 8000, 200, "A-Wall-External"),
                    ("west", 0, 8000, 0, 0, 200, "A-Wall-External"),
                    ("spine", 6600, 0, 6600, 8000, 120, "A-Wall-Internal"),
                    ("private", 6600, 4300, 12000, 4300, 120, "A-Wall-Internal"),
                    ("bath-east", 8800, 0, 8800, 4300, 120, "A-Wall-Internal"),
                    ("bath-north", 6600, 2500, 8800, 2500, 120, "A-Wall-Internal"),
                ]
                for wall_id, x1, y1, x2, y2, thickness, class_name in wall_specs:
                    wall_operation = create(
                        f"wall-{wall_id}",
                        "wall",
                        x1=x1,
                        y1=y1,
                        x2=x2,
                        y2=y2,
                        height=2700,
                        thickness=thickness,
                        name=f"{prefix}_WALL_{wall_id.upper()}",
                        class_name=class_name,
                    )
                    await call(
                        "vw_apply",
                        {
                            "operations": [wall_operation],
                            "idempotency_key": f"{prefix}-wall-{wall_id}",
                        },
                        label=f"atomic_wall_{wall_id}",
                    )
                report["wall_operation_count"] = len(wall_specs)
                operations = []

                # Furniture and sanitary fixtures provide real plan-readability.
                rectangles = [
                    ("sofa", 700, 1350, 2950, 2250, "A-Furn"),
                    ("coffee", 1350, 2700, 2550, 3300, "A-Furn"),
                    ("media", 5400, 1450, 6200, 3400, "A-Furn"),
                    ("dining", 3350, 2750, 5200, 3900, "A-Furn"),
                    ("kit-west", 250, 5200, 850, 7600, "A-Furn-Cabinet"),
                    ("kit-north", 850, 7200, 6100, 7750, "A-Furn-Cabinet"),
                    ("island", 2600, 5750, 4750, 6450, "A-Furn-Cabinet"),
                    ("bed1", 8350, 5000, 10850, 7350, "A-Furn"),
                    ("pillow1a", 8500, 6750, 9450, 7200, "A-Furn"),
                    ("pillow1b", 9750, 6750, 10700, 7200, "A-Furn"),
                    ("wardrobe1", 6900, 7000, 7900, 7750, "A-Furn-Storage"),
                    ("night1a", 7900, 6400, 8300, 7000, "A-Furn"),
                    ("night1b", 10900, 6400, 11600, 7000, "A-Furn"),
                    ("bed2", 9400, 950, 11400, 3150, "A-Furn"),
                    ("pillow2", 9600, 2600, 11200, 3050, "A-Furn"),
                    ("wardrobe2", 9100, 3350, 11600, 4000, "A-Furn-Storage"),
                    ("shower", 6800, 300, 7800, 1300, "P-Fixture"),
                    ("vanity", 8000, 300, 8550, 1100, "P-Fixture"),
                    ("hall-closet", 6900, 2800, 7450, 4050, "A-Furn-Storage"),
                ]
                for item_id, x1, y1, x2, y2, class_name in rectangles:
                    operations.append(
                        create(
                            f"furn-{item_id}",
                            "rect",
                            x1=x1,
                            y1=y1,
                            x2=x2,
                            y2=y2,
                            name=f"{prefix}_FURN_{item_id.upper()}",
                            class_name=class_name,
                        )
                    )
                for chair_id, x1, y1, x2, y2 in [
                    ("c1", 3500, 2350, 3950, 2700),
                    ("c2", 4600, 2350, 5050, 2700),
                    ("c3", 3500, 3950, 3950, 4300),
                    ("c4", 4600, 3950, 5050, 4300),
                    ("c5", 3000, 3050, 3300, 3500),
                    ("c6", 5250, 3050, 5550, 3500),
                ]:
                    operations.append(
                        create(
                            f"chair-{chair_id}",
                            "rect",
                            x1=x1,
                            y1=y1,
                            x2=x2,
                            y2=y2,
                            name=f"{prefix}_CHAIR_{chair_id.upper()}",
                            class_name="A-Furn",
                        )
                    )
                operations.extend(
                    [
                        create("wc", "oval", x1=7850, y1=1400, x2=8500, y2=2200, name=f"{prefix}_WC", class_name="P-Fixture"),
                        create("sink", "oval", x1=8075, y1=450, x2=8475, y2=850, name=f"{prefix}_SINK", class_name="P-Fixture"),
                        create("shower-diag-a", "line", x1=6800, y1=300, x2=7800, y2=1300, name=f"{prefix}_SHOWER_A", class_name="P-Fixture"),
                        create("shower-diag-b", "line", x1=7800, y1=300, x2=6800, y2=1300, name=f"{prefix}_SHOWER_B", class_name="P-Fixture"),
                    ]
                )

                report["furnishing_operation_count"] = len(operations)
                await call(
                    "vw_apply",
                    {
                        "operations": operations,
                        "idempotency_key": f"{prefix}-furnishings",
                    },
                    label="atomic_furnishings",
                )
                operations = []

                # Drawing sheet, hierarchy, north arrow, scale and coordinated dimensions.
                operations.extend(
                    [
                        create("sheet-border", "rect", x1=-1500, y1=-1500, x2=18400, y2=9800, name=f"{prefix}_BORDER", class_name="A-Sheet-Border"),
                        create("title-rule", "line", x1=12800, y1=0, x2=12800, y2=9000, name=f"{prefix}_TITLE_RULE", class_name="A-Sheet-Border"),
                        create("title", "text", x=13200, y=8850, width=4700, wrap=True, text="PROPOSED TWO-BEDROOM APARTMENT", text_size=16, name=f"{prefix}_TITLE", class_name="A-Anno-Title"),
                        create("subtitle", "text", x=13200, y=8350, width=4700, wrap=True, text="GENERAL ARRANGEMENT PLAN", text_size=13, name=f"{prefix}_SUBTITLE", class_name="A-Anno-Title"),
                        create("scale", "text", x=13200, y=7850, width=4700, wrap=True, text="Scale 1:50 at A3  |  Units: mm", text_size=11, name=f"{prefix}_SCALE", class_name="A-Anno-Text"),
                        create("status", "text", x=13200, y=7450, width=4700, wrap=True, text="STATUS: CONNECTOR PRODUCTION TEST", text_size=10, name=f"{prefix}_STATUS", class_name="A-Anno-Text"),
                        create("area-heading", "text", x=13200, y=6700, width=4700, wrap=True, text="ROOM SCHEDULE", text_size=13, name=f"{prefix}_AREA_HEAD", class_name="A-Anno-Title"),
                        create("area-list", "text", x=13200, y=6250, width=4500, wrap=True, text="01  Living / Dining       27.9 m2\n02  Kitchen                18.0 m2\n03  Bathroom                3.8 m2\n04  Entrance / Hall         2.5 m2\n05  Bedroom 2              10.9 m2\n06  Bedroom 1              16.5 m2\n\nNET PROGRAM AREA          79.6 m2", text_size=10, name=f"{prefix}_AREA_LIST", class_name="A-Anno-Text"),
                        create("notes-heading", "text", x=13200, y=3850, width=4700, wrap=True, text="GENERAL NOTES", text_size=13, name=f"{prefix}_NOTES_HEAD", class_name="A-Anno-Title"),
                        create("notes", "text", x=13200, y=3450, width=4500, wrap=True, text="1. Verify dimensions on site.\n2. Do not scale from this drawing.\n3. Doors and windows are wall-hosted BIM objects.\n4. Room names/numbers are native Space objects.", text_size=9, name=f"{prefix}_NOTES", class_name="A-Anno-Text"),
                        create("north-a", "line", x1=15100, y1=1050, x2=15100, y2=2500, name=f"{prefix}_NORTH_A", class_name="A-Anno-Marker"),
                        create("north-b", "polygon", points=[[15100, 2800], [14750, 2250], [15450, 2250]], closed=True, name=f"{prefix}_NORTH_B", class_name="A-Anno-Marker"),
                        create("north-label", "text", x=14950, y=3100, text="N", text_size=16, name=f"{prefix}_NORTH_LABEL", class_name="A-Anno-Marker"),
                        create("dim-overall-x", "linear_dimension", x1=0, y1=0, x2=12000, y2=0, offset=-800, class_name="A-Anno-Dims"),
                        create("dim-overall-y", "linear_dimension", x1=12000, y1=0, x2=12000, y2=8000, offset=800, class_name="A-Anno-Dims"),
                        create("dim-zone-x1", "linear_dimension", x1=0, y1=8000, x2=6600, y2=8000, offset=700, class_name="A-Anno-Dims"),
                        create("dim-zone-x2", "linear_dimension", x1=6600, y1=8000, x2=12000, y2=8000, offset=700, class_name="A-Anno-Dims"),
                        create("dim-zone-y1", "linear_dimension", x1=12000, y1=0, x2=12000, y2=4300, offset=450, class_name="A-Anno-Dims"),
                        create("dim-zone-y2", "linear_dimension", x1=12000, y1=4300, x2=12000, y2=8000, offset=450, class_name="A-Anno-Dims"),
                    ]
                )
                operations.append(
                    {
                        "type": "set_properties",
                        "params": {
                            "edits": [
                                {
                                    "ref": "$sheet-border",
                                    "properties": {"fillPattern": 0},
                                }
                            ]
                        },
                    }
                )

                report["annotation_operation_count"] = len(operations)
                await call(
                    "vw_apply",
                    {
                        "operations": operations,
                        "idempotency_key": f"{prefix}-annotations",
                    },
                    label="atomic_sheet_annotations",
                )

                async def wall_uuid(wall_id: str) -> str:
                    payload = await call(
                        "vw_read",
                        {
                            "action": "query",
                            "criteria": f"((N='{prefix}_WALL_{wall_id.upper()}'))",
                            "object_type": "wall",
                            "limit": 10,
                            "fields": ["uuid", "name", "type"],
                        },
                        label=f"wall_uuid_{wall_id}",
                    )
                    matches = objects(payload)
                    if len(matches) != 1 or not matches[0].get("uuid"):
                        raise RuntimeError(f"wall {wall_id} UUID readback failed")
                    return str(matches[0]["uuid"])

                walls = {
                    wall_id: await wall_uuid(wall_id)
                    for wall_id in ("south", "east", "north", "west", "spine", "private", "bath-east", "bath-north")
                }
                opening_specs = [
                    ("door", "entry", "south", 5200, 0, 0, 1000, 2100, None),
                    ("door", "hall", "spine", 6600, 3350, 90, 900, 2100, None),
                    ("door", "bath", "bath-north", 7600, 2500, 0, 800, 2100, None),
                    ("door", "bed2", "bath-east", 8800, 3500, 90, 900, 2100, None),
                    ("door", "bed1", "private", 7600, 4300, 0, 900, 2100, None),
                    ("window", "living", "west", 0, 2500, 90, 1800, 1500, 900),
                    ("window", "kitchen", "west", 0, 6500, 90, 1500, 1200, 1050),
                    ("window", "kitchen-north", "north", 3500, 8000, 0, 1800, 1200, 1050),
                    ("window", "bed1", "north", 9500, 8000, 0, 1800, 1500, 900),
                    ("window", "bed2", "east", 12000, 2200, 90, 1500, 1500, 900),
                    ("window", "bath", "south", 7600, 0, 0, 900, 700, 1500),
                ]
                opening_ops: list[dict[str, Any]] = []
                for kind, opening_id, wall_id, x, y, rotation, width, height, sill in opening_specs:
                    opening_params: dict[str, Any] = {
                        "plugin_name": "Door" if kind == "door" else "Window",
                        "descriptor_fingerprint": door_fingerprint if kind == "door" else window_fingerprint,
                        "wall_uuid": walls[wall_id],
                        "x": x,
                        "y": y,
                        "rotation": rotation,
                        "width": width,
                        "height": height,
                        "name": f"{prefix}_{kind.upper()}_{opening_id.upper()}",
                        "class_name": "A-Door" if kind == "door" else "A-Window",
                    }
                    if sill is not None:
                        opening_params["sill_height"] = sill
                    opening_ops.append(create(f"{kind}-{opening_id}", kind, **opening_params))
                await call(
                    "vw_apply",
                    {"operations": opening_ops, "idempotency_key": f"{prefix}-openings"},
                    label="atomic_hosted_openings",
                )

                summary = await call(
                    "vw_read", {"action": "summary", "limit": 200}, label="final_summary"
                )
                space_readback = await call(
                    "vw_read",
                    {
                        "action": "query",
                        "criteria": "ALL",
                        "object_type": "space",
                        "limit": 20,
                        "fields": ["uuid", "name", "type", "room_id", "area"],
                    },
                    label="verify_spaces",
                )
                opening_readback = await call(
                    "vw_read",
                    {
                        "action": "query",
                        "criteria": "ALL",
                        "limit": 200,
                        "fields": ["uuid", "name", "type"],
                    },
                    label="verify_hosted_openings",
                )
                door_objects = [
                    item
                    for item in objects(opening_readback)
                    if str(item.get("name", "")).startswith(f"{prefix}_DOOR_")
                ]
                window_objects = [
                    item
                    for item in objects(opening_readback)
                    if str(item.get("name", "")).startswith(f"{prefix}_WINDOW_")
                ]
                if len(objects(space_readback)) != 6:
                    raise RuntimeError("semantic Space count is not six")
                if len(door_objects) != 5 or len(window_objects) != 6:
                    raise RuntimeError("hosted opening counts are incorrect")

                await call(
                    "vw_document",
                    {"action": "save", "file_path": str(save_path)},
                    label="save_vwx",
                )
                await call(
                    "vw_io",
                    {"action": "export", "file_path": str(pdf_path), "format": "pdf"},
                    label="export_pdf",
                )
                report["checks"] = {
                    "semantic_spaces": len(objects(space_readback)),
                    "hosted_doors": len(door_objects),
                    "hosted_windows": len(window_objects),
                    "summary": data(summary),
                }

    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({**report, "report": str(report_path)}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    anyio.run(main)
