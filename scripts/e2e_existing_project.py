"""Existing-project e2e: open the big TEST project (scripts/make_big_project.py), pull information,
identify things visually and change them - timing every call. Works only on %TEMP%\\vw-e2e\\big-project.vwx
and closes it WITHOUT saving. Needs the mcp_drive daemon (py -3.12 scripts\\mcp_drive.py start).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

PROJECT = os.path.join(os.environ["TEMP"], "vw-e2e", "big-project.vwx")
results: list[tuple[str, bool, float]] = []


def call(tool: str, args: dict) -> tuple[dict, dict, float]:
    t = time.perf_counter()
    with socket.create_connection(("127.0.0.1", int(os.environ.get("VW_DRIVE_PORT", "8765"))), timeout=900) as c:
        c.sendall((json.dumps({"op": "call", "tool": tool, "args": args}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = c.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    r = json.loads(buf)
    res = r["reply"].get("result") or {}
    return res.get("structuredContent") or {}, r, time.perf_counter() - t


def check(name: str, ok: bool, dt: float, info="") -> None:
    results.append((name, bool(ok), dt))
    text = info if isinstance(info, str) else json.dumps(info, ensure_ascii=False)
    print(f"{'PASS' if ok else 'FAIL'} {name} [{dt * 1000:.0f} ms]  {text[:400]}")


def main() -> int:
    if not os.path.exists(PROJECT):
        print("missing", PROJECT, "- run scripts/make_big_project.py first")
        return 2
    o, _, dt = call("vw_document", {"action": "open", "path": PROJECT})
    check("open existing project", o.get("ok"), dt, o.get("document"))
    D = (o.get("document") or {}).get("name") or os.path.basename(PROJECT)
    P = {"document": D}

    s, raw, dt = call("vw_read", {**P, "what": "summary"})
    data = s.get("data") or {}
    check("summary (orientation)", s.get("ok") and data.get("objects_scanned", 0) > 5000, dt,
          {"objects": data.get("objects_scanned"), "types": data.get("counts_by_type"), "bytes": len(json.dumps(s))})

    q, _, dt = call("vw_read", {**P, "what": "query", "type": "door", "layer": "L02", "fields": ["uuid", "name", "width"]})
    doors = q.get("data") or []
    check("doors on L02", len(doors) == 36, dt, f"{len(doors)} doors")

    q, _, dt = call("vw_read", {**P, "what": "query", "type": "window", "fields": ["uuid", "name", "width", "layer"],
                                "limit": 500})
    narrow = [w for w in q.get("data") or [] if (w.get("width") or 0) < 1200]
    check("narrow windows (<1200) across building", len(narrow) == 20, dt, f"{len(narrow)} narrow of {len(q.get('data') or [])}")

    q, _, dt = call("vw_read", {**P, "what": "query", "search": "A-305 Bedroom"})
    hit = [x for x in q.get("data") or [] if x.get("type") == "space"]
    check("find room by label", len(hit) == 1, dt, [h.get("room_name") or h.get("name") for h in hit])
    room = hit[0]["uuid"] if hit else None

    q, _, dt = call("vw_read", {**P, "what": "query", "class": "A-Furn", "layer": "L04", "limit": 1})
    check("class + layer filter", q.get("ok") and len(q.get("data") or []) == 1, dt, q.get("page"))

    if room:
        ob, _, dt = call("vw_read", {**P, "what": "object", "target": room})
        b = ((ob.get("data") or {}).get("bounds")) or {}
        check("room geometry", ob.get("ok") and b, dt, b)
        try:
            (x1, y1), (x2, y2) = (b["top_left"], b["bottom_right"]) if isinstance(b, dict) else b[:2]
            q, _, dt = call("vw_read", {**P, "what": "query", "type": "door", "layer": "L03",
                                        "within": [[min(x1, x2) - 200, min(y1, y2) - 200], [max(x1, x2) + 200, max(y1, y2) + 200]]})
            check("doors around that room (within)", len(q.get("data") or []) >= 1, dt, len(q.get("data") or []))
        except (KeyError, TypeError, ValueError):
            check("doors around that room (within)", False, 0, "no bounds")

    c, raw, dt = call("vw_capture", {**P, "view": "layer", "layer": "L03"})
    imgs = [b for b in (raw["reply"].get("result") or {}).get("content", []) if b.get("type") == "image"]
    m = c.get("image_to_world")
    check("capture storey L03 (plan, isolated)", c.get("ok") and imgs and c.get("restored") is True, dt,
          {"image": c.get("image_size"), "map": m, "isolated": c.get("isolated_layers")})
    if m and c.get("image_size"):
        w, h = c["image_size"]
        # top-right quarter of the image = apartments on the far east of the north row
        q, _, dt = call("vw_read", {**P, "what": "query", "type": "space", "layer": "L03",
                                    "within_px": [[int(w * 0.75), 0], [w, int(h * 0.5)]], "fields": ["uuid", "room_name", "name"]})
        names = sorted({(x.get("room_name") or x.get("name") or "")[:6] for x in q.get("data") or []})
        check("select what you see (within_px top-right)", bool(names), dt, names)

    e, _, dt = call("vw_edit", {**P, "changes": [{"op": "set", "target": f"uuid:{d['uuid']}", "width": 1100}
                                                 for d in doors if (d.get("name") or "").endswith("Entrance")]})
    check("bulk edit: L02 entrance doors -> 1100", e.get("ok"), dt, {k: e.get(k) for k in ("applied", "undo_steps")})
    q, _, dt = call("vw_read", {**P, "what": "query", "type": "door", "layer": "L02", "search": "Entrance",
                                "fields": ["width"]})
    widths = sorted({x.get("width") for x in q.get("data") or []})
    check("readback entrance widths", widths == [1100], dt, widths)

    if room:
        e, _, dt = call("vw_edit", {**P, "changes": [{"op": "set", "target": room, "name": "A-305 Study"}]})
        q, _, dt2 = call("vw_read", {**P, "what": "query", "search": "A-305 Study"})
        check("rename room", e.get("ok") and len(q.get("data") or []) == 1, dt + dt2)

    e, _, dt = call("vw_organize", {**P, "kind": "class", "action": "set", "name": "A-Furn", "visibility": "hidden"})
    c2, _, dt2 = call("vw_capture", {**P, "view": "layer", "layer": "L02"})
    check("hide furniture class + capture", e.get("ok") and c2.get("ok"), dt + dt2,
          {"image": c2.get("image_size")})
    call("vw_organize", {**P, "kind": "class", "action": "set", "name": "A-Furn", "visibility": "visible"})

    q, _, dt = call("vw_read", {**P, "what": "query", "type": "window", "layer": "L01", "fields": ["uuid"]})
    check("windows survive edits (L01 = 24)", len(q.get("data") or []) == 24, dt, len(q.get("data") or []))

    r, _, dt = call("vw_document", {**P, "action": "close", "discard_changes": "discard"})
    check("close without saving", r.get("ok"), dt)

    ok = sum(1 for _, good, _ in results if good)
    total_ms = sum(dt for _, _, dt in results) * 1000
    print(f"\n{ok}/{len(results)} passed, {total_ms / 1000:.1f}s total tool time")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
