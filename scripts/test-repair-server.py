"""Drive vectorworks_mcp/repair_server.py over stdio and check the protocol.

Stdlib only. Exercises the legacy initialize handshake, the 2026-07-28
server/discover envelope, tools/list, tools/call (vw_status), unknown methods,
notifications and parse errors. When the `mcp` SDK is importable (e.g. run with
the runtime venv's python) it additionally connects with the real SDK client in
both legacy and auto mode.

Usage: python scripts/test-repair-server.py [--server PATH] [--launcher PATH.cmd]
Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_SERVER = REPO / "vectorworks_mcp" / "repair_server.py"
FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILURES.append(label)


def run_session(cmd: list[str], messages: list[dict | str], env: dict) -> tuple[list, str, float]:
    payload = "".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in messages)
    started = time.perf_counter()
    proc = subprocess.run(
        cmd, input=payload.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, timeout=30,
    )
    elapsed = time.perf_counter() - started
    lines = [l for l in proc.stdout.decode("utf-8").splitlines() if l.strip()]
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except ValueError:
            parsed.append({"__unparseable__": line})
    return parsed, proc.stderr.decode("utf-8", "replace"), elapsed


def by_id(responses: list, msg_id):
    for r in responses:
        if isinstance(r, dict) and r.get("id") == msg_id:
            return r
    return None


def stdio_checks(cmd: list[str], env: dict) -> None:
    legacy = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "vw_status", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "does/not/exist"},
        "{not json",
        {"jsonrpc": "2.0", "id": 5, "method": "ping"},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "vw_create", "arguments": {}}},
    ]
    responses, stderr, elapsed = run_session(cmd, legacy, env)
    check(all("__unparseable__" not in r for r in responses), "stdout carries only JSON-RPC frames")
    init = by_id(responses, 1)
    check(bool(init and init["result"]["protocolVersion"] == "2025-06-18"), "legacy initialize echoes protocolVersion")
    check(bool(init and "tools" in init["result"]["capabilities"]), "initialize advertises tools capability")
    tools = by_id(responses, 2)
    names = [t["name"] for t in (tools or {}).get("result", {}).get("tools", [])]
    check(names == ["vw_status"], "tools/list exposes only vw_status")
    call = by_id(responses, 3)
    sc = (call or {}).get("result", {}).get("structuredContent", {})
    check(sc.get("error", {}).get("code") == "setup_required", "vw_status returns setup_required")
    check(bool(sc.get("repair_command")), "vw_status returns a repair command")
    check((call or {}).get("result", {}).get("isError") is True, "isError is exactly ok is False")
    check(by_id(responses, 4) and by_id(responses, 4)["error"]["code"] == -32601, "unknown method -> -32601")
    check(any(r.get("error", {}).get("code") == -32700 for r in responses if isinstance(r, dict)), "parse error -> -32700")
    check(bool(by_id(responses, 5) and by_id(responses, 5).get("result") == {}), "ping answered")
    other = by_id(responses, 6)
    check(bool(other and other["result"]["structuredContent"]["error"]["code"] == "setup_required"),
          "any other tool also reports setup_required")
    check(len([r for r in responses if isinstance(r, dict) and r.get("method")]) == 0, "server sends no requests")
    print("     legacy session wall time: %.0f ms" % (elapsed * 1000))

    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "0"}}
    modern = [
        {"jsonrpc": "2.0", "id": "d", "method": "server/discover", "params": {"_meta": meta}},
        {"jsonrpc": "2.0", "id": "l", "method": "tools/list", "params": {"_meta": meta}},
        {"jsonrpc": "2.0", "id": "c", "method": "tools/call", "params": {"_meta": meta, "name": "vw_status", "arguments": {}}},
    ]
    responses, _, _ = run_session(cmd, modern, env)
    disc = by_id(responses, "d")
    res = (disc or {}).get("result", {})
    check("2026-07-28" in res.get("supportedVersions", []), "server/discover lists 2026-07-28")
    check(res.get("resultType") == "complete" and "ttlMs" in res and "cacheScope" in res,
          "discover result carries resultType/ttlMs/cacheScope")
    lres = (by_id(responses, "l") or {}).get("result", {})
    check(lres.get("resultType") == "complete" and [t["name"] for t in lres.get("tools", [])] == ["vw_status"],
          "modern tools/list ok")
    cres = (by_id(responses, "c") or {}).get("result", {})
    check(cres.get("_meta", {}).get("io.modelcontextprotocol/serverInfo", {}).get("name") == "vectorworks-mcp-repair",
          "modern results stamp serverInfo")

    bad = [{"jsonrpc": "2.0", "id": 1, "method": "server/discover",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2099-01-01"}}}]
    responses, _, _ = run_session(cmd, bad, env)
    err = (by_id(responses, 1) or {}).get("error", {})
    check(err.get("code") == -32022 and "2026-07-28" in err.get("data", {}).get("supported", []),
          "unknown modern version -> -32022 with supported list")


def _is_error(result) -> bool:
    return bool(getattr(result, "is_error", getattr(result, "isError", None)))


def _structured(result):
    return getattr(result, "structured_content", getattr(result, "structuredContent", None))


def sdk_checks(cmd: list[str], env: dict) -> None:
    try:
        import anyio  # noqa: F401
        from mcp.client.session import ClientSession  # noqa: F401
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.client._probe import negotiate_auto
    except Exception as exc:  # pragma: no cover - depends on interpreter
        print("skip mcp SDK client checks (%s)" % exc.__class__.__name__)
        return
    import anyio
    from mcp.client.session import ClientSession

    async def legacy_run():
        params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("vw_status", {})
                return [t.name for t in tools.tools], result

    async def auto_run():
        params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await negotiate_auto(session)
                tools = await session.list_tools()
                result = await session.call_tool("vw_status", {})
                return session.discover_result is not None, [t.name for t in tools.tools], result

    try:
        names, result = anyio.run(legacy_run)
        check(names == ["vw_status"], "mcp SDK (legacy handshake) lists vw_status")
        check(_is_error(result) and (_structured(result) or {}).get("error", {}).get("code") == "setup_required",
              "mcp SDK (legacy) call_tool vw_status -> setup_required")
    except Exception as exc:
        check(False, "mcp SDK legacy session: %r" % exc)
    try:
        modern, names, result = anyio.run(auto_run)
        check(modern, "mcp SDK auto mode negotiated the 2026-07-28 era via server/discover")
        check(names == ["vw_status"] and _is_error(result), "mcp SDK (modern) tools/list + call ok")
    except Exception as exc:
        check(False, "mcp SDK auto session: %r" % exc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=str(DEFAULT_SERVER))
    ap.add_argument("--launcher", help="also test a generated vectorworks-mcp.cmd (expects repair mode)")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["VW_MCP_STATE_DIR"] = tmp  # empty state -> "not installed"
        env["PYTHONUTF8"] = "1"
        cmd = [sys.executable, "-X", "utf8", "-u", args.server, "--reason", "test"]
        print("== repair server via %s" % sys.executable)
        stdio_checks(cmd, env)
        sdk_checks(cmd, env)
        if args.launcher:
            print("== launcher %s" % args.launcher)
            lcmd = [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", args.launcher]
            stdio_checks(lcmd, env)
    if FAILURES:
        print("\n%d check(s) failed" % len(FAILURES))
        return 1
    print("\nall repair-server checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
