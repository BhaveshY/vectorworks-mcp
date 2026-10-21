"""Post-install smoke test: the INSTALLED launcher, end to end, the way Codex/Claude start it.

  py -3.12 scripts\\smoke_installed.py            (Vectorworks must be running with the bridge)
  py -3.12 scripts\\smoke_installed.py --no-vw    (only launcher + tools/list + vw_status)

Steps: initialize + tools/list (10 tools, timed) -> vw_status (bridge ready) -> new UNTITLED document ->
vw_create 4 walls + door + window + space -> vw_read counts -> vw_capture (image file on disk) ->
close the document discarding changes. Never opens, saves or touches any other document.
Prints PASS/FAIL lines and a final JSON line {"ok": ..., "checks": [...]}; exit code 0 only when all pass.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

LAUNCHER = pathlib.Path(os.environ.get("VW_MCP_LAUNCHER")
                        or pathlib.Path(os.environ["USERPROFILE"]) / ".vectorworks-mcp" / "bin" / "vectorworks-mcp.cmd")


class Session:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(["cmd.exe", "/d", "/c", str(LAUNCHER)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.next_id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed stdout (process died)")
            reply = json.loads(line)
            if reply.get("id") == self.next_id:
                return reply

    def notify(self, method: str) -> None:
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode())
        self.proc.stdin.flush()

    def tool(self, name: str, args: dict | None = None) -> dict:
        reply = self.request("tools/call", {"name": name, "arguments": args or {}})
        return (reply.get("result") or {}).get("structuredContent") or {}

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def _count(read: dict) -> int | None:
    page = read.get("page") or {}
    for key in ("total", "returned"):
        if isinstance(page.get(key), int):
            return page[key]
    data = read.get("data")
    if isinstance(data, dict):
        data = data.get("rows")
    return len(data) if isinstance(data, list) else None


def main() -> int:
    no_vw = "--no-vw" in sys.argv
    checks: list[dict] = []

    def check(name: str, ok: bool, detail=None) -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  " + json.dumps(detail, default=str)[:500]), flush=True)
        return bool(ok)

    if not check("launcher exists", LAUNCHER.is_file(), str(LAUNCHER)):
        return _finish(checks)
    t0 = time.perf_counter()
    s = Session()
    doc = None
    try:
        init = s.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "smoke-installed", "version": "1"}})
        s.notify("notifications/initialized")
        tools = (s.request("tools/list").get("result") or {}).get("tools") or []
        ms = round((time.perf_counter() - t0) * 1000)
        names = sorted(t.get("name") for t in tools)
        check("initialize + tools/list (10 tools, < 2000 ms)", len(tools) == 10 and ms < 2000,
              {"ms": ms, "tools": names, "server": (init.get("result") or {}).get("serverInfo")})

        st = s.tool("vw_status")
        if no_vw:
            check("vw_status answers", isinstance(st, dict) and "ok" in st, st)
            return _finish(checks)
        if not check("vw_status ready (Vectorworks + bridge connected)", st.get("ready") is True, st):
            return _finish(checks)

        new = s.tool("vw_document", {"action": "new"})
        doc = ((new.get("document") or {}).get("fingerprint"))
        if not check("new untitled document", bool(doc), new):
            return _finish(checks)
        P = {"document": doc}
        made = s.tool("vw_create", {**P, "objects": [
            {"type": "wall", "id": "s", "start": [0, 0], "end": [5000, 0]},
            {"type": "wall", "id": "e", "start": [5000, 0], "end": [5000, 4000]},
            {"type": "wall", "id": "n", "start": [5000, 4000], "end": [0, 4000]},
            {"type": "wall", "id": "w", "start": [0, 4000], "end": [0, 0]},
            {"type": "door", "wall": "$s", "at": [2500, 0], "width": 900},
            {"type": "window", "wall": "$n", "at": [2500, 4000], "width": 1500},
            {"type": "space", "name": "Smoke Test", "points": [[0, 0], [5000, 0], [5000, 4000], [0, 4000]]}]})
        check("vw_create walls + door + window + space", made.get("ok") is True, made)

        counts = {}
        for kind in ("wall", "door", "window", "space"):
            counts[kind] = _count(s.tool("vw_read", {**P, "what": "query", "type": kind}))
        check("vw_read counts 4/1/1/1", counts == {"wall": 4, "door": 1, "window": 1, "space": 1}, counts)

        cap = s.tool("vw_capture", P)
        path = cap.get("path") or ""
        size = cap.get("image_size")
        check("vw_capture wrote an image", cap.get("ok") is True and bool(path) and pathlib.Path(path).is_file(),
              {"path": path, "image_size": size, "error": cap.get("error")})
    except Exception as exc:  # noqa: BLE001 - report, never hang
        check("no exception", False, repr(exc))
    finally:
        if doc:
            closed = s.tool("vw_document", {"document": doc, "action": "close", "discard_changes": "discard"})
            check("closed the test document without saving", closed.get("ok") is True, closed)
        s.close()
    return _finish(checks)


def _finish(checks: list[dict]) -> int:
    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "passed": sum(c["ok"] for c in checks), "total": len(checks),
                      "failed": [c["name"] for c in checks if not c["ok"]]}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
