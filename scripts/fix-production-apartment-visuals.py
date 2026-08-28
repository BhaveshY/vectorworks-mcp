"""Repair the current production-apartment test sheet through grouped MCP tools."""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = Path.home() / ".vectorworks-mcp" / "auth-token"


def objects(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("data")
    return value if isinstance(value, list) else []


def create(operation_id: str, **params: Any) -> dict[str, Any]:
    return {"type": "create", "operation_id": operation_id, "params": params}


async def main() -> None:
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
                read, write, read_timeout_seconds=timedelta(seconds=90)
            ) as session:
                await session.initialize()

                async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    result = await session.call_tool(tool, arguments)
                    payload = result.structuredContent or {}
                    if result.isError or not isinstance(payload, dict) or payload.get("ok") is False:
                        raise RuntimeError(json.dumps(payload, ensure_ascii=False)[:4000])
                    return payload

                document = await call("vw_read", {"action": "document"})
                document_data = document.get("data") or {}
                file_path = Path(str(document_data.get("filepath", "")))
                if not file_path.is_file() or not file_path.name.startswith("production-apartment-"):
                    raise RuntimeError("the active document is not the production-apartment fixture")

                queried = await call(
                    "vw_read",
                    {
                        "action": "query",
                        "criteria": "ALL",
                        "limit": 200,
                        "fields": ["uuid", "name", "type"],
                    },
                )
                named = {str(item.get("name")): item for item in objects(queried) if item.get("name")}
                slab_names = [name for name in named if name.endswith("_SLAB") and name.startswith("APT_")]
                if len(slab_names) != 1:
                    raise RuntimeError("expected exactly one named apartment slab")
                prefix = slab_names[0][:-5]
                slab = named[slab_names[0]]
                border = named.get(f"{prefix}_BORDER")
                if border is None or border.get("type") != "rect":
                    raise RuntimeError("expected exactly one named apartment sheet border")

                text_specs = {
                    "TITLE": dict(x=13200, y=8850, text="PROPOSED TWO-BEDROOM APARTMENT", text_size=16, width=4700),
                    "SUBTITLE": dict(x=13200, y=8350, text="GENERAL ARRANGEMENT PLAN", text_size=13, width=4700),
                    "SCALE": dict(x=13200, y=7850, text="Scale 1:50 at A3  |  Units: mm", text_size=11, width=4700),
                    "STATUS": dict(x=13200, y=7450, text="STATUS: CONNECTOR PRODUCTION TEST", text_size=10, width=4700),
                    "AREA_HEAD": dict(x=13200, y=6700, text="ROOM SCHEDULE", text_size=13, width=4700),
                    "AREA_LIST": dict(
                        x=13200,
                        y=6250,
                        text=(
                            "01  Living / Dining       27.9 m2\n"
                            "02  Kitchen                18.0 m2\n"
                            "03  Bathroom                3.8 m2\n"
                            "04  Entrance / Hall         2.5 m2\n"
                            "05  Bedroom 2              10.9 m2\n"
                            "06  Bedroom 1              16.5 m2\n\n"
                            "NET PROGRAM AREA          79.6 m2"
                        ),
                        text_size=10,
                        width=4500,
                    ),
                    "NOTES_HEAD": dict(x=13200, y=3850, text="GENERAL NOTES", text_size=13, width=4700),
                    "NOTES": dict(
                        x=13200,
                        y=3450,
                        text=(
                            "1. Verify dimensions on site.\n"
                            "2. Do not scale from this drawing.\n"
                            "3. Doors and windows are wall-hosted BIM objects.\n"
                            "4. Room names/numbers are native Space objects."
                        ),
                        text_size=9,
                        width=4500,
                    ),
                }
                space_objects = [
                    item
                    for name, item in named.items()
                    if name.startswith(f"{prefix}_SPACE_") and item.get("type") == "space"
                ]
                if len(space_objects) != 6:
                    raise RuntimeError("expected exactly six named apartment Space objects")
                operations: list[dict[str, Any]] = [
                    {
                        "type": "set_properties",
                        "params": {
                            "edits": [
                                {
                                    "ref": f"uuid:{slab['uuid']}",
                                    "properties": {"fillPattern": 0},
                                },
                                {
                                    "ref": f"uuid:{border['uuid']}",
                                    "properties": {"fillPattern": 0},
                                },
                                *[
                                    {
                                        "ref": f"uuid:{space['uuid']}",
                                        "properties": {"fillPattern": 0},
                                    }
                                    for space in space_objects
                                ],
                            ]
                        },
                    }
                ]
                for suffix, spec in text_specs.items():
                    name = f"{prefix}_{suffix}"
                    old = named.get(name)
                    if old is None or old.get("type") != "text":
                        raise RuntimeError(f"required annotation is missing: {name}")
                    operations.append({"type": "delete", "params": {"target": f"uuid:{old['uuid']}"}})
                    class_name = "A-Anno-Title" if suffix in {"TITLE", "SUBTITLE", "AREA_HEAD", "NOTES_HEAD"} else "A-Anno-Text"
                    operations.append(
                        create(
                            f"replace-{suffix.lower()}",
                            object_type="text",
                            name=name,
                            class_name=class_name,
                            wrap=True,
                            **spec,
                        )
                    )

                result = await call(
                    "vw_apply",
                    {
                        "operations": operations,
                        "idempotency_key": f"{prefix}-visual-repair-v4",
                    },
                )
                await call("vw_document", {"action": "save", "file_path": str(file_path)})
                pdf_path = file_path.with_name(f"{file_path.stem}-final-visible.pdf")
                await call(
                    "vw_io",
                    {"action": "export", "format": "pdf", "file_path": str(pdf_path)},
                )
                summary = await call("vw_read", {"action": "summary", "limit": 200})
                print(
                    json.dumps(
                        {
                            "transaction": result.get("data", result),
                            "summary": summary.get("data"),
                            "vwx": str(file_path),
                            "pdf": str(pdf_path),
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )


if __name__ == "__main__":
    anyio.run(main)
