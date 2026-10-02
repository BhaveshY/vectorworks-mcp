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
    def __init__(self, python: str, env: dict, label: str) -> None:
        self.label = label
        self.started = time.perf_counter()
        self.proc = subprocess.Popen([python, "-X", "utf8", "-u", "-m", "vectorworks_mcp"], cwd=str(ROOT), env=env,
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
    ro = {"vw_status", "vw_read", "vw_capture"}
    for t in tools:
        ann = t.get("annotations") or {}
        ok = ann.get("openWorldHint") is False and (ann.get("readOnlyHint") is True) == (t["name"] in ro)
        ok = ok and t["inputSchema"].get("additionalProperties") is False and "outputSchema" in t
        ok = ok and len(t["description"]) <= 600 and "Example" in t["description"]
        check(f"tool hygiene {t['name']}", ok, json.dumps(ann))
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
        ("vw_edit", {"changes": [{"op": "set", "target": "x", "properties": {"colour_of_moon": 1}}]}, "changes[0].properties"),
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
    check("vw://guide served (<= 2k tokens)", len(text) / 4 < 2200, f"{len(text)} chars")
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
        check("discovery via instances/*.json; ready", sc.get("ready") is True and sc.get("instance"), json.dumps(sc)[:300])
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
        check("vw_create walls + hosted door/window ($w1) + unit strings, one transaction",
              sc.get("ok") and len(ids) == 8 and sc.get("undo") == "one undo step", json.dumps(sc)[:400])
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
        sc, _ = c.call("vw_read", {"what": "object", "target": box})
        check("object detail (native + script detail)", sc.get("ok") and sc["data"].get("uuid") == box and "records" in sc["data"],
              json.dumps(sc)[:300])
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
        check("resources via script (hatches)", sc.get("ok") and sc.get("via") == "script", json.dumps(sc)[:200])
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
        check("rev5: walls + hosted door in two steps (warned)", sc.get("ok") and sc.get("undo") == "two undo steps",
              json.dumps(sc)[:300])
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
        check("vw_script lint: palette index into COLOR -> script_lint_failed with signature",
              raw.get("isError") and err.get("code") == "script_lint_failed" and err.get("commit_state") == "not_started"
              and "SetClFillFore(clasName: STRING, color: COLOR)" in err.get("next_step", ""), json.dumps(sc)[:400])
        sc, _ = c.call("vw_script", {"code": "h = vs.HMirror(None, False, (0, 0), (1, 0))"})
        check("vw_script lint: unknown vs function", err_code(sc) == "script_lint_failed"
              and "vs.MirrorN" in json.dumps(sc), json.dumps(sc)[:300])
        sc, _ = c.call("vw_script", {"code": "x1, y1, x2, y2 = vs.GetBBox(vs.FSActLayer())"})
        check("vw_script lint: GetBBox unpack count", err_code(sc) == "script_lint_failed", json.dumps(sc)[:300])
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
              bool(probs) and "save their work and restart Vectorworks" in probs[0].get("next_step", "")
              and any(r.get("script_engine") == "poisoned" for r in sc.get("instances", [])), json.dumps(sc)[:500])
        c.close()
    finally:
        mock.kill()
        shutil.rmtree(out_dir, ignore_errors=True)


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
    parser.add_argument("--only", default="", help="comma list: startup,absent,mock,legacy,real")
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
