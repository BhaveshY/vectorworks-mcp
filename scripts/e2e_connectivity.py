"""Connectivity e2e: one MCP session must survive a full Vectorworks restart.

Starts the INSTALLED launcher exactly like Codex/Claude do (stdio), then:
  1. initialize + tools/list timing
  2. vw_status / vw_read while Vectorworks runs
  3. restarts Vectorworks with scripts/dev-restart-vectorworks.ps1 (refuses if
     saved documents are open) while the MCP session stays open
  4. vw_status during the restart (must answer, never crash)
  5. vw_read after the restart on the SAME session
Prints a JSON report. Requires Vectorworks with the bridge installed.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAUNCHER = pathlib.Path(os.environ["USERPROFILE"]) / ".vectorworks-mcp" / "bin" / "vectorworks-mcp.cmd"


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


def main() -> int:
    report: dict = {"checks": []}

    def check(name: str, ok: bool, detail=None) -> None:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + name, "" if ok else json.dumps(detail)[:400], flush=True)

    t0 = time.perf_counter()
    s = Session()
    s.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "e2e-connectivity", "version": "1"}})
    s.notify("notifications/initialized")
    tools = s.request("tools/list")["result"]["tools"]
    startup_ms = round((time.perf_counter() - t0) * 1000)
    check("launcher answers tools/list < 1000 ms", startup_ms < 1000 and len(tools) == 10, {"ms": startup_ms})

    st = s.tool("vw_status")
    check("vw_status ready before restart", st.get("ready") is True, st)
    docs = s.tool("vw_read", {"what": "documents"})
    check("vw_read documents before restart", docs.get("ok") is True, docs)
    pid_before = (st.get("instances") or [{}])[0].get("pid")

    restart = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                str(ROOT / "scripts" / "dev-restart-vectorworks.ps1"), "-Force"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    during = []
    while restart.poll() is None:
        t = time.perf_counter()
        st = s.tool("vw_status")
        during.append({"ready": st.get("ready"), "ms": round((time.perf_counter() - t) * 1000),
                       "problem": (st.get("problems") or [{}])[0].get("code")})
        time.sleep(3)
    check("restart helper succeeded", restart.returncode == 0, restart.stdout.read().decode(errors="replace")[-600:])
    check("vw_status always answered during restart (never died)", all(d["ms"] < 10000 for d in during) and during,
          during[:6])
    check("vw_status reported the outage clearly", any(d["ready"] is False and d["problem"] for d in during), during[:6])

    deadline = time.time() + 120
    st = {}
    while time.time() < deadline:
        st = s.tool("vw_status")
        if st.get("ready"):
            break
        time.sleep(3)
    pid_after = (st.get("instances") or [{}])[0].get("pid")
    check("same MCP session sees the new Vectorworks instance", st.get("ready") is True and pid_after != pid_before,
          {"before": pid_before, "after": pid_after, "status": st})
    docs = s.tool("vw_read", {"what": "documents"})
    check("vw_read works after restart on the same session", docs.get("ok") is True, docs)
    new = s.tool("vw_document", {"action": "new"})
    fp = ((new.get("document") or new.get("data") or {}) or {}).get("fingerprint")
    made = s.tool("vw_create", {"document": fp, "objects": [{"type": "rect", "start": [0, 0], "end": [1000, 1000]}]})
    check("write works after restart on the same session", made.get("ok") is True, made)
    s.tool("vw_document", {"action": "close", "document": fp, "discard_changes": "discard"})
    check("MCP process still alive", s.proc.poll() is None)
    s.proc.stdin.close()
    report["ok"] = all(c["ok"] for c in report["checks"])
    out = ROOT / "test-artifacts" / "live-1.0" / "connectivity.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print("OK" if report["ok"] else "FAILED", out)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
