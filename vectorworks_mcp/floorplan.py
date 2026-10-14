"""Floor-plan recipes (ported from the 0.7 schematic/BIM floor-plan tools).

``build(spec, scale)`` turns a floor_plan spec into vw_create objects (mm):
- style "bim" (default): real walls per room (+ labels, dimensions, spaces),
  extra walls, hosted doors/windows placed on the nearest wall;
- style "schematic": 2D only (rect walls, door leaf + swing arc, window lines),
  exactly like the 0.7 vw_create_schematic_* tools.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from . import coerce as C
from .builders import OPENING_FLAT_KEYS, OPENING_SIDE_KEYS
from .errors import validation


def _num(src: dict, keys: tuple[str, ...], field: str, scale: float, default: Optional[float] = None,
         positive: bool = False) -> float:
    for key in keys:
        if key in src and src[key] not in (None, ""):
            value = C.length(src[key], field, scale)
            if positive and value <= 0:
                raise validation(f"{field} must be > 0", field, 1000)
            return value
    if default is None:
        raise validation(f"{field} is required", field, 1000)
    return default


def _xy(src: dict, names: tuple[str, ...], xkeys: tuple[str, str], field: str, scale: float) -> list[float]:
    for name in names:
        if src.get(name) is not None:
            return C.point(src[name], field, scale)
    if xkeys[0] in src and xkeys[1] in src:
        return [C.length(src[xkeys[0]], field, scale), C.length(src[xkeys[1]], field, scale)]
    raise validation(f"{field} is required as [x, y]", field, [0, 0])


def build(spec: Any, scale: float) -> tuple[list[dict], list[str], dict]:
    spec = C.parse_json_maybe(spec)
    if not isinstance(spec, dict):
        raise validation("floor_plan must be an object with rooms/walls/doors/windows", "floor_plan",
                         {"rooms": [{"name": "Living", "origin": [0, 0], "width": 5000, "depth": 4000}]})
    s = C.lower_keys(spec)
    style = C.choice(s.get("style"), "floor_plan.style", ["bim", "schematic"], {"2d": "schematic", "3d": "bim", "native": "bim"},
                     default="bim")
    rooms = C.parse_json_maybe(s.get("rooms") or [])
    walls = C.parse_json_maybe(s.get("walls") or [])
    doors = C.parse_json_maybe(s.get("doors") or [])
    windows = C.parse_json_maybe(s.get("windows") or [])
    if not rooms and not walls:
        raise validation("floor_plan needs rooms or walls", "floor_plan.rooms",
                         [{"name": "Living", "origin": [0, 0], "width": 5000, "depth": 4000}])
    thickness = _num(s, ("wall_thickness", "thickness"), "floor_plan.wall_thickness", scale, 200.0, positive=True)
    height = _num(s, ("wall_height", "height"), "floor_plan.wall_height", scale, 3000.0, positive=True)
    prefix = str(s.get("name") or s.get("name_prefix") or "").strip()
    classes = C.lower_keys(C.parse_json_maybe(s.get("classes") or {}))
    objects: list[dict] = []
    warnings: list[str] = []
    counts = {"rooms": len(rooms), "walls": len(walls), "doors": len(doors), "windows": len(windows)}
    local_walls: list[tuple[str, list[float], list[float], float]] = []

    def pname(name: Any, fallback: str) -> str:
        name = str(name or "").strip() or fallback
        return f"{prefix} {name}".strip() if prefix else name

    for i, room in enumerate(rooms):
        f = f"floor_plan.rooms[{i}]"
        r = C.lower_keys(room)
        x, y = _xy(r, ("origin", "at", "corner", "position"), ("x", "y"), f + ".origin", scale)
        w = _num(r, ("width", "w"), f + ".width", scale, positive=True)
        d = _num(r, ("depth", "length", "d"), f + ".depth", scale, positive=True)
        t = _num(r, ("wall_thickness", "thickness"), f + ".wall_thickness", scale, thickness, positive=True)
        name = pname(r.get("name"), f"Room {i + 1}")
        rid = f"r{i + 1}"
        if style == "schematic":
            if t * 2 >= min(w, d):
                raise validation(f"{f}.wall_thickness must be less than half of width and depth", f + ".wall_thickness", 100)
            cls = r.get("class") or classes.get("wall") or "A-FP-Schematic-Wall"
            for side, (ax, ay, bx, by) in {"south": (x, y, x + w, y + t), "north": (x, y + d - t, x + w, y + d),
                                           "west": (x, y + t, x + t, y + d - t), "east": (x + w - t, y + t, x + w, y + d - t)}.items():
                objects.append({"type": "rect", "id": f"{rid}_{side}", "start": [ax, ay], "end": [bx, by],
                                "name": f"{name} {side} wall", "class": cls})
        else:
            h = _num(r, ("wall_height", "height"), f + ".wall_height", scale, height, positive=True)
            obj = {"type": "room", "id": rid, "origin": [x, y], "width": w, "depth": d, "wall_thickness": t,
                   "wall_height": h, "name": name, "label_room": C.boolean(s.get("label_rooms", True), "floor_plan.label_rooms"),
                   "dimensions": C.boolean(s.get("dimension_rooms", False), "floor_plan.dimension_rooms"),
                   "space": C.boolean(s.get("spaces", r.get("space", False)), "floor_plan.spaces")}
            cls = r.get("class") or classes.get("wall")
            if cls:
                obj["class"] = cls
            if s.get("wall_style") or r.get("wall_style"):
                obj["wall_style"] = r.get("wall_style") or s.get("wall_style")
            objects.append(obj)
            corners = [(x, y), (x + w, y), (x + w, y + d), (x, y + d)]
            for k, side in enumerate(("south", "east", "north", "west")):
                local_walls.append((f"{rid}_{side}", list(corners[k]), list(corners[(k + 1) % 4]), t))

    for i, wall in enumerate(walls):
        f = f"floor_plan.walls[{i}]"
        wsrc = C.lower_keys(wall)
        a = _xy(wsrc, ("start", "from"), ("x1", "y1"), f + ".start", scale)
        b = _xy(wsrc, ("end", "to"), ("x2", "y2"), f + ".end", scale)
        t = _num(wsrc, ("thickness",), f + ".thickness", scale, thickness)
        name = pname(wsrc.get("name"), f"Wall {i + 1}")
        wid = str(wsrc.get("id") or f"w{i + 1}")
        if style == "schematic":
            cls = wsrc.get("class") or classes.get("wall") or "A-FP-Schematic-Wall"
            if t <= 0 or (a[0] != b[0] and a[1] != b[1]):
                objects.append({"type": "line", "id": wid, "start": a, "end": b, "name": name, "class": cls})
                if t > 0:
                    warnings.append(f"{f}: angled schematic wall drawn as a centre line")
            elif a[1] == b[1]:
                objects.append({"type": "rect", "id": wid, "start": [min(a[0], b[0]), a[1] - t / 2],
                                "end": [max(a[0], b[0]), a[1] + t / 2], "name": name, "class": cls})
            else:
                objects.append({"type": "rect", "id": wid, "start": [a[0] - t / 2, min(a[1], b[1])],
                                "end": [a[0] + t / 2, max(a[1], b[1])], "name": name, "class": cls})
        else:
            obj = {"type": "wall", "id": wid, "start": a, "end": b, "thickness": t if t > 0 else thickness,
                   "height": _num(wsrc, ("height",), f + ".height", scale, height, positive=True), "name": name}
            cls = wsrc.get("class") or classes.get("wall")
            if cls:
                obj["class"] = cls
            objects.append(obj)
            local_walls.append((wid, a, b, obj["thickness"]))

    for i, door in enumerate(doors):
        f = f"floor_plan.doors[{i}]"
        dsrc = C.lower_keys(door)
        name = pname(dsrc.get("name"), f"Door {i + 1}")
        width = _num(dsrc, ("width",), f + ".width", scale, 900.0, positive=True)
        if style == "schematic":
            hinge = _xy(dsrc, ("hinge", "at", "position"), ("hinge_x", "hinge_y"), f + ".hinge", scale)
            rotation = C.number(dsrc.get("rotation", 0), f + ".rotation")
            swing = C.choice(dsrc.get("swing"), f + ".swing", ["left", "right"], default="left")
            sweep = 90 if swing == "left" else -90
            leaf = [hinge[0] + width * math.cos(math.radians(rotation + sweep)), hinge[1] + width * math.sin(math.radians(rotation + sweep))]
            cls = dsrc.get("class") or classes.get("door") or "A-FP-Schematic-Door"
            objects.append({"type": "line", "start": hinge, "end": leaf, "name": f"{name} leaf", "class": cls})
            objects.append({"type": "arc", "center": hinge, "radius": width, "start_angle": rotation, "sweep_angle": sweep,
                            "name": f"{name} swing", "class": cls})
        else:
            objects.append(_hosted("door", dsrc, f, scale, name, width, local_walls, classes.get("door")))

    for i, window in enumerate(windows):
        f = f"floor_plan.windows[{i}]"
        wsrc = C.lower_keys(window)
        name = pname(wsrc.get("name"), f"Window {i + 1}")
        if style == "schematic":
            a = _xy(wsrc, ("start", "from"), ("x1", "y1"), f + ".start", scale)
            b = _xy(wsrc, ("end", "to"), ("x2", "y2"), f + ".end", scale)
            depth = _num(wsrc, ("marker_depth", "depth"), f + ".marker_depth", scale, 150.0, positive=True)
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            if length <= 0:
                raise validation(f"{f} endpoints must differ", f + ".end", [1000, 0])
            ox, oy = -(b[1] - a[1]) / length * depth / 2, (b[0] - a[0]) / length * depth / 2
            cls = wsrc.get("class") or classes.get("window") or "A-FP-Schematic-Window"
            objects.append({"type": "line", "start": [a[0] + ox, a[1] + oy], "end": [b[0] + ox, b[1] + oy], "name": f"{name} line A", "class": cls})
            objects.append({"type": "line", "start": [a[0] - ox, a[1] - oy], "end": [b[0] - ox, b[1] - oy], "name": f"{name} line B", "class": cls})
        else:
            if "start" in wsrc and "end" in wsrc and "at" not in wsrc:
                a = C.point(wsrc["start"], f + ".start", scale)
                b = C.point(wsrc["end"], f + ".end", scale)
                wsrc = dict(wsrc, at=[(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], width=math.hypot(b[0] - a[0], b[1] - a[1]))
                scale_for_window = 1.0
            else:
                scale_for_window = scale
            width = _num(wsrc, ("width",), f + ".width", scale_for_window, 1200.0, positive=True)
            objects.append(_hosted("window", wsrc, f, scale_for_window, name, width, local_walls, classes.get("window")))
    return objects, warnings, counts


def _hosted(kind: str, src: dict, field: str, scale: float, name: str, width: float,
            local_walls: list[tuple[str, list[float], list[float], float]], cls: Any) -> dict:
    obj: dict[str, Any] = {"type": kind, "name": name, "width": width}
    for key in ("height", "sill", "sill_height", "rotation", "parameters"):
        if key in src:
            obj[key] = src[key] if key in {"rotation", "parameters"} else C.length(src[key], f"{field}.{key}", scale)
    for key, value in src.items():
        # swing side / hinge jamb / flip and plug-in options (swing In|Out, open_angle, ...): builders.opening_options
        sem = "".join(ch for ch in str(key).lower() if ch.isalnum())
        if (sem in OPENING_FLAT_KEYS or sem in OPENING_SIDE_KEYS or
                sem in {"flip", "flipped", "mirror", "mirrored", "rightside"}) and not isinstance(value, (list, dict)):
            obj[key] = value
    if cls or src.get("class"):
        obj["class"] = src.get("class") or cls
    if src.get("wall"):
        obj["wall"] = src["wall"]
        if "at" in src:
            obj["at"] = C.point(src["at"], field + ".at", scale)
        elif "offset" in src:
            obj["offset"] = C.length(src["offset"], field + ".offset", scale)
        return obj
    at = _xy(src, ("at", "position", "center"), ("x", "y"), field + ".at", scale)
    best: Optional[tuple[float, str, list[float]]] = None
    for wid, a, b, t in local_walls:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy
        if length2 <= 0:
            continue
        u = max(0.0, min(1.0, ((at[0] - a[0]) * dx + (at[1] - a[1]) * dy) / length2))
        px, py = a[0] + u * dx, a[1] + u * dy
        dist = math.hypot(at[0] - px, at[1] - py)
        if dist <= max(t, 50.0) and (best is None or dist < best[0]):
            best = (dist, wid, [px, py])
    if best is None:
        raise validation(f"{field}.at {at} is not on any wall of this floor plan; put it on a wall or pass wall=<uuid>",
                         field + ".at", [2000, 0])
    obj["wall"] = "$" + best[1]
    obj["at"] = best[2]
    return obj
