"""Build a large, realistic TEST project through the installed MCP server (for existing-project e2e tests).

5 storeys x 12 apartments: exterior/interior walls (classed), doors, windows, named Spaces
("A-<storey><nn> Living/Bedroom/Bath"), furniture rectangles, labels, a sheet per storey with a
viewport. Saved to %TEMP%\\vw-e2e\\big-project.vwx (never anywhere else). Needs the mcp_drive
session daemon:  py -3.12 scripts\\mcp_drive.py start; py -3.12 scripts\\make_big_project.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

OUT = os.path.join(os.environ["TEMP"], "vw-e2e", "big-project.vwx")
STOREYS, COLS, ROWS = 5, 6, 2          # 12 apartments per storey
APT_W, APT_D, CORRIDOR = 8000, 10000, 2000


def call(tool: str, args: dict) -> dict:
    with socket.create_connection(("127.0.0.1", int(os.environ.get("VW_DRIVE_PORT", "8765"))), timeout=900) as c:
        c.sendall((json.dumps({"op": "call", "tool": tool, "args": args}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = c.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    reply = json.loads(buf)["reply"]
    sc = (reply.get("result") or {}).get("structuredContent") or {}
    if not sc.get("ok"):
        raise SystemExit(f"{tool} failed: {json.dumps(sc)[:1500]}")
    return sc


def apartment(storey: int, n: int, ox: float, oy: float, flip: bool) -> list[dict]:
    """One 8 x 10 m apartment: living (5 m), bedroom + bath (3 m strip), door to the corridor side."""
    p = f"S{storey}A{n:02d}"
    tag = f"A-{storey}{n:02d}"
    x0, x1, y0, y1 = ox, ox + APT_W, oy, oy + APT_D
    corridor_y = y1 if not flip else y0       # side facing the corridor
    facade_y = y0 if not flip else y1
    xs = x0 + 5000                            # living | bed/bath split
    ym = (y0 + y1) / 2
    o = [
        {"type": "wall", "id": f"{p}f", "start": [x0, facade_y], "end": [x1, facade_y], "thickness": 300,
         "height": 2800, "class": "A-Wall-Ext"},
        {"type": "wall", "id": f"{p}c", "start": [x0, corridor_y], "end": [x1, corridor_y], "thickness": 200,
         "height": 2800, "class": "A-Wall-Int"},
        {"type": "wall", "id": f"{p}l", "start": [x0, y0], "end": [x0, y1], "thickness": 200, "height": 2800,
         "class": "A-Wall-Int"},
        {"type": "wall", "id": f"{p}s", "start": [xs, y0], "end": [xs, y1], "thickness": 100, "height": 2800,
         "class": "A-Wall-Int"},
        {"type": "wall", "id": f"{p}m", "start": [xs, ym], "end": [x1, ym], "thickness": 100, "height": 2800,
         "class": "A-Wall-Int"},
        {"type": "door", "wall": f"${p}c", "at": [x0 + 2500, corridor_y], "width": 1000, "name": f"{tag} Entrance"},
        {"type": "door", "wall": f"${p}s", "at": [xs, (ym + (y1 if not flip else y0)) / 2], "width": 900},
        {"type": "door", "wall": f"${p}s", "at": [xs, (ym + (y0 if not flip else y1)) / 2], "width": 800},
        {"type": "window", "wall": f"${p}f", "at": [x0 + 2500, facade_y], "width": 2400 if n % 3 else 1000,
         "name": f"{tag} Living window"},
        {"type": "window", "wall": f"${p}f", "at": [x0 + 6500, facade_y], "width": 1200},
        {"type": "space", "name": f"{tag} Living", "points": [[x0, y0], [xs, y0], [xs, y1], [x0, y1]]},
        {"type": "space", "name": f"{tag} Bedroom",
         "points": [[xs, facade_y], [x1, facade_y], [x1, ym], [xs, ym]]},
        {"type": "space", "name": f"{tag} Bath",
         "points": [[xs, ym], [x1, ym], [x1, corridor_y], [xs, corridor_y]]},
        {"type": "text", "at": [x0 + 600, ym], "text": tag, "text_size": 18, "class": "A-Anno"},
    ]
    # furniture: table, sofa, bed, wardrobe, 20 chairs/stools as small rects
    fy = facade_y + (800 if not flip else -800)
    o += [{"type": "rect", "origin": [x0 + 800, fy - (0 if not flip else 900)], "width": 1800, "height": 900,
           "class": "A-Furn", "name": f"{tag} Sofa"},
          {"type": "rect", "origin": [x0 + 1200, ym - 450], "width": 1600, "height": 900, "class": "A-Furn",
           "name": f"{tag} Table"},
          {"type": "rect", "origin": [xs + 600, (y0 + ym) / 2 - 1000 if not flip else (ym + y1) / 2 - 1000],
           "width": 1600, "height": 2000, "class": "A-Furn", "name": f"{tag} Bed"}]
    for k in range(100):  # small fixtures (tiles/stools) so a storey has ~1,400 objects
        o.append({"type": "rect", "origin": [x0 + 300 + (k % 20) * 220, ym + 300 + (k // 20) * 220 - 1300],
                  "width": 150, "height": 150, "class": "A-Furn"})
    return o


def main() -> int:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if os.path.exists(OUT) and "--force" not in sys.argv:
        print("exists:", OUT, "(pass --force to rebuild)")
        return 0
    t0 = time.time()
    doc = call("vw_document", {"action": "new"})["document"]["name"]
    P = {"document": doc}
    total = 0
    for s in range(STOREYS):
        layer = f"L{s:02d}"
        call("vw_organize", {**P, "kind": "layer", "action": "create", "name": layer, "elevation": s * 3000})
        objs: list[dict] = []
        n = 1
        for row in range(ROWS):
            for col in range(COLS):
                oy = 0 if row == 0 else APT_D + CORRIDOR
                objs += apartment(s, n, col * APT_W, oy, flip=row == 1)
                n += 1
        objs.append({"type": "text", "at": [0, -2500], "text": f"LEVEL {s:02d}  +{s * 3.0:.2f}", "text_size": 36,
                     "class": "A-Anno"})
        r = call("vw_create", {**P, "layer": layer, "create_layer": True, "objects": objs, "preview": False})
        total += r.get("count", 0)
        print(f"{layer}: {r.get('count')} objects, {time.time() - t0:.1f}s")
        call("vw_organize", {**P, "kind": "viewport", "action": "create", "sheet": f"A-1{s:02d}", "layers": [layer],
                             "scale": "1:200", "view": "top_plan", "title": f"LEVEL {s:02d} 1:200"})
    call("vw_document", {**P, "action": "save_as", "path": OUT})
    print(f"saved {OUT}: {total} created objects in {time.time() - t0:.1f}s")
    call("vw_document", {"document": os.path.basename(OUT), "action": "close"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
