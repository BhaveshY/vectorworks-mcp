"""Create and verify a quality-gated production apartment through the MCP surface."""

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
sys.path.insert(0, str(ROOT))

from production_apartment import (  # noqa: E402
    build_manifest,
    compile_foundation,
    compile_openings,
    expected_counts,
)


OUTPUT_ROOT = ROOT / "test-artifacts" / "production-apartment-quality-20260828"
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


def payload_data(payload: dict[str, Any]) -> Any:
    return payload.get("data") if isinstance(payload, dict) else None


def payload_objects(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload_data(payload)
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("objects"), list):
        return value["objects"]
    return []


async def main() -> None:
    if not TOKEN_FILE.is_file():
        raise SystemExit("Vectorworks MCP authentication token is missing")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = f"APTQ_{run_id}"
    save_path = OUTPUT_ROOT / f"production-apartment-quality-{run_id}.vwx"
    pdf_path = OUTPUT_ROOT / f"production-apartment-quality-{run_id}.pdf"
    screenshot_path = OUTPUT_ROOT / f"production-apartment-quality-{run_id}.png"
    report_path = OUTPUT_ROOT / f"production-apartment-quality-{run_id}-report.json"
    manifest = build_manifest()
    report: dict[str, Any] = {
        "run_id": run_id,
        "program": "quality-gated 75.84 m2 two-bedroom apartment",
        "units": "millimetres",
        "timings_ms": {},
        "outputs": {
            "vwx": str(save_path),
            "pdf": str(pdf_path),
            "screenshot": str(screenshot_path),
        },
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
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as session:
                await session.initialize()
                listed = await session.list_tools()
                tool_names = {tool.name for tool in listed.tools}
                if tool_names != EXPECTED_TOOLS:
                    raise RuntimeError(f"unexpected MCP tool surface: {sorted(tool_names)}")

                async def call(tool: str, arguments: dict[str, Any], *, label: str) -> dict[str, Any]:
                    started = time.perf_counter()
                    result = await session.call_tool(tool, arguments)
                    report["timings_ms"][label] = round((time.perf_counter() - started) * 1000.0, 3)
                    payload = result.structuredContent or {}
                    if result.isError or not isinstance(payload, dict) or payload.get("ok") is False:
                        raise RuntimeError(f"{label} failed: {json.dumps(payload, ensure_ascii=False)[:5000]}")
                    return payload

                async def read_all(
                    *,
                    label: str,
                    criteria: str = "ALL",
                    object_type: str = "",
                    fields: list[str] | None = None,
                ) -> list[dict[str, Any]]:
                    cursor = ""
                    rows: list[dict[str, Any]] = []
                    page_number = 0
                    while True:
                        page_number += 1
                        payload = await call(
                            "vw_read",
                            {
                                "action": "query",
                                "criteria": criteria,
                                "object_type": object_type,
                                "limit": 200,
                                "cursor": cursor,
                                "fields": fields or [],
                            },
                            label=f"{label}_page_{page_number}",
                        )
                        rows.extend(payload_objects(payload))
                        cursor = str((payload.get("page") or {}).get("next_cursor") or "")
                        if not cursor:
                            return rows

                health = await call("vw_status", {"action": "health"}, label="health")
                bridge = health.get("bridge") or {}
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
                        raise RuntimeError(f"bridge preflight failed for {key}")
                report["bridge"] = bridge

                capabilities = await call("vw_catalog", {"action": "capabilities"}, label="capabilities")
                required_types = {
                    "wall", "space", "slab", "door", "window", "rect", "line", "polygon", "text", "linear_dimension"
                }
                available_types = set((payload_data(capabilities) or {}).get("create_object_types") or [])
                missing = required_types - available_types
                if missing:
                    raise RuntimeError(f"bridge lacks required native types: {sorted(missing)}")

                door_schema = await call(
                    "vw_catalog", {"action": "parametric_schemas", "query": "Door"}, label="door_schema"
                )
                window_schema = await call(
                    "vw_catalog", {"action": "parametric_schemas", "query": "Window"}, label="window_schema"
                )
                door_fingerprint = str((payload_data(door_schema) or {}).get("descriptor_fingerprint", ""))
                window_fingerprint = str((payload_data(window_schema) or {}).get("descriptor_fingerprint", ""))
                if not door_fingerprint or not window_fingerprint:
                    raise RuntimeError("Door/Window schema fingerprint is missing")

                quality = await call(
                    "vw_read", {"action": "plan_quality", "plan": manifest}, label="host_plan_quality"
                )
                quality_report = payload_data(quality) or {}
                report["plan_quality"] = quality_report
                if not quality_report.get("passed"):
                    raise RuntimeError(
                        "plan quality gate rejected the fixture before mutation: "
                        + json.dumps(quality_report.get("issues"), ensure_ascii=False)[:5000]
                    )

                existing = await read_all(label="inspect_existing_document", fields=["uuid", "name", "type"])
                missing_uuids = [item for item in existing if not item.get("uuid")]
                if missing_uuids:
                    raise RuntimeError("cannot clear document safely: an object omitted its UUID")
                report["cleared_object_count"] = len(existing)
                for batch_index in range(0, len(existing), 250):
                    batch = existing[batch_index : batch_index + 250]
                    await call(
                        "vw_apply",
                        {
                            "operations": [
                                {"type": "delete", "params": {"target": f"uuid:{item['uuid']}"}}
                                for item in batch
                            ],
                            "idempotency_key": f"{prefix}-clear-{batch_index // 250 + 1}",
                        },
                        label=f"atomic_clear_batch_{batch_index // 250 + 1}",
                    )

                compiled = compile_foundation(manifest, prefix)
                counts = expected_counts(compiled)
                report["expected_counts"] = counts
                report["foundation_operation_count"] = len(compiled.foundation)
                await call(
                    "vw_apply",
                    {"operations": list(compiled.foundation), "idempotency_key": f"{prefix}-foundation"},
                    label="atomic_foundation",
                )

                wall_rows = await read_all(
                    label="wall_uuid_readback",
                    object_type="wall",
                    fields=["uuid", "name", "type", "class", "bounds"],
                )
                rows_by_name = {str(row.get("name")): row for row in wall_rows}
                wall_uuids: dict[str, str] = {}
                for wall_id, expected_name in compiled.wall_names.items():
                    row = rows_by_name.get(expected_name)
                    if not row or not row.get("uuid"):
                        raise RuntimeError(f"wall UUID readback failed for {wall_id}")
                    wall_uuids[wall_id] = str(row["uuid"])
                if len(wall_uuids) != counts["walls"]:
                    raise RuntimeError("wall count mismatch before hosted openings")

                opening_operations = compile_openings(
                    compiled,
                    prefix=prefix,
                    wall_uuids=wall_uuids,
                    door_fingerprint=door_fingerprint,
                    window_fingerprint=window_fingerprint,
                )
                report["opening_operation_count"] = len(opening_operations)
                await call(
                    "vw_apply",
                    {"operations": opening_operations, "idempotency_key": f"{prefix}-openings"},
                    label="atomic_hosted_openings",
                )

                observed = await read_all(
                    label="postflight",
                    fields=["uuid", "name", "type", "class", "bounds", "room_id", "net_area", "gross_area"],
                )
                spaces = [item for item in observed if item.get("type") == "space"]
                walls = [item for item in observed if item.get("type") == "wall"]
                doors = [item for item in observed if str(item.get("name", "")).startswith(f"{prefix}_DOOR_")]
                windows = [item for item in observed if str(item.get("name", "")).startswith(f"{prefix}_WINDOW_")]
                actual_counts = {"spaces": len(spaces), "walls": len(walls), "doors": len(doors), "windows": len(windows)}
                if actual_counts != counts:
                    raise RuntimeError(f"postflight count mismatch: {actual_counts} != {counts}")
                expected_rooms = {(room.name, room.number) for room in compiled.manifest.rooms}
                observed_rooms = {
                    (str(item.get("name", "")), str(item.get("room_id", "")))
                    for item in spaces
                }
                if observed_rooms != expected_rooms:
                    raise RuntimeError(
                        f"Space semantic readback mismatch: {sorted(observed_rooms)} != {sorted(expected_rooms)}"
                    )
                if any(not item.get("net_area") or not item.get("gross_area") for item in spaces):
                    raise RuntimeError("Space area semantics are missing from postflight")
                report["postflight"] = {
                    "actual_counts": actual_counts,
                    "rooms": sorted(
                        (
                            {
                                "name": str(item.get("name", "")),
                                "room_id": str(item.get("room_id", "")),
                                "net_area": item.get("net_area"),
                                "gross_area": item.get("gross_area"),
                            }
                            for item in spaces
                        ),
                        key=lambda item: item["room_id"],
                    ),
                }

                await call(
                    "vw_document", {"action": "save", "file_path": str(save_path)}, label="save_vwx"
                )
                await call(
                    "vw_io", {"action": "export", "file_path": str(pdf_path), "format": "pdf"}, label="export_pdf"
                )
                await call(
                    "vw_view", {"action": "capture", "file_path": str(screenshot_path)}, label="capture_png"
                )

    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({**report, "report": str(report_path)}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    anyio.run(main)
