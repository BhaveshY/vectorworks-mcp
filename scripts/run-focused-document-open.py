"""Run one non-retrying Vectorworks document-open regression through MCP."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("document", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument(
        "--auth-token-file",
        type=Path,
        default=Path.home() / ".vectorworks-mcp" / "auth-token",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    target = args.document.expanduser().resolve()
    token_file = args.auth_token_file.expanduser().resolve()
    if not target.is_file() or target.suffix.lower() != ".vwx":
        raise SystemExit("document must be an existing .vwx file")
    if not token_file.is_file():
        raise SystemExit("authentication token file is missing")

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
    report: dict[str, object] = {"target": str(target), "retried": False}
    exit_code = 0
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=args.timeout_seconds),
            ) as session:
                await session.initialize()
                started = time.perf_counter()
                result = await session.call_tool(
                    "vw_document", {"action": "open", "file_path": str(target)}
                )
                report["elapsed_ms"] = round(
                    (time.perf_counter() - started) * 1000.0, 3
                )
                payload = result.structuredContent or {}
                report["open"] = payload
                if result.isError or payload.get("ok") is False:
                    exit_code = 2
                else:
                    readback = await session.call_tool("vw_document", {"action": "info"})
                    readback_payload = readback.structuredContent or {}
                    report["readback"] = readback_payload
                    data = readback_payload.get("data") or {}
                    active = Path(str(data.get("filepath", ""))).resolve()
                    report["exact_path_match"] = active == target
                    if readback.isError or not report["exact_path_match"]:
                        exit_code = 3
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    anyio.run(main)
