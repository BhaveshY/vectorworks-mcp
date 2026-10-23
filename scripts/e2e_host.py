"""End-to-end test of the Vectorworks MCP 1.0 host over stdio JSON-RPC.

Stdlib only. Launches ``python -X utf8 -m vectorworks_mcp`` as a subprocess and
drives it like an MCP client:

  1. startup: initialize -> tools/list time, tools/list size, annotations,
     schema hygiene, 2026-07-28 discover / per-request _meta / -32022;
  2. Vectorworks absent (VW_MCP_PORT -> closed port): vw_status never fails,
     tools fail fast with bridge_unreachable, validation envelopes;
  3. full write/read/capture flows against the protocol-v2 mock bridge
     (vectorworks_mcp.testing.mock_bridge), incl. jobs, progress, busy,
     multi-document pinning, script-backed features;
  4. pre-1.0 write paths against the mock in --legacy (revision 5) mode;
  5. --real: READ-ONLY calls against the running Vectorworks bridge on 9877
     (pre-1.0 compatibility). Never sends a write or a capture there.

Usage:  python scripts/e2e_host.py [--python <venv python>] [--real] [--keep]
Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS: list[tuple[str, bool, str]] = []
MOCK_TOKEN = "e2e-mock-token"


def check(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail and not condition else (f" ({detail})" if detail else "")))
    return bool(condition)


class Client:
    def __init__(self, python: str, env: dict, label: str, command: list[str] | None = None) -> None:
        self.label = label
        self.started = time.perf_counter()
        self.proc = subprocess.Popen(command or [python, "-X", "utf8", "-u", "-m", "vectorworks_mcp"], cwd=str(ROOT), env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.messages: "queue.Queue[dict]" = queue.Queue()
        self.notifications: list[dict] = []
        self.next_id = 0
        self.stderr: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.messages.put({"_garbage": line.decode("utf-8", "replace")})
                continue
            if "id" not in msg and "method" in msg:
                self.notifications.append(msg)
            else:
                self.messages.put(msg)

    def _read_err(self) -> None:
        for line in self.proc.stderr:
            self.stderr.append(line.decode("utf-8", "replace").rstrip())

    def send(self, msg: dict) -> None:
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 60.0, meta: dict | None = None) -> dict:
        self.next_id += 1
        rid = self.next_id
        params = dict(params or {})
        if meta:
            params["_meta"] = meta
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self.messages.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if msg.get("id") == rid:
                return msg
            if "_garbage" in msg:
                raise AssertionError("non-JSON on stdout: " + msg["_garbage"][:200])
        raise TimeoutError(f"{method} timed out after {timeout} s")

    def initialize(self) -> dict:
        reply = self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                            "clientInfo": {"name": "e2e", "version": "1"}}, timeout=20)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply

    def call(self, tool: str, args: dict | None = None, timeout: float = 120.0, progress: bool = False) -> tuple[dict, dict]:
        meta = {"progressToken": f"p{self.next_id + 1}"} if progress else None
        reply = self.request("tools/call", {"name": tool, "arguments": args or {}}, timeout=timeout, meta=meta)
        result = reply.get("result") or {}
        return result.get("structuredContent") or {}, result

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(10)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_mock(python: str, state_dir: Path, legacy: bool = False) -> tuple[subprocess.Popen, int]:
    args = [python, "-u", "-m", "vectorworks_mcp.testing.mock_bridge", "--port", "0", "--token", MOCK_TOKEN,
            "--state-dir", str(state_dir)]
    if legacy:
        args.append("--legacy")
    proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    line = proc.stdout.readline().decode().strip()
    assert line.startswith("READY "), line
    return proc, int(line.split()[1])


def mock_ctl(port: int, **params) -> dict:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    body = json.dumps({"id": "ctl", "action": "mock_set", "auth_token": MOCK_TOKEN, "params": params}).encode()
    s.sendall(struct.pack(">I", len(body)) + body)
    size = struct.unpack(">I", s.recv(4))[0]
    data = b""
    while len(data) < size:
        data += s.recv(size - len(data))
    s.close()
    return json.loads(data)


def base_env(state_dir: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VW_MCP_")}
    env.update({"VW_MCP_STATE_DIR": str(state_dir), "PYTHONIOENCODING": "utf-8"})
    env.update(extra)
    return env


def err_code(sc: dict) -> str:
    return (sc.get("error") or {}).get("code", "")


def problem_next_step(sc: dict, code: str) -> str:
    """vw_status lifts the FIRST problem's next_step to the top level (not repeated in the problem); later
    problems keep their own. Returns the next_step that applies to problem `code` ('' if absent)."""
    probs = sc.get("problems") or []
    for i, p in enumerate(probs):
        if p.get("code") == code:
            return p.get("next_step") or (sc.get("next_step", "") if i == 0 else "")
    return ""


# --------------------------------------------------------------------------
def phase_startup(python: str, state_dir: Path) -> None:
    print("\n== 1. startup, tools/list, protocol eras ==")
    times = []
    for _ in range(3):
        c = Client(python, base_env(state_dir, VW_MCP_PORT=str(free_port())), "startup")
        c.initialize()
        reply = c.request("tools/list")
        times.append((time.perf_counter() - c.started) * 1000)
        c.close()
    best = min(times)
    check("cold start initialize->tools/list < 2000 ms", best < 2000, f"{[round(t) for t in times]} ms")
    check("cold start < 600 ms (Codex grace)", best < 600, f"best {best:.0f} ms")
    tools = reply["result"]["tools"]
    raw = json.dumps(reply["result"], separators=(",", ":"))
    check("10 tools listed", len(tools) == 10, ", ".join(t["name"] for t in tools))
    check("tools/list size <= ~6k tokens", len(raw) / 4 <= 6500, f"{len(raw)} bytes ~{len(raw) // 4} tokens")
    # outputSchema (~2 KB of model context) is opt-in via VW_MCP_OUTPUT_SCHEMA=1; the title is top-level only
    # (2025-06-18+), never duplicated into annotations.
    c = Client(python, base_env(state_dir, VW_MCP_PORT=str(free_port()), VW_MCP_OUTPUT_SCHEMA="1"), "schema")
    c.initialize()
    with_schema = {t["name"]: t for t in c.request("tools/list")["result"]["tools"]}
    c.close()
    ro = {"vw_status", "vw_read", "vw_capture"}
    for t in tools:
        ann = t.get("annotations") or {}
        ok = ann.get("openWorldHint") is False and (ann.get("readOnlyHint") is True) == (t["name"] in ro)
        ok = ok and t["inputSchema"].get("additionalProperties") is False
        ok = ok and "outputSchema" not in t and isinstance((with_schema.get(t["name"]) or {}).get("outputSchema"), dict)
        ok = ok and bool(t.get("title")) and "title" not in ann
        ok = ok and len(t["description"]) <= 600 and "Example" in t["description"]
        check(f"tool hygiene {t['name']}", ok,
              json.dumps({"annotations": ann, "title": t.get("title"), "outputSchema": "outputSchema" in t,
                          "outputSchema with VW_MCP_OUTPUT_SCHEMA=1": "outputSchema" in (with_schema.get(t["name"]) or {})}))
    destructive = {t["name"] for t in tools if t["annotations"].get("destructiveHint")}
    check("destructive tools", destructive == {"vw_edit", "vw_apply", "vw_organize", "vw_document", "vw_script"},
          str(sorted(destructive)))

    c = Client(python, base_env(state_dir, VW_MCP_PORT=str(free_port())), "modern")
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}
    disc = c.request("server/discover", {}, meta=meta)
    res = disc.get("result", {})
    check("server/discover (2026-07-28)", "2026-07-28" in res.get("supportedVersions", []) and res.get("resultType") == "complete"
          and "ttlMs" in res and "cacheScope" in res, json.dumps(res)[:200])
    lst = c.request("tools/list", {}, meta=meta).get("result", {})
    check("modern tools/list carries resultType/ttlMs/serverInfo",
          lst.get("resultType") == "complete" and "ttlMs" in lst and "io.modelcontextprotocol/serverInfo" in (lst.get("_meta") or {}))
    bad = c.request("tools/list", {}, meta={"io.modelcontextprotocol/protocolVersion": "2099-01-01",
                                            "io.modelcontextprotocol/clientCapabilities": {}})
    check("unsupported version -> -32022", (bad.get("error") or {}).get("code") == -32022, json.dumps(bad)[:200])
    garbage = c.request("no/such/method")
    check("unknown method -> -32601", (garbage.get("error") or {}).get("code") == -32601)
    c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    c.proc.stdin.write(b"this is not json\n")
    c.proc.stdin.flush()
    ping = c.request("ping")
    check("survives garbage input; ping answers", ping.get("result") == {})
    c.close()


def phase_absent(python: str, state_dir: Path) -> None:
    print("\n== 2. Vectorworks absent (closed port) ==")
    (state_dir / "auth-token").write_text(MOCK_TOKEN, encoding="utf-8")
    c = Client(python, base_env(state_dir, VW_MCP_PORT=str(free_port())), "absent")
    c.initialize()
    t0 = time.perf_counter()
    sc, raw = c.call("vw_status")
    dt = (time.perf_counter() - t0) * 1000
    check("vw_status ok:true when Vectorworks is absent", sc.get("ok") is True and raw.get("isError") is False, json.dumps(sc)[:300])
    check("vw_status reports bridge_unreachable + next_step",
          any(p.get("code") == "bridge_unreachable" for p in sc.get("problems", [])) and sc.get("next_step"))
    check("vw_status fast with closed port", dt < 3000, f"{dt:.0f} ms")
    t0 = time.perf_counter()
    sc, raw = c.call("vw_read", {"what": "layers"})
    dt = (time.perf_counter() - t0) * 1000
    check("vw_read -> isError + bridge_unreachable envelope", raw.get("isError") is True and err_code(sc) == "bridge_unreachable"
          and sc["error"].get("next_step") and sc["error"].get("retryable") is True, json.dumps(sc)[:300])
    check("unreachable cached: second call fast", dt < 500, f"{dt:.0f} ms")
    # validation envelopes (nothing sent)
    cases = [
        ("vw_read", {"what": "bogus"}, "what"),
        ("vw_read", {}, "what"),
        ("vw_create", {"objects": [{"type": "wall", "start": [0, 0]}]}, "objects[0].end"),
        ("vw_create", {"objects": [{"type": "hexagon"}]}, "objects[0].type"),
        ("vw_create", {"objects": "not json"}, "objects"),
        ("vw_edit", {"changes": [{"op": "move", "target": "uuid:x"}]}, "changes[0].by"),
        ("vw_edit", {"changes": [{"op": "set", "target": "x"}]}, "changes[0].properties"),
        ("vw_edit", {"changes": [{"op": "order", "target": "x", "position": "sideways"}]}, "changes[0].position"),
        ("vw_capture", {"view": "panorama"}, "view"),
        ("vw_document", {"action": "export", "path": "relative.pdf"}, "path"),
        ("vw_apply", {"operations": [{"type": "explode", "params": {}}]}, "operations[1].type"),
        ("vw_organize", {"kind": "planet", "action": "create"}, "kind"),
        ("vw_create", {"objects": [{"type": "wall", "start": [0, 0], "end": [1000, 0]}], "units": "parsec"}, "units"),
    ]
    for tool, args, field in cases:
        sc, raw = c.call(tool, args)
        e = sc.get("error") or {}
        ok = raw.get("isError") is True and e.get("code") in {"validation_error", "path_error"} and e.get("next_step") \
            and e.get("commit_state") == "not_started" and (e.get("field") == field or field in str(e.get("field")))
        check(f"validation envelope {tool} {json.dumps(args)[:60]}", ok, json.dumps(e)[:300])
    sc, _ = c.call("vw_create", {"objects": [{"type": "wall", "id": "w1", "start": "0,0", "end": ["6m", 0]},
                                             {"type": "door", "wall": "$w1", "offset": "1.5m"}], "dry_run": True})
    ops = sc.get("operations") or []
    check("dry_run works without Vectorworks; '6m' -> 6000 mm; door offset placed",
          sc.get("ok") and len(ops) == 2 and ops[0]["params"]["end_x"] == 6000.0 and ops[1]["params"]["x1"] == 1500.0,
          json.dumps(sc)[:300])
    sc, _ = c.call("vw_create", {"floor_plan": {"rooms": [{"name": "Living", "origin": [0, 0], "width": 5000, "depth": 4000},
                                                          {"name": "Kitchen", "origin": [5000, 0], "width": 3000, "depth": 4000}],
                                                "doors": [{"at": [5000, 2000]}], "windows": [{"at": [2500, 0], "width": 1500}]},
                                 "dry_run": True})
    check("floor_plan dry_run (bim)", sc.get("ok") and sc.get("count", 0) >= 12, json.dumps(sc)[:200])
    sc, _ = c.call("vw_create", {"floor_plan": {"style": "schematic", "rooms": [{"x": 0, "y": 0, "width": 4000, "depth": 3000}],
                                                "doors": [{"hinge": [1000, 0], "swing": "right"}]}, "dry_run": True})
    check("floor_plan dry_run (schematic)", sc.get("ok") and sc.get("count") == 6, json.dumps(sc)[:200])
    sc, _ = c.call("vw_read", {"what": "plan_quality", "plan": {"rooms": []}})
    check("plan_quality host-only (lazy import)", "ok" in sc and (sc.get("ok") or err_code(sc) == "validation_error"),
          json.dumps(sc)[:200])
    res = c.request("resources/read", {"uri": "vw://guide"})
    text = res["result"]["contents"][0]["text"]
    check("vw://guide served (<= 2.5k tokens)", "vw_create" in text and len(text) / 4 <= 2500, f"{len(text)} chars")
    prompts = c.request("prompts/list")["result"]["prompts"]
    check("4 prompts", {p["name"] for p in prompts} == {"draw-floor-plan", "inspect-drawing", "export-sheets", "fix-connection"})
    got = c.request("prompts/get", {"name": "draw-floor-plan", "arguments": {"brief": "2 rooms"}})
    check("prompts/get", "2 rooms" in json.dumps(got))
    c.close()


def phase_mock(python: str, state_dir: Path) -> None:
    print("\n== 3. protocol-v2 mock bridge: write/read/capture flows ==")
    (state_dir / "auth-token").write_text(MOCK_TOKEN, encoding="utf-8")
    mock, port = start_mock(python, state_dir)
    try:
        c = Client(python, base_env(state_dir), "mock")
        c.initialize()
        sc, _ = c.call("vw_status")
        # compact status: a single instance is the instances[] row (no separate top-level "instance" key)
        rows = sc.get("instances") or []
        check("discovery via instances/*.json; ready", sc.get("ready") is True and len(rows) == 1
              and rows[0].get("port") == port and rows[0].get("instance") and "problems" not in sc, json.dumps(sc)[:300])
        sc, _ = c.call("vw_create", {"objects": [
            {"type": "wall", "id": "w1", "start": [0, 0], "end": ["6m", 0], "thickness": "200mm", "height": 2700},
            {"type": "wall", "id": "w2", "start": [6000, 0], "end": [6000, 4000]},
            {"type": "door", "wall": "$w1", "offset": 1500},
            {"type": "window", "wall": "$w2", "at": [6000, 2000], "width": "1.2m"},
            {"type": "rectangle", "origin": "0,5000", "width": "3m", "height": "2000", "name": "Box", "class": "A-Furn"},
            {"type": "text", "at": [100, 100], "text": "Living"},
            {"type": "circle", "center": [500, 500], "diameter": 400},
            {"type": "polyline", "points": [[0, 0], [100, 100], [200, 0]]}]})
        ids = sc.get("ids") or {}
        types = sc.get("types") or {}
        # compact receipt: ids + type counts + walls_joined + undo_steps (no per-opening rows / undo warnings);
        # the host walls are verified by the query below
        check("vw_create walls + door/window ($w1) + unit strings: walls native, openings by script into the wall, "
              "sizes native (3 undo steps, said so)",
              sc.get("ok") and len(ids) == 8 and sc.get("undo_steps") == 3 and sc.get("walls_joined") == 1
              and types.get("wall") == 2 and types.get("door") == 1 and types.get("window") == 1
              and "created" not in sc, json.dumps(sc)[:600])
        q, _ = c.call("vw_read", {"what": "query", "type": "door"})
        qw, _ = c.call("vw_read", {"what": "query", "type": "window"})
        d0, w0 = (q.get("data") or [{}])[0], (qw.get("data") or [{}])[0]
        check("inserted door/window: in their walls, width/height/sill set by the native parameter step",
              d0.get("wall") == ids.get("w1") and d0.get("width") == 900.0 and w0.get("wall") == ids.get("w2")
              and w0.get("width") == 1200.0 and w0.get("sill_height") == 900.0
              and (w0.get("parameters") or {}).get("SashLS") == 2 and (d0.get("parameters") or {}).get("SideLights") is False,
              json.dumps([d0, w0])[:600])
        sc, _ = c.call("vw_create", {"idempotency_key": "openings-replay", "objects": [
            {"type": "door", "id": "dx", "wall": ids.get("w1"), "at": [4500, 300], "width": 800, "side": "right",
             "hinge": "end", "swing": "Out", "open_angle": 45, "name": "D-02", "class": "A-Door"}]})
        q, _ = c.call("vw_read", {"what": "query", "type": "door"})
        dx = next((r for r in q.get("data") or [] if r.get("uuid") == (sc.get("ids") or {}).get("dx")), {})
        check("door into an existing wall: side=right/hinge=end -> right, no flip; name/class/params natively",
              sc.get("ok") and sc.get("undo_steps") == 2 and dx.get("right") is True and dx.get("flip") is False
              and dx.get("name") == "D-02" and dx.get("class") == "A-Door" and dx.get("width") == 800.0
              and dx.get("x1") == 4500.0 and dx.get("y1") == 0.0, json.dumps([sc, dx])[:700])
        sc2, _ = c.call("vw_create", {"idempotency_key": "openings-replay", "objects": [
            {"type": "door", "id": "dx", "wall": ids.get("w1"), "at": [4500, 300], "width": 800, "side": "right",
             "hinge": "end", "swing": "Out", "open_angle": 45, "name": "D-02", "class": "A-Door"}]})
        q2, _ = c.call("vw_read", {"what": "query", "type": "door"})
        check("resend with the same idempotency_key: openings not inserted twice",
              sc2.get("ok") and (sc2.get("ids") or {}).get("dx") == (sc.get("ids") or {}).get("dx")
              and len(q2.get("data") or []) == len(q.get("data") or []), json.dumps(sc2)[:300])
        nwalls = len((c.call("vw_read", {"what": "query", "type": "wall"})[0].get("data") or []))
        sc, _ = c.call("vw_create", {"objects": [
            {"type": "wall", "id": "cw", "start": [0, 9000], "end": [4000, 9000]},
            {"type": "door", "wall": "$cw", "at": [1000, 9000]},
            {"type": "door", "wall": ids.get("o5"), "at": [500, 5000]}]})
        after = len((c.call("vw_read", {"what": "query", "type": "wall"})[0].get("data") or []))
        check("door into a non-wall host: script fails, the walls of the call are deleted again (rolled_back)",
              err_code(sc) == "native_rejected" and (sc.get("error") or {}).get("commit_state") == "rolled_back"
              and after == nwalls, json.dumps(sc)[:400])
        c.call("vw_edit", {"changes": [{"op": "delete", "target": dx.get("uuid") or "uuid:none"}]})
        box = ids.get("o5")
        sc, _ = c.call("vw_read", {"what": "query", "type": "wall", "limit": 5000})
        check("query with limit clamped (warning), returns walls", sc.get("ok") and len(sc.get("data", [])) == 2 and
              any("clamped" in w for w in sc.get("warnings", [])), json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "query", "class": "A-Furn", "fields": "uuid,name"})
        check("query by class + fields projection (string list)", sc.get("ok") and sc["data"] == [{"uuid": box, "name": "Box"}],
              json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "query", "limit": "3"})
        check("paging: next_cursor", sc.get("page", {}).get("next_cursor") == "3", json.dumps(sc.get("page")))
        sc2, _ = c.call("vw_read", {"what": "query", "limit": 3, "cursor": sc["page"]["next_cursor"]})
        check("paging: second page differs", sc2.get("ok") and sc2["data"][0]["uuid"] != sc["data"][0]["uuid"])
        sc, _ = c.call("vw_read", {"what": "summary"})
        check("summary counts", sc.get("ok") and sc["data"]["total_objects"] == 8, json.dumps(sc)[:200])
        sc, raw = c.call("vw_read", {"what": "object", "target": box})
        text_len = len(json.dumps(sc))
        check("object detail compact by default (geometry, <= 1.5 KB, no records)", sc.get("ok") and
              sc["data"].get("uuid") == box and "records" not in sc["data"] and sc["data"].get("width") == 3000
              and text_len <= 1500, f"{text_len} B " + json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "object", "target": box, "detail": True})
        check("object detail=true adds records", sc.get("ok") and "records" in sc["data"], json.dumps(sc)[:300])
        sc, _ = c.call("vw_edit", {"changes": [
            {"op": "move", "target": box, "by": ["1m", 0]},
            {"op": "set", "target": box, "properties": {"fill_color": "#FF0000", "line_weight": "10", "name": "Red box"}},
            {"op": "rotate", "target": box, "angle": "45"},
            {"op": "duplicate", "target": box, "by": [0, 3000], "count": 2},
            {"type": "class", "target": box, "class": "A-Furn-2"}]})
        check("vw_edit move/set/rotate/duplicate/class in one transaction", sc.get("ok") and len(sc.get("ids", {})) == 2,
              json.dumps(sc)[:400])
        dup = list((sc.get("ids") or {}).values())[0] if sc.get("ids") else None
        sc, _ = c.call("vw_organize", {"kind": "layer", "action": "create", "name": "Furniture", "scale": 50})
        check("vw_organize layer create", sc.get("ok"), json.dumps(sc)[:300])
        sc, _ = c.call("vw_edit", {"changes": [
            {"op": "mirror", "target": box, "axis": "y", "center": [0, 0]},
            {"op": "layer", "target": dup, "layer": "Furniture"},
            {"op": "record", "target": box, "record": "Costs", "fields": {"Cost": "120"}},
            {"op": "group", "targets": [box, dup], "name": "G1"}]})
        check("script-backed edits (mirror/layer/record/group) via run_script", sc.get("ok") and sc.get("applied") >= 4,
              json.dumps(sc)[:400])
        sc, _ = c.call("vw_read", {"what": "records", "target": box})
        check("records read via script", sc.get("ok") and "Costs" in json.dumps(sc), json.dumps(sc)[:300])
        sc, raw = c.call("vw_capture", {"view": "objects", "objects": [box], "standard_view": "iso", "render_mode": "opengl"})
        content = raw.get("content") or []
        image = next((b for b in content if b.get("type") == "image"), None)
        link = next((b for b in content if b.get("type") == "resource_link"), None)
        check("vw_capture returns image + resource_link + restored", sc.get("ok") and image and link and sc.get("restored") is True,
              json.dumps(sc)[:300])
        if image:
            import base64
            data = base64.b64decode(image["data"])
            dims = None
            if data[:2] == b"\xff\xd8":
                i = 2
                while i < len(data):
                    marker, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
                    if marker in (0xC0, 0xC2):
                        dims = (int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big"))
                        break
                    i += 2 + length
            elif data[:4] == b"\x89PNG":
                dims = (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
            check("capture downscaled to <= 1568 px long edge", dims is not None and max(dims) <= 1568, str(dims))
        if link:
            res = c.request("resources/read", {"uri": link["uri"]})
            check("vw://capture/{name} serves the full-res file", "blob" in json.dumps(res)[:500])
        sc, _ = c.call("vw_view", {"standard_view": "top_plan", "fit": "drawing", "render_mode": "wireframe"})
        check("vw_view set (previous/current)", sc.get("ok") and sc.get("previous") and sc.get("current"), json.dumps(sc)[:300])
        sc, _ = c.call("vw_view", {})
        check("vw_view without args = get", sc.get("ok") and "current" in sc)
        for args in ({"kind": "class", "action": "create", "name": "A-Wall-Ext", "fill_color": "#808080"},
                     {"kind": "story", "action": "create", "name": "Level 1", "elevation": "3m"},
                     {"kind": "saved_view", "action": "create", "name": "Iso check"},
                     {"kind": "worksheet", "action": "write", "name": "Room Schedule", "cell": "B2", "value": "42"},
                     {"kind": "layer", "action": "set", "name": "Furniture", "visibility": "gray"}):
            sc, _ = c.call("vw_organize", args)
            check(f"vw_organize {args['kind']} {args['action']}", sc.get("ok"), json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "worksheet", "target": "Room Schedule", "range": "A1:B2"})
        check("worksheet range read", sc.get("ok") and sc["data"]["rows"][1][1] == "42", json.dumps(sc)[:300])
        for what in ("layers", "classes", "saved_views", "symbols", "worksheets", "resources", "view", "documents",
                     "capabilities", "parametric_schema", "sheets", "selection"):
            args = {"what": what}
            if what == "parametric_schema":
                args["target"] = "Door"
            sc, _ = c.call("vw_read", args)
            check(f"vw_read {what}", sc.get("ok"), json.dumps(sc)[:200])
        sc, _ = c.call("vw_read", {"what": "resources", "resource_type": "hatches"})
        # the internal "via" marker is pruned; the script listing is what splits document vs library names
        check("resources via script (hatches)", sc.get("ok") and "via" not in sc
              and {"Hatch A", "Aluminum HF"} <= set(sc.get("data") or []) and sc.get("in_document") == ["Hatch A", "Hatch B"],
              json.dumps(sc)[:200])
        # vw_apply legacy grammar with forgiving input (op alias, numeric strings, flat params)
        sc, _ = c.call("vw_apply", {"operations": [
            {"op": "create", "id": "r9", "object_type": "rect", "x1": "0", "y1": "0", "x2": "10", "y2": "5"},
            {"type": "transform", "params": {"target": "$r9", "dx": "1", "dy": 0}}], "coordinate_units": "m"})
        check("vw_apply 0.7 grammar + forgiving (op/type, strings, flat params, m units)", sc.get("ok") and sc.get("committed"),
              json.dumps(sc)[:300])
        key = sc.get("idempotency_key")
        st, _ = c.call("vw_status", {"idempotency_key": key})
        check("vw_status(idempotency_key) -> committed", (st.get("transaction") or {}).get("state") == "committed",
              json.dumps(st.get("transaction"))[:200])
        sc, _ = c.call("vw_apply", {"operations": [{"type": "create", "operation_id": "d1", "params": {
            "object_type": "door", "plugin_name": "Door", "wall_uuid": ids["w1"], "x": 3000, "y": 0, "width": 900,
            "height": 2100}}]})
        check("vw_apply door without descriptor_fingerprint (auto-filled)", sc.get("ok"), json.dumps(sc)[:300])
        sc, _ = c.call("vw_script", {"code": "result = {'n': 1 + 1}"})
        check("vw_script result", sc.get("ok") and sc.get("result") == {"n": 2}, json.dumps(sc)[:200])
        sc, raw = c.call("vw_script", {"code": "raise ValueError('boom')"})
        check("vw_script error -> script_error, rolled back", raw.get("isError") and err_code(sc) == "script_error"
              and sc["error"].get("commit_state") == "rolled_back", json.dumps(sc)[:300])
        # long operation: async job + progress notifications
        out_dir = Path(tempfile.mkdtemp(prefix="vwmcp-e2e-"))
        before = len(c.notifications)
        sc, _ = c.call("vw_document", {"action": "export", "path": str(out_dir / "set.pdf"), "range": "all_pages"}, progress=True)
        progress = [n for n in c.notifications[before:] if n.get("method") == "notifications/progress"]
        check("export pdf as async job with progress notifications", sc.get("ok") and (out_dir / "set.pdf").exists()
              and len(progress) >= 1, f"{len(progress)} progress, {json.dumps(sc)[:200]}")
        sc, raw = c.call("vw_document", {"action": "export", "path": str(out_dir / "set.pdf")})
        check("existing export target -> confirmation_required", err_code(sc) == "confirmation_required", json.dumps(sc)[:200])
        sc, _ = c.call("vw_document", {"action": "save_as", "path": str(out_dir / "e2e.vwx")})
        check("save_as untitled document (no dialog)", sc.get("ok") and (out_dir / "e2e.vwx").exists(), json.dumps(sc)[:200])
        # multi-document pinning
        mock_ctl(port, switch_to_new_document=True)
        sc, raw = c.call("vw_create", {"objects": [{"type": "line", "start": [0, 0], "end": [100, 0]}]})
        check("user switched document -> document_changed with next_step", err_code(sc) == "document_changed" and
              "open_documents" in json.dumps(sc), json.dumps(sc)[:300])
        sc, _ = c.call("vw_create", {"objects": [{"type": "line", "start": [0, 0], "end": [100, 0]}], "document": "e2e.vwx"})
        check("document=<name> targets + activates the original", sc.get("ok") and sc.get("document", {}).get("name") == "e2e.vwx",
              json.dumps(sc)[:300])
        sc, _ = c.call("vw_document", {"action": "list"})
        check("vw_document list shows 2 documents", sc.get("ok") and len(sc.get("documents", [])) == 2, json.dumps(sc)[:300])
        sc, raw = c.call("vw_document", {"action": "close", "document": "Untitled 2"})
        check("close untitled", sc.get("ok"), json.dumps(sc)[:200])
        # busy UI
        mock_ctl(port, ui_state="modal")
        t0 = time.perf_counter()
        sc, raw = c.call("vw_read", {"what": "layers"})
        dt = time.perf_counter() - t0
        check("modal dialog -> vectorworks_busy within budget", err_code(sc) == "vectorworks_busy" and dt < 10, f"{dt:.1f}s {json.dumps(sc)[:200]}")
        st, _ = c.call("vw_status")
        check("vw_status reports busy", st.get("ok") and not st.get("ready") and "vectorworks_busy" in json.dumps(st))
        mock_ctl(port, ui_state="idle")
        # bridge restart: kill + restart mock -> host reconnects transparently
        mock.kill()
        mock.wait(5)
        sc, raw = c.call("vw_read", {"what": "layers"})
        check("bridge gone -> bridge_unreachable (no hang)", err_code(sc) == "bridge_unreachable", json.dumps(sc)[:200])
        time.sleep(2.2)
        mock, port = start_mock(python, state_dir)
        sc, _ = c.call("vw_status")
        check("bridge restarted on a new port -> rediscovered", sc.get("ready") is True, json.dumps(sc)[:300])
        sc, raw = c.call("vw_capture", {})
        check("capture after restart", sc.get("ok"), json.dumps(sc)[:200])
        check("no stdout garbage / host alive", c.proc.poll() is None)
        c.close()
        shutil.rmtree(out_dir, ignore_errors=True)
    finally:
        mock.kill()


def phase_legacy_mock(python: str, state_dir: Path) -> None:
    print("\n== 4. pre-1.0 (revision 5) write paths against the mock --legacy ==")
    legacy_dir = state_dir / "legacy"
    legacy_dir.mkdir(exist_ok=True)
    (legacy_dir / "auth-token").write_text(MOCK_TOKEN, encoding="utf-8")
    mock, port = start_mock(python, legacy_dir, legacy=True)
    try:
        c = Client(python, base_env(legacy_dir, VW_MCP_PORT=str(port)), "legacy-mock")
        c.initialize()
        sc, _ = c.call("vw_status")
        check("pre-1.0 detected, upgrade recommended", "upgrade_recommended" in json.dumps(sc), json.dumps(sc)[:300])
        sc, _ = c.call("vw_create", {"objects": [{"type": "wall", "id": "w1", "start": [0, 0], "end": [5000, 0]},
                                                 {"type": "door", "wall": "$w1", "offset": 1000}]})
        check("rev5: walls + hosted door in two steps (warned)", sc.get("ok") and sc.get("undo_steps") == 2
              and any("two undo steps" in w for w in sc.get("warnings") or []), json.dumps(sc)[:600])
        sc, raw = c.call("vw_script", {"code": "result = 1"})
        check("rev5: vw_script -> upgrade_required with command", err_code(sc) == "upgrade_required" and
              "install" in sc["error"]["next_step"], json.dumps(sc)[:300])
        sc, _ = c.call("vw_capture", {"view": "drawing"})
        check("rev5: capture current view + warning (no select-all fit)", sc.get("ok") and sc.get("warnings"), json.dumps(sc)[:300])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "move", "target": "selection", "by": [10, 0]}]})
        check("rev5: empty selection -> not_found", err_code(sc) == "not_found", json.dumps(sc)[:200])
        c.close()
    finally:
        mock.kill()


def phase_edge(python: str, state_dir: Path) -> None:
    print("\n== 3b. edge cases: lost write reply, still_running jobs, token fallback, more actions ==")
    edge_dir = state_dir / "edge"
    edge_dir.mkdir(exist_ok=True)
    (edge_dir / "auth-token").write_text("stale-wrong-token", encoding="utf-8")
    mock, port = start_mock(python, edge_dir)
    out_dir = Path(tempfile.mkdtemp(prefix="vwmcp-edge-"))
    try:
        c = Client(python, base_env(edge_dir, VW_MCP_AUTH_TOKEN=MOCK_TOKEN, VW_MCP_READ_BUDGET="3"), "edge")
        c.initialize()
        sc, _ = c.call("vw_status")
        check("auth: stale token file, env token accepted (re-read per call)", sc.get("ready") is True, json.dumps(sc)[:300])
        mock_ctl(port, drop_reply_once="apply_operations")
        sc, raw = c.call("vw_create", {"objects": [{"type": "rect", "origin": [0, 0], "width": 10, "height": 10}],
                                       "idempotency_key": "edge-lost-1"})
        check("lost write reply resolved via transaction_status (no resend)", sc.get("ok") is True, json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "summary"})
        check("lost-reply write applied exactly once", (sc.get("data") or {}).get("total_objects") == 1, json.dumps(sc)[:200])
        mock_ctl(port, latency=2.0)
        sc, raw = c.call("vw_capture", {"view": "current"})
        job = (sc.get("error") or {}).get("details", {}).get("job_id")
        check("timeout_ms exceeded -> still_running + job_id (never a silent drop)", err_code(sc) == "still_running" and job,
              json.dumps(sc)[:300])
        mock_ctl(port, latency=0)
        time.sleep(2.5)
        st, _ = c.call("vw_status", {"job_id": job})
        check("vw_status(job_id) -> done", (st.get("job") or {}).get("state") == "done", json.dumps(st.get("job"))[:300])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "set", "target": "uuid:" + _first_uuid(c), "properties": {"name": "Sel me"}},
                                               ]})
        sc, _ = c.call("vw_edit", {"changes": [{"op": "select", "targets": ["Sel me"]}]})
        sel, _ = c.call("vw_read", {"what": "selection"})
        check("select by name -> selection", sc.get("ok") and len(sel.get("data", [])) == 1, json.dumps(sel)[:200])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "move", "target": "selection", "by": [5, 5]}]})
        check("move target='selection'", sc.get("ok") and sc.get("applied") == 1, json.dumps(sc)[:200])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "deselect"}]})
        sel, _ = c.call("vw_read", {"what": "selection"})
        check("deselect", sc.get("ok") and sel.get("data") == [], json.dumps(sel)[:200])
        img = out_dir / "photo.png"
        img.write_bytes(bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10]))
        dwg = out_dir / "base.dwg"
        dwg.write_bytes(b"AC1032")
        for args in ({"action": "import", "path": str(img), "at": [100, 100]}, {"action": "import", "path": str(dwg)},
                     {"action": "new"}, {"action": "activate", "document": "Untitled 1"},
                     {"action": "export", "path": str(out_dir / "plan.dwg")}, {"action": "export", "path": str(out_dir / "m.ifc")},
                     {"action": "export", "path": str(out_dir / "img.png")}, {"action": "info"}):
            sc, _ = c.call("vw_document", args, progress=True)
            check(f"vw_document {args['action']} {Path(str(args.get('path', ''))).suffix}", sc.get("ok"), json.dumps(sc)[:250])
        vwx = out_dir / "other.vwx"
        vwx.write_bytes(b"MOCKVWX")
        sc, _ = c.call("vw_document", {"action": "open", "path": str(vwx)}, progress=True)
        check("vw_document open (async job) re-pins", sc.get("ok") and sc.get("document", {}).get("name") == "other.vwx",
              json.dumps(sc)[:250])
        sc, _ = c.call("vw_view", {"save_view_as": "Edge view", "standard_view": "front"})
        check("vw_view save_view_as", sc.get("ok") and sc.get("saved_view_created") == "Edge view", json.dumps(sc)[:250])
        sc, _ = c.call("vw_organize", {"kind": "resource", "action": "import", "source": str(vwx), "name": "Chair",
                                       "resource_type": "symbols"})
        check("vw_organize resource import (script)", sc.get("ok"), json.dumps(sc)[:250])
        sc, raw = c.call("vw_organize", {"kind": "layer", "action": "delete", "name": "No such"})
        check("layer delete unknown -> not_found", err_code(sc) == "not_found", json.dumps(sc)[:250])
        sc, _ = c.call("vw_read", {"what": "documents"})
        check("documents lists 3", len(sc.get("data", [])) == 3, json.dumps(sc)[:300])
        # vs.* signature lint (host side, nothing sent)
        sc, raw = c.call("vw_script", {"code": "vs.SetClFillFore('A', vs.RGBToColorIndex(65535, 0, 0))"})
        err = sc.get("error") or {}
        # Type mismatches are warnings, not rejections (live 2026-10-11: Vectorworks
        # accepts a palette index for COLOR once scripts run in ScriptContext).
        check("vw_script lint: palette index into COLOR -> runs, with a COLOR warning",
              sc.get("ok") and any("COLOR" in w for w in sc.get("warnings", [])), json.dumps(sc)[:400])
        sc, _ = c.call("vw_script", {"code": "h = vs.HMirror(None, False, (0, 0), (1, 0))"})
        check("vw_script lint: unknown vs function", err_code(sc) == "script_lint_failed"
              and "vs.MirrorN" in json.dumps(sc), json.dumps(sc)[:300])
        sc, _ = c.call("vw_script", {"code": "x1, y1, x2, y2 = vs.GetBBox(vs.FSActLayer())"})
        check("vw_script lint: GetBBox unpack count is reported as a warning",
              "GetBBox" in json.dumps(sc.get("warnings", [])) or "GetBBox" in json.dumps(sc), json.dumps(sc)[:300])
        sc, _ = c.call("vw_script", {"code": "vs.SetClFillFore('A', (65535, 0, 0))\nvs.Rect(0, 0, 10, 10)\nresult = 1"})
        check("vw_script lint: valid COLOR tuple / expanded POINTs pass", sc.get("ok") and sc.get("result") == 1,
              json.dumps(sc)[:300])
        sc, _ = c.call("vw_script", {"code": "vs.NotARealCall()\nresult = 2", "lint": False})
        check("vw_script lint=false bypasses the lint", err_code(sc) == "script_error" and "NotARealCall" in json.dumps(sc),
              json.dumps(sc)[:300])
        # contained native crash -> script_crashed, then the engine is poisoned
        sc, raw = c.call("vw_script", {"code": "result = '__vw_mcp_mock_crash__'"})
        err = sc.get("error") or {}
        check("contained script crash -> script_crashed, commit_state unknown",
              raw.get("isError") and err.get("code") == "script_crashed" and err.get("commit_state") == "unknown"
              and "restart Vectorworks" in err.get("next_step", ""), json.dumps(sc)[:400])
        sc, _ = c.call("vw_script", {"code": "result = 1"})
        check("poisoned engine -> script_engine_unavailable (fail fast)", err_code(sc) == "script_engine_unavailable"
              and "restart Vectorworks" in (sc.get("error") or {}).get("next_step", ""), json.dumps(sc)[:300])
        sc, _ = c.call("vw_status", {})
        probs = [p for p in sc.get("problems", []) if p.get("code") == "script_engine_unavailable"]
        check("vw_status surfaces script_engine poisoned with restart next_step",
              bool(probs) and "save their work and restart Vectorworks" in problem_next_step(sc, "script_engine_unavailable")
              and sc.get("ready") is True  # native tools still work
              and any(r.get("script_engine") == "poisoned" for r in sc.get("instances", [])),
              json.dumps({k: sc.get(k) for k in ("ready", "problems", "next_step")})[:500])
        c.close()
    finally:
        mock.kill()
        shutil.rmtree(out_dir, ignore_errors=True)


def phase_production(python: str, state_dir: Path) -> None:
    """Drawing production (viewports + annotations), graphic attributes and 3D massing (script-backed)."""
    print("\n== 3d. sheets/viewports, graphic attributes, massing (run_script snippets vs mock vs.*) ==")
    prod_dir = state_dir / "prod"
    prod_dir.mkdir(exist_ok=True)
    (prod_dir / "auth-token").write_text(MOCK_TOKEN, encoding="utf-8")
    mock, port = start_mock(python, prod_dir)
    try:
        c = Client(python, base_env(prod_dir), "prod")
        c.initialize()
        sc, _ = c.call("vw_create", {"objects": [
            {"type": "rect", "id": "r", "origin": [0, 0], "width": 3000, "height": 2000},
            {"type": "line", "id": "l", "start": [0, -1000], "end": [3000, -1000]},
            {"type": "text", "id": "t", "at": [0, 2500], "text": "Plan"}]})
        ids = sc.get("ids") or {}
        check("setup objects", sc.get("ok") and len(ids) == 3, json.dumps(sc)[:300])
        # -- viewports ------------------------------------------------------------------------------------
        sc, _ = c.call("vw_organize", {
            "kind": "viewport", "action": "create", "sheet": "A-101", "name": "GF Plan", "layers": ["Design Layer-1"],
            "scale": "1:100", "view": "top_plan", "at": [0, 0], "crop": [[-500, -1500], [3500, 3000]],
            "classes": {"Dimension": "hide"}, "title": "Ground floor",
            "annotations": [{"type": "text", "at": [0, 2800], "text": "GF", "size": 14, "bold": True},
                            {"type": "dimension", "start": [0, 0], "end": [3000, 0], "offset": -500},
                            {"type": "line", "start": [0, -1200], "end": [3000, -1200], "arrows": "end"}]})
        res = sc.get("result") or {}
        vp = res.get("viewport") or {}
        check("viewport create: sheet made, 1:100, top_plan, crop, class override, 3 annotations, one undo step",
              sc.get("ok") and res.get("sheet_created") == "A-101" and vp.get("scale") == 100 and vp.get("view") == "top_plan"
              and vp.get("crop") is True and "Dimension" in vp.get("hidden_classes", []) and vp.get("title") == "Ground floor"
              and vp.get("layers") == ["Design Layer-1"]
              # the 3 annotations + the title as a Drawing Label (the only way a title shows on the sheet);
              # one undo step = one script transaction (rollback checked below), its "undo" marker is pruned
              and [a.get("type") for a in res.get("annotations_created") or []] == ["text", "dimension", "line", "drawing_label"]
              and all(a.get("uuid") for a in res["annotations_created"]) and not sc.get("warnings"),
              json.dumps(sc)[:500])
        vp_uuid = vp.get("uuid")
        sc, _ = c.call("vw_read", {"what": "viewports", "target": "A-101"})
        check("vw_read viewports by sheet NAME", sc.get("ok") and len(sc.get("data") or []) == 1, json.dumps(sc)[:300])
        sc, _ = c.call("vw_organize", {"kind": "viewport", "action": "set", "target": vp_uuid, "scale": 50,
                                       "view": "iso", "render_mode": "hidden_line", "at": [100, 50]})
        vp = (sc.get("result") or {}).get("viewport") or {}
        check("viewport set: scale/view/render/position read back", sc.get("ok") and vp.get("scale") == 50 and
              vp.get("view") == "iso" and vp.get("render_mode") == "hidden_line" and vp.get("center") == [100, 50],
              json.dumps(sc)[:400])
        sc, _ = c.call("vw_organize", {"kind": "viewport", "action": "update", "sheet": "A-101"})
        check("viewport update (all on sheet) regenerates", sc.get("ok") and (sc.get("result") or {}).get("updated") == [vp_uuid],
              json.dumps(sc)[:300])
        sc, _ = c.call("vw_organize", {"kind": "viewport", "action": "create", "sheet": "A-101", "layers": ["Nope"]})
        check("viewport create with unknown layer -> not_found, rolled back", err_code(sc) == "not_found" and
              "Nope" in json.dumps(sc), json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "viewports", "target": "A-101"})
        check("rolled back: still one viewport", len(sc.get("data") or []) == 1, json.dumps(sc)[:300])
        sc, _ = c.call("vw_organize", {"kind": "viewport", "action": "delete", "target": "GF Plan"})
        check("viewport delete needs confirm", err_code(sc) == "confirmation_required", json.dumps(sc)[:300])
        sc, _ = c.call("vw_organize", {"kind": "viewport", "action": "delete", "target": "GF Plan", "confirm": True})
        check("viewport delete with confirm", sc.get("ok") and (sc.get("result") or {}).get("deleted", {}).get("uuid") == vp_uuid,
              json.dumps(sc)[:300])
        sc, _ = c.call("vw_organize", {"kind": "viewport", "action": "set", "target": "x", "scale": "1:x"})
        check("viewport scale '1:x' -> validation_error", err_code(sc) == "validation_error", json.dumps(sc)[:200])
        # -- graphic attributes ---------------------------------------------------------------------------
        # compact: plain names + in_document; detail=true gives {name, source} rows
        sc, _ = c.call("vw_read", {"what": "resources", "type": "hatch"})
        data = sc.get("data") or []
        check("resources type=hatch lists document + library names", sc.get("ok") and all(isinstance(n, str) for n in data)
              and {"Hatch A", "Aluminum HF"} <= set(data) and "Hatch A" in (sc.get("in_document") or [])
              and "Aluminum HF" not in (sc.get("in_document") or []), json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "resources", "type": "hatch", "detail": True})
        names = {i.get("name"): i.get("source") for i in sc.get("data") or [] if isinstance(i, dict)}
        check("resources detail=true: source per name", sc.get("ok") and names.get("Hatch A") == "document"
              and names.get("Aluminum HF") == "library", json.dumps(sc)[:300])
        sc, _ = c.call("vw_edit", {"changes": [
            {"op": "set", "target": ids.get("r"), "properties": {"fill": "Aluminum HF", "line_style": "Clouds",
                                                                 "pen_color": "#FF0000"}},
            {"op": "set", "target": ids.get("t"), "properties": {"font": "Times New Roman", "text_size": "14pt",
                                                                 "text_style": "bold italic", "align": "center"}},
            {"op": "set", "target": ids.get("l"), "properties": {"arrows": "both", "line_type": "Dash"}}]})
        check("vw_edit styles: library hatch + line type imported, text font/size/style/align, arrows",
              sc.get("ok") and sc.get("applied", 0) >= 4 and len(sc.get("imported_resources") or []) == 2,
              json.dumps(sc)[:500])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "set", "target": ids.get("r"), "properties": {"fill": "No Such Hatch"}}]})
        check("unknown hatch -> not_found naming vw_read resources", err_code(sc) == "not_found" and
              "resources" in json.dumps(sc), json.dumps(sc)[:300])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "set", "target": ids.get("r"), "properties": {"font": "Arial"}}]})
        check("font on a rectangle -> rejected (rolled back)", err_code(sc) == "native_rejected", json.dumps(sc)[:300])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "set", "target": ids.get("r"), "properties": {"fill": "solid", "fill_color": "blue"}}]})
        check("fill solid + colour (native + script, two steps warned)", sc.get("ok") and
              any("two undo steps" in w for w in sc.get("warnings") or []), json.dumps(sc)[:300])
        # -- massing --------------------------------------------------------------------------------------
        sc, _ = c.call("vw_create", {"objects": [
            {"type": "extrude", "id": "m1", "points": [[0, 0], [8000, 0], [8000, 6000], [0, 6000]], "height": "9m",
             "elevation": 300, "fill": "Brick HF"},
            {"type": "column", "id": "c1", "at": [1000, 1000], "width": 400, "height": 3500},
            {"type": "wall", "id": "w", "start": [0, 0], "end": [8000, 0], "line_style": "Dash"}]})
        # compact receipt: undo_steps is the count (no undo warnings); the wall style shows up as what was picked
        # / imported, not as a styled[] echo
        check("vw_create extrude + column + wall style: all receipts, 3 undo steps (native, script, plug-in params) warned",
              sc.get("ok") and {"m1", "c1", "w"} <= set(sc.get("ids") or {}) and sc.get("undo_steps") == 3
              and sc.get("types") == {"wall": 1, "extrude": 1, "column": 1}
              and (sc.get("picked") or {}).get("w", {}).get("line_style") == "Dash"
              and {"type": "hatch", "name": "Brick HF"} in (sc.get("imported_resources") or []),
              json.dumps(sc)[:500])
        sc, _ = c.call("vw_create", {"objects": [{"type": "massing", "origin": [0, 0], "width": 5000, "depth": 4000,
                                                  "height": 3000}]})
        check("script-only create (extrude rect footprint) = one undo step", sc.get("ok") and sc.get("undo_steps") == 1
              and sc.get("types") == {"extrude": 1}, json.dumps(sc)[:300])
        sc, _ = c.call("vw_create", {"objects": [{"type": "column", "at": [0, 0], "diameter": 300}], "dry_run": True})
        check("dry_run lists script objects", sc.get("ok") and (sc.get("script_objects") or [{}])[0].get("kind") == "column",
              json.dumps(sc)[:300])
        sc, _ = c.call("vw_create", {"objects": [{"type": "extrude", "points": [[0, 0], [1, 0], [1, 1]]}]})
        check("extrude without height -> validation_error", err_code(sc) == "validation_error", json.dumps(sc)[:200])
        # -- Codex-run regressions (luna_sheet / astra_production) ---------------------------------------
        sc, _ = c.call("vw_organize", {"kind": "viewport", "action": "create", "sheet": "A-102", "view": "top_plan",
                                       "render_mode": "hidden_line", "scale": "1:50"})
        vp = (sc.get("result") or {}).get("viewport") or {}
        check("rendered top_plan viewport accepted as Top/orthogonal with a warning", sc.get("ok") and
              vp.get("projection") == "orthogonal" and any("Top/Plan" in w for w in sc.get("warnings") or []),
              json.dumps(sc)[:400])
        before = (c.call("vw_read", {"what": "summary"})[0].get("data") or {}).get("total_objects")
        sc, _ = c.call("vw_create", {"objects": [{"type": "rect", "origin": [0, 0], "width": 100, "height": 100},
                                                 {"type": "extrude", "origin": [0, 0], "width": 100, "depth": 100,
                                                  "height": 100, "fill": "No Such Hatch"}]})
        after = (c.call("vw_read", {"what": "summary"})[0].get("data") or {}).get("total_objects")
        check("mixed create, script part fails -> native part deleted again, commit_state rolled_back",
              err_code(sc) == "not_found" and (sc.get("error") or {}).get("commit_state") == "rolled_back"
              and before == after, f"{before} -> {after} {json.dumps(sc)[:300]}")
        sc, _ = c.call("vw_create", {"objects": [{"type": "extrude", "id": "x", "origin": [0, 0], "width": 100,
                                                  "depth": 100, "height": 100, "fill": "solid"}]})
        check("extrude fill solid lands on the profile", sc.get("ok"), json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "parametric_schema", "target": "Column2"})
        params = {p.get("id"): p for p in (sc.get("data") or {}).get("parameters") or []}
        check("parametric_schema Column2 (no native provider) via parametric record", sc.get("ok") and "via" not in sc
              and (sc.get("data") or {}).get("plugin") == "Column2" and params.get("OA Height", {}).get("type") == "length",
              json.dumps(sc)[:300])
        col = ((c.call("vw_create", {"objects": [{"type": "column", "id": "k", "at": [0, 0]}]})[0]).get("ids") or {}).get("k")
        sc, _ = c.call("vw_edit", {"changes": [{"op": "parametric", "target": col, "plugin": "Column2",
                                                "parameters": {"OA Height": "9m", "Use Capital": True, "Shaft Width": 450}}]})
        check("vw_edit parametric Column2 via parametric record (units, bool), read back", sc.get("ok") and
              sc.get("applied") == 1, json.dumps(sc)[:300])
        sc, _ = c.call("vw_create", {"objects": [{"type": "parametric", "plugin": "Column2", "at": [500, 0],
                                                  "parameters": {"OA Height": 2800}}]})
        check("vw_create parametric Column2 via parametric record", sc.get("ok") and
              sc.get("types") == {"parametric": 1} and len(sc.get("ids") or {}) == 1, json.dumps(sc)[:300])
        c.close()
    finally:
        mock.kill()


def phase_weak_model(python: str, state_dir: Path) -> None:
    """Regressions from a real weak-model Codex run (test-artifacts/codex-e2e/luna_room.jsonl)."""
    print("\n== 3c. weak-model room recipe: one-call room, door queries, forgiving vw_apply, Home screen ==")
    weak_dir = state_dir / "weak"
    weak_dir.mkdir(exist_ok=True)
    (weak_dir / "auth-token").write_text(MOCK_TOKEN, encoding="utf-8")
    mock, port = start_mock(python, weak_dir)
    try:
        c = Client(python, base_env(weak_dir), "weak")
        c.initialize()
        # Home screen in front, auto-dismiss off -> vw_status explains; dismiss_home closes it
        mock_ctl(port, auto_dismiss=False, modal_dialog={"title": "Vectorworks Home", "class": "Chrome_WidgetWin_1",
                                                         "kind": "home"})
        st, _ = c.call("vw_status")
        probs = [p for p in st.get("problems", []) if p.get("code") == "modal_dialog"]
        # auto-dismiss OFF: the Home screen really blocks (with auto-dismiss on it is only a note, never a blocker)
        check("vw_status: Home screen -> modal_dialog problem pointing to dismiss_home",
              st.get("ready") is False and bool(probs) and "dismiss_home" in problem_next_step(st, "modal_dialog")
              and "note" not in st, json.dumps({k: st.get(k) for k in ("ready", "problems", "next_step", "note")})[:400])
        sc, _ = c.call("vw_read", {"what": "layers"})
        check("busy with Home screen -> next_step names dismiss_home", err_code(sc) == "vectorworks_busy"
              and "dismiss_home" in (sc.get("error") or {}).get("next_step", ""), json.dumps(sc)[:300])
        sc, _ = c.call("vw_document", {"action": "dismiss_home"})
        check("vw_document(action='dismiss_home') closes it",
              sc.get("ok") and (sc.get("result") or {}).get("dismissed") is True, json.dumps(sc)[:300])
        st, _ = c.call("vw_status")
        check("vw_status ready after dismiss_home", st.get("ready") is True, json.dumps(st)[:300])
        mock_ctl(port, auto_dismiss=True, modal_dialog={"title": "Vectorworks 2026 is available", "kind": "update_available"})
        sc, _ = c.call("vw_read", {"what": "layers"})
        check("auto-dismissed update dialog -> call succeeds with a warning", sc.get("ok") and
              any("start-up dialog" in w for w in sc.get("warnings", [])), json.dumps(sc)[:300])
        mock_ctl(port, modal_dialog={"title": "Save changes?", "kind": "other"})
        sc, _ = c.call("vw_read", {"what": "layers"})
        check("other dialog -> busy, next_step asks the user to close it by title", err_code(sc) == "vectorworks_busy"
              and "Save changes?" in (sc.get("error") or {}).get("next_step", ""), json.dumps(sc)[:300])
        mock_ctl(port, auto_dismiss=True, modal_dialog={"title": "Vectorworks Home", "class": "Chrome_WidgetWin_1",
                                                        "kind": "home"})
        st, _ = c.call("vw_status")
        check("vw_status: Home screen with auto-dismiss on -> never a blocker (ready, no modal_dialog problem)",
              st.get("ready") is True and not any(p.get("code") in {"modal_dialog", "vectorworks_busy"}
                                                  for p in st.get("problems") or []), json.dumps(st)[:400])
        mock_ctl(port, modal_dialog=None)
        # canonical one-call room: walls + door/window into "$id" walls + space
        sc, _ = c.call("vw_create", {"units": "mm", "objects": [
            {"type": "door", "id": "d1", "wall": "south", "at": [3000, 0], "width": "900mm"},
            {"type": "wall", "id": "south", "start": [0, 0], "end": [6000, 0], "thickness": "200mm", "height": "2700mm"},
            {"type": "wall", "id": "east", "start": [6000, 0], "end": [6000, 4000]},
            {"type": "wall", "id": "north", "start": [6000, 4000], "end": [0, 4000]},
            {"type": "wall", "id": "west", "start": [0, 4000], "end": [0, 0]},
            {"type": "window", "id": "win1", "wall": "$north", "offset": "2400mm", "width": "1200mm"},
            {"type": "space", "id": "office", "name": "Office", "points": [[0, 0], [6000, 0], [6000, 4000], [0, 4000]]}]})
        ids = sc.get("ids") or {}
        # compact receipt: the joins are a count (failures listed in wall_joins_failed), openings are ids only
        check("one-call room (door listed before its wall, bare wall id) -> 7 objects, 4 L joins",
              sc.get("ok") and len(ids) == 7 and sc.get("walls_joined") == 4 and "wall_joins_failed" not in sc
              and sc.get("types") == {"wall": 4, "space": 1, "door": 1, "window": 1}, json.dumps(sc)[:500])
        obj, _ = c.call("vw_read", {"what": "object", "target": ids.get("d1", "")})
        door = obj.get("data") or {}
        check("receipt id of the door resolves to a door hosted by the 'south' wall",
              door.get("type") == "door" and ids.get("south") in (door.get("wall"), door.get("wall_uuid")),
              json.dumps(obj)[:400])
        sc, _ = c.call("vw_read", {"what": "query", "type": "door"})
        rows = sc.get("data") or []
        check("vw_read(query, type=door) lists doors in walls with wall + width",
              len(rows) == 1 and rows[0].get("wall") == ids.get("south") and rows[0].get("width") == 900.0,
              json.dumps(sc)[:400])
        sc, _ = c.call("vw_read", {"what": "query", "query": "Window"})
        check("query='Window' (weak model) treated as type", len(sc.get("data") or []) == 1, json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "query", "type": "rooms"})
        check("type='rooms' -> spaces", len(sc.get("data") or []) == 1 and sc["data"][0].get("type") == "space",
              json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "object", "target": ids.get("d1")})
        check("vw_read(object) finds a door inside a wall (exact uuid lookup)", sc.get("ok") and
              (sc.get("data") or {}).get("uuid") == ids.get("d1"), json.dumps(sc)[:300])
        # parametric Door with DoorWidth (read-only leaf width) as a numeric string
        sc, _ = c.call("vw_create", {"units": "mm", "objects": [
            {"type": "parametric", "plugin": "Door", "wall": ids.get("east"), "at": [6000, 2000],
             "parameters": {"DoorWidth": "900", "doorheight": "2100", "open angle": "45"}}]})
        check("parametric Door {DoorWidth:'900'} -> door width field, coerced params", sc.get("ok") and
              any("DoorWidth" in w for w in sc.get("warnings", [])), json.dumps(sc)[:400])
        sc, _ = c.call("vw_create", {"objects": [{"type": "parametric", "plugin": "Stair", "at": [1000, 1000],
                                                 "parameters": {"TotalRise": "2.7m", "NumRisers": "15", "StairType": 1}}]})
        check("parametric Stair: '2.7m' -> 2700, '15' -> int, popup -> text", sc.get("ok"), json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "query", "type": "stair"})
        check("vw_read(query, type=stair) -> plug-in object", len(sc.get("data") or []) == 1, json.dumps(sc)[:300])
        # vw_apply: every problem in one error, then the weak model's natural door/window shape works
        sc, _ = c.call("vw_apply", {"coordinate_units": "mm", "operations": [
            {"type": "create", "operation_id": "a1", "params": {"object_type": "door", "width": 900}},
            {"type": "create", "operation_id": "a2", "params": {"object_type": "window", "at": [3000, 4000]}},
            {"type": "create", "operation_id": "a3", "params": {"object_type": "rect", "x1": 0}}]})
        issues = ((sc.get("error") or {}).get("details") or {}).get("issues") or []
        check("vw_apply reports ALL issues at once (+ vw_create hint)", err_code(sc) == "validation_error"
              and len(issues) >= 4 and "vw_create" in sc["error"].get("next_step", ""), json.dumps(sc)[:600])
        sc, _ = c.call("vw_apply", {"coordinate_units": "mm", "operations": [
            {"type": "create", "operation_id": "door_apply", "params": {"object_type": "door", "wall": ids.get("south"),
                                                                        "at": [1500, 0], "width": 900}},
            {"type": "create", "operation_id": "window_apply", "params": {"object_type": "window", "wall": ids.get("west"),
                                                                          "x1": 0, "y1": 1400, "x2": 0, "y2": 2600}}]})
        check("vw_apply door/window with vw_create aliases + defaults", sc.get("ok") and sc.get("committed"),
              json.dumps(sc)[:400])
        check("vw_apply raw door/window create warns (dropped on wall regeneration -> use vw_create)",
              any("regenerated" in w and "vw_create" in w for w in sc.get("warnings") or []), json.dumps(sc)[:400])
        sc, _ = c.call("vw_apply", {"coordinate_units": "mm", "join_walls": False, "operations": [
            {"type": "create", "operation_id": "x1", "params": {"object_type": "wall", "x1": 6000, "y1": 4000,
                                                                "x2": 9000, "y2": 4000}}]})
        check("vw_apply join_walls=false -> no joins", sc.get("ok") and not sc.get("wall_joins"), json.dumps(sc)[:300])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "delete", "target": ids.get("south")}]})
        check("deleting a wall reports cascaded_inserts (its doors)", sc.get("ok")
              and len(sc.get("cascaded_inserts") or []) == 2, json.dumps(sc)[:400])
        c.close()
    finally:
        mock.kill()


NEW_BRIDGE_FEATURES = ["op_layer", "order_op", "auto_send_to_back", "capture_isolation", "compact_import_receipts"]


def phase_findings(python: str, state_dir: Path, features: list) -> None:
    """E2E findings 2026-10-14 (#1-#21): flat/typed edits, friendly parametric names, Space room names, compact
    reads, did-you-mean, search filters, layer placement, op-cap chunking, compact receipts, order, previews.
    Run against the bridge 1.0.0 behaviour (no features: script fallbacks) and the 1.0.x flags (native paths)."""
    label = "1.0.x flags" if features else "bridge 1.0.0"
    print(f"\n== 3e. 2026-10-14 findings ({label}) ==")
    tag = "new" if features else "old"
    fdir = state_dir / f"findings-{tag}"
    fdir.mkdir(exist_ok=True)
    (fdir / "auth-token").write_text(MOCK_TOKEN, encoding="utf-8")
    mock, port = start_mock(python, fdir)
    try:
        if features:
            mock_ctl(port, features=features)
        c = Client(python, base_env(fdir), f"findings-{tag}")
        c.initialize()
        # -- room on a named layer (#14), spaces behind walls (#1) -------------------------------------------
        room = [{"type": "wall", "id": "s", "start": [0, 0], "end": [6000, 0]},
                {"type": "wall", "id": "e", "start": [6000, 0], "end": [6000, 4000]},
                {"type": "wall", "id": "n", "start": [6000, 4000], "end": [0, 4000]},
                {"type": "wall", "id": "w", "start": [0, 4000], "end": [0, 0]},
                {"type": "door", "id": "d", "wall": "$s", "at": [3000, 0]},
                {"type": "window", "id": "win", "wall": "$n", "at": [3000, 4000], "sill": 900},
                {"type": "space", "id": "sp", "name": "Office", "points": [[0, 0], [6000, 0], [6000, 4000], [0, 4000]]}]
        sc, _ = c.call("vw_create", {"objects": room, "layer": "Frist"})
        check(f"[{tag}] unknown layer -> not_found, nothing created", err_code(sc) == "not_found" and
              "create_layer" in json.dumps(sc), json.dumps(sc)[:300])
        sc, raw = c.call("vw_create", {"objects": room, "layer": "First", "create_layer": True, "preview": False})
        ids = sc.get("ids") or {}
        check(f"[{tag}] create_layer + layer: receipt names the layer", sc.get("ok") and sc.get("layer") == "First"
              and len(ids) == 7, json.dumps(sc)[:400])
        q, _ = c.call("vw_read", {"what": "query", "layer": "First", "fields": ["uuid", "type", "layer"]})
        on_layer = {i.get("uuid") for i in q.get("data") or []}
        check(f"[{tag}] every object is on layer First (doors/windows in their walls)",
              {ids.get("s"), ids.get("e"), ids.get("n"), ids.get("w"), ids.get("sp")} <= on_layer, json.dumps(q)[:300])
        native_layer = "op_layer" in features
        check(f"[{tag}] layer placement path: {'native per-op layer' if native_layer else 'script fallback in the same call'}",
              ("layer_fallback" in sc) != native_layer, json.dumps(sc)[:300])
        # the receipt no longer says so (documented default): check the stacking order (query = back to front)
        stack = [i.get("uuid") for i in q.get("data") or []]
        walls = [ids.get(k) for k in ("s", "e", "n", "w")]
        check(f"[{tag}] space sent behind the walls", ids.get("sp") in stack and all(w in stack for w in walls)
              and stack.index(ids.get("sp")) < min(stack.index(w) for w in walls), json.dumps(q)[:300])
        sc_rect, _ = c.call("vw_create", {"objects": [{"type": "rect", "id": "r", "origin": [8000, 0], "width": 1000,
                                                       "height": 500, "name": "Box"},
                                                      {"type": "text", "id": "t", "at": [8000, 2000], "text": "Label"}]})
        rid, tid = (sc_rect.get("ids") or {}).get("r"), (sc_rect.get("ids") or {}).get("t")
        # -- flat set routed by type (#2), friendly parametric (#5), Space name (#7) -------------------------
        sc, _ = c.call("vw_edit", {"changes": [{"op": "set", "target": ids.get("d"), "width": 1000, "open_angle": 45}]})
        check(f"[{tag}] flat set on a door -> parametric Width/OpenAngle", sc.get("ok") and
              any(r.get("op") == "object.update_parametric" for r in sc.get("receipts") or []), json.dumps(sc)[:300])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "parametric", "target": ids.get("win"),
                                                "parameters": {"sill_height": 800}}]})
        check(f"[{tag}] parametric without plugin: plugin from target, sill_height -> Elevation",
              sc.get("ok") and any("Elevation" in w for w in sc.get("warnings") or []), json.dumps(sc)[:300])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "set", "target": ids.get("w"), "thickness": 300, "height": 2800}]})
        check(f"[{tag}] flat set on a wall -> thickness/height (script, read back)", sc.get("ok") and
              (sc.get("receipts") or [{}])[0].get("set") == {"thickness": 300, "height": 2800}, json.dumps(sc)[:300])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "set", "target": rid, "width": 2000}]})
        check(f"[{tag}] width on a rectangle -> validation naming rect + matching example",
              err_code(sc) == "validation_error" and "rect" in (sc.get("error") or {}).get("message", "") and
              (sc.get("error") or {}).get("example", {}).get("op") == "scale", json.dumps(sc)[:300])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "set", "target": tid, "text": "Kitchen"}]})
        check(f"[{tag}] flat text on a text object", sc.get("ok"), json.dumps(sc)[:300])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "set", "target": ids.get("sp"), "name": "Kitchen"}]})
        row = (sc.get("receipts") or [{}])[-1]
        check(f"[{tag}] Space name = displayed room label (read back)", sc.get("ok") and row.get("room_name") == "Kitchen",
              json.dumps(sc)[:300])
        q, _ = c.call("vw_read", {"what": "query", "type": "space"})
        row = (q.get("data") or [{}])[0]
        # compact rows: room_name only when it differs from the object name
        check(f"[{tag}] query reports room_name next to the object name",
              (row.get("room_name") or row.get("name")) == "Kitchen" and row.get("room_name") != row.get("name"),
              json.dumps(q)[:300])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "parametric", "target": ids.get("d"), "parameters": {"swng": "Out"}}]})
        check(f"[{tag}] unknown parameter -> did-you-mean", err_code(sc) == "validation_error" and
              "did you mean" in json.dumps(sc), json.dumps(sc)[:300])
        # -- order (#1) ---------------------------------------------------------------------------------------
        sc, _ = c.call("vw_edit", {"changes": [{"op": "send_to_back", "target": rid}]})
        check(f"[{tag}] op=order (send_to_back synonym)", sc.get("ok") and
              any(r.get("position") == "back" for r in sc.get("receipts") or []), json.dumps(sc)[:300])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "order", "target": rid, "position": "front"}]})
        check(f"[{tag}] op=order front", sc.get("ok"), json.dumps(sc)[:300])
        # -- compact reads (#3, #4, #6) ------------------------------------------------------------------------
        sc, _ = c.call("vw_read", {"what": "object", "target": ids.get("w")})
        data = sc.get("data") or {}
        check(f"[{tag}] wall read: start/end/thickness/height, <= 1.5 KB", data.get("start") == [0, 4000] and
              data.get("thickness") == 300 and len(json.dumps(sc)) <= 1500, json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "parametric_schema", "target": "Door"})
        check(f"[{tag}] parametric_schema compact (id/name/type/value)", sc.get("ok") and
              all(set(p) <= {"id", "name", "type", "value", "read_only"} for p in sc["data"]["parameters"]),
              json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "parametric_schema", "target": "Window", "search": "sill"})
        check(f"[{tag}] schema search: friendly 'sill' finds Elevation", sc.get("ok") and
              any(p.get("id") == "Elevation" for p in sc["data"]["parameters"]), json.dumps(sc)[:300])
        # -- search filters (#9, #18), sheet alias (#15), did-you-mean (#8) ------------------------------------
        sc, _ = c.call("vw_read", {"what": "resources", "type": "hatch", "search": "brick"})
        check(f"[{tag}] resources search filters by name", sc.get("ok") and sc.get("data") == ["Brick HF"],
              json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "resources", "search": "dash"})
        check(f"[{tag}] resources search across kinds", sc.get("ok") and
              {i.get("type") for i in sc.get("data") or []} >= {"line_type"}, json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "query", "query": "bo"})
        check(f"[{tag}] query = name/text contains", [i.get("uuid") for i in sc.get("data") or []] == [rid],
              json.dumps(sc)[:300])
        sc, _ = c.call("vw_edit", {"changes": [{"op": "set", "target": rid, "fill": "Brik HF"}]})
        check(f"[{tag}] unknown hatch -> did you mean 'Brick HF'", err_code(sc) == "not_found" and
              "Brick HF" in json.dumps(sc), json.dumps(sc)[:300])
        sc, _ = c.call("vw_create", {"objects": [{"type": "symbol", "symbol": "Chiar", "at": [0, 0]}]})
        check(f"[{tag}] unknown symbol -> did you mean 'Chair'", err_code(sc) == "not_found" and "Chair" in json.dumps(sc),
              json.dumps(sc)[:300])
        sc, _ = c.call("vw_read", {"what": "viewports", "sheet": "A-404"})
        check(f"[{tag}] viewports accepts sheet= as target", err_code(sc) != "validation_error", json.dumps(sc)[:300])
        # -- big plans: chunking + compact receipts (#19, #20) + preview ---------------------------------------
        big = [{"type": "rect", "origin": [(i % 25) * 700, 10000 + (i // 25) * 700], "width": 500, "height": 500}
               for i in range(250)]
        sc, raw = c.call("vw_create", {"objects": big, "layer": "Bulk", "create_layer": True})
        size = len(json.dumps(sc))
        image = any(b.get("type") == "image" for b in raw.get("content") or [])
        check(f"[{tag}] 250 objects: compact receipt (count + uuids once) <= 12 KB", sc.get("ok") and
              sc.get("count") == 250 and len(sc.get("uuids") or []) == 250 and "created" not in sc and
              "ids" not in sc and size <= 12_500, f"{size} B")
        # the LOOK hint lives in next_step (the preview block only describes the image)
        check(f"[{tag}] >= 10 objects -> preview image in the same result + LOOK hint", image and
              (sc.get("preview") or {}).get("view") == "top_plan" and "LOOK" in sc.get("next_step", ""),
              json.dumps({k: sc.get(k) for k in ("preview", "next_step")})[:300])
        big = [{"type": "rect", "origin": [(i % 25) * 700, 40000 + (i // 25) * 700], "width": 500, "height": 500}
               for i in range(400)]
        sc, _ = c.call("vw_create", {"objects": big, "preview": False})
        want_steps = 1 if features else 2
        check(f"[{tag}] 400 objects -> {want_steps} transaction(s) (native cap {2000 if features else 250})",
              sc.get("ok") and sc.get("count") == 400 and sc.get("undo_steps") == want_steps, json.dumps(sc)[:300])
        sc, raw = c.call("vw_edit", {"changes": [{"op": "move", "target": rid, "by": [100, 0]}], "preview": True})
        check(f"[{tag}] vw_edit preview=true returns an image",
              any(b.get("type") == "image" for b in raw.get("content") or []), json.dumps(sc)[:200])
        sc, raw = c.call("vw_organize", {"kind": "viewport", "action": "create", "sheet": "A-101", "layers": ["First"],
                                         "scale": "1:50"})
        check(f"[{tag}] viewport create returns a sheet preview", sc.get("ok") and
              any(b.get("type") == "image" for b in raw.get("content") or []), json.dumps(sc)[:300])
        c.close()
    finally:
        mock.terminate()


def _first_uuid(c: "Client") -> str:
    sc, _ = c.call("vw_read", {"what": "query", "limit": 1, "fields": ["uuid"]})
    return sc["data"][0]["uuid"]


def phase_real(python: str) -> None:
    print("\n== 5. REAL running bridge (read-only) ==")
    env = {k: v for k, v in os.environ.items() if not k.startswith("VW_MCP_")}
    c = Client(python, env, "real")
    c.initialize()
    sc, _ = c.call("vw_status")
    check("real: vw_status", sc.get("ok") and sc.get("instances"), json.dumps(sc)[:400])
    if not sc.get("instance"):
        print("  (no reachable Vectorworks; skipping the rest)")
        c.close()
        return
    for args in ({"what": "document"}, {"what": "documents"}, {"what": "layers"}, {"what": "classes"}, {"what": "summary"},
                 {"what": "query", "limit": 5}, {"what": "query", "type": "wall", "fields": ["uuid", "type", "name"]},
                 {"what": "view"}, {"what": "symbols", "limit": 5}, {"what": "worksheets"}, {"what": "resources"},
                 {"what": "parametric_schema", "target": "Door"}, {"what": "capabilities"}, {"what": "selection"},
                 {"what": "sheets"}):
        sc, raw = c.call("vw_read", args, timeout=60)
        ok = sc.get("ok") is True or err_code(sc) in {"unsupported", "upgrade_required"}
        check(f"real: vw_read {json.dumps(args)}", ok, json.dumps(sc)[:300])
    sc, raw = c.call("vw_script", {"code": "result = 1"})
    check("real rev5: vw_script -> upgrade_required (nothing sent)", err_code(sc) == "upgrade_required", json.dumps(sc)[:200])
    sc, raw = c.call("vw_organize", {"kind": "layer", "action": "create", "name": "x"})
    check("real rev5: layer create -> upgrade_required (nothing sent)", err_code(sc) == "upgrade_required", json.dumps(sc)[:200])
    c.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--real", action="store_true", help="also run READ-ONLY checks against Vectorworks on 9877")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--only", default="", help="comma list: startup,absent,mock,edge,weak,production,findings,legacy,real")
    args = parser.parse_args()
    only = set(filter(None, args.only.split(",")))
    state_dir = Path(tempfile.mkdtemp(prefix="vwmcp-state-"))
    try:
        if not only or "startup" in only:
            phase_startup(args.python, state_dir)
        if not only or "absent" in only:
            phase_absent(args.python, state_dir)
        if not only or "mock" in only:
            phase_mock(args.python, state_dir)
        if not only or "edge" in only:
            phase_edge(args.python, state_dir)
        if not only or "edge" in only or "weak" in only:
            phase_weak_model(args.python, state_dir)
        if not only or "production" in only:
            phase_production(args.python, state_dir)
        if not only or "findings" in only:
            phase_findings(args.python, state_dir, [])
            phase_findings(args.python, state_dir, NEW_BRIDGE_FEATURES)
        if not only or "legacy" in only:
            phase_legacy_mock(args.python, state_dir)
        if args.real or "real" in only:
            phase_real(args.python)
    finally:
        if not args.keep:
            shutil.rmtree(state_dir, ignore_errors=True)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for name, _, detail in failed:
        print(f"  FAILED: {name}: {detail[:300]}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
