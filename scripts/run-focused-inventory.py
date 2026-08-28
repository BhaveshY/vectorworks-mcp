"""Read the active Vectorworks document through the compact MCP surface."""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    token_file = Path.home() / ".vectorworks-mcp" / "auth-token"
    env = os.environ.copy()
    env.update(
        {
            "VW_MCP_HOST": "127.0.0.1",
            "VW_MCP_PORT": "9877",
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
                read, write, read_timeout_seconds=timedelta(seconds=60)
            ) as session:
                await session.initialize()
                result: dict[str, object] = {}
                for label, tool, args in (
                    ("document", "vw_read", {"action": "document"}),
                    ("view", "vw_view", {"action": "get"}),
                    ("summary", "vw_read", {"action": "summary", "limit": 200}),
                    (
                        "objects",
                        "vw_read",
                        {
                            "action": "query",
                            "criteria": "ALL",
                            "limit": 200,
                            "fields": [
                                "uuid",
                                "name",
                                "type",
                                "class_name",
                                "bounds",
                                "fillColor",
                                "fillPattern",
                                "penColor",
                                "lineWeight",
                                "opacity",
                            ],
                        },
                    ),
                ):
                    response = await session.call_tool(tool, args)
                    result[label] = response.structuredContent or {}
                print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    anyio.run(main)
