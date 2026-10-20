"""E2E benchmark: a typical agent task flow through the installed MCP server (mcp_drive session daemon),
recording time and response size (text bytes, image bytes) per call. Works only in a new untitled document and
closes it without saving.  py -3.12 scripts\\mcp_drive.py start; py -3.12 scripts\\e2e_bench.py [label]
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

OUT = Path(os.environ.get("TEMP", ".")) / "vw-e2e"
rows: list[dict] = []


def call(tool: str, args: dict) -> dict:
    t = time.perf_counter()
    with socket.create_connection(("127.0.0.1", int(os.environ.get("VW_DRIVE_PORT", "8765"))), timeout=900) as c:
        c.sendall((json.dumps({"op": "call", "tool": tool, "args": args}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = c.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    ms = (time.perf_counter() - t) * 1000
    reply = json.loads(buf)["reply"]
    res = reply.get("result") or {}
    content = res.get("content") or []
    text = sum(len(b.get("text", "")) for b in content if b.get("type") == "text")
    img = sum(len(b.get("data", "")) for b in content if b.get("type") == "image")
    sc = res.get("structuredContent") or {}
    rows.append({"tool": tool, "what": args.get("what") or args.get("action") or args.get("view") or args.get("kind") or "",
                 "ms": round(ms), "text": text, "img": img, "ok": sc.get("ok")})
    return sc


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    OUT.mkdir(parents=True, exist_ok=True)
    call("vw_status", {})
    d = call("vw_document", {"action": "new"})["document"]["fingerprint"]
    P = {"document": d}
    room = call("vw_create", {**P, "objects": [
        {"type": "wall", "id": "s", "start": [0, 0], "end": [8000, 0]},
        {"type": "wall", "id": "e", "start": [8000, 0], "end": [8000, 6000]},
        {"type": "wall", "id": "n", "start": [8000, 6000], "end": [0, 6000]},
        {"type": "wall", "id": "w", "start": [0, 6000], "end": [0, 0]},
        {"type": "wall", "id": "i", "start": [5000, 0], "end": [5000, 6000], "thickness": 100},
        {"type": "door", "id": "d1", "wall": "$s", "at": [2500, 0], "width": 900},
        {"type": "door", "id": "d2", "wall": "$i", "at": [5000, 3000], "width": 800},
        {"type": "window", "id": "w1", "wall": "$n", "at": [2500, 6000], "width": 1500},
        {"type": "window", "id": "w2", "wall": "$n", "at": [6500, 6000], "width": 1200},
        {"type": "space", "name": "Living", "points": [[0, 0], [5000, 0], [5000, 6000], [0, 6000]]},
        {"type": "space", "name": "Bedroom", "points": [[5000, 0], [8000, 0], [8000, 6000], [5000, 6000]]}]})
    ids = room.get("ids") or {}
    call("vw_capture", {**P})
    call("vw_read", {**P, "what": "query", "type": "door"})
    call("vw_read", {**P, "what": "object", "target": ids.get("w1", "")})
    call("vw_edit", {**P, "changes": [{"op": "move", "target": ids.get("i", ""), "by": [300, 0]},
                                      {"op": "set", "target": ids.get("w1", ""), "width": 1800},
                                      {"op": "set", "target": "Bedroom", "name": "Office"}]})
    call("vw_organize", {**P, "kind": "layer", "action": "create", "name": "First", "elevation": 3000})
    call("vw_create", {**P, "layer": "First", "objects": [
        {"type": "rect", "origin": [0, 8000], "width": 3000, "height": 2000, "fill": "gradient"},
        {"type": "text", "at": [0, 11000], "text": "SITE PLAN", "text_size": 14}]})
    call("vw_read", {**P, "what": "resources", "type": "hatch", "search": "brick"})
    call("vw_capture", {**P, "view": "layer", "layer": "First"})
    call("vw_organize", {**P, "kind": "viewport", "action": "create", "sheet": "A-101", "layers": ["Design Layer-1"],
                         "scale": "1:50", "view": "top_plan", "title": "GROUND FLOOR"})
    pdf = OUT / f"bench-{label}-{int(time.time())}.pdf"
    call("vw_document", {**P, "action": "export", "path": str(pdf), "range": "sheet", "sheet": "A-101"})
    call("vw_read", {**P, "what": "summary"})
    call("vw_document", {**P, "action": "close", "discard_changes": "discard"})
    total_ms = sum(r["ms"] for r in rows)
    total_text = sum(r["text"] for r in rows)
    total_img = sum(r["img"] for r in rows)
    for r in rows:
        print(f"{r['tool']:12} {r['what']:10} {r['ms']:6} ms  text {r['text']:6} B  img {r['img']:7} B  ok={r['ok']}")
    print(f"TOTAL {len(rows)} calls  {total_ms / 1000:.1f} s  text {total_text} B  img {total_img} B  "
          f"failures {sum(1 for r in rows if r['ok'] is False)}")
    (OUT / f"bench-{label}.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
