"""Doors and windows for vw_create (objects and floor_plan): inserted by script, sized natively.

Live (VW 2024, bridge 714c15d): a door/window created by the native apply_operations create sits in the wall
container WITHOUT a wall break record, and Vectorworks deletes it as soon as the wall is regenerated (wall class
change, move, reset) - silent data loss. ``vs.AddSymToWall`` (+ ``vs.LNewObj``) makes a real wall break that
survives wall changes. Setting the size through the parametric record (vs.SetRField) did not change the object, so
the sizes and the other parameters are set afterwards by the native update_parametric path (the one vw_edit uses).

Flow in vw_create: native creates (walls, ...) -> one script (massing snippet ``openings``) -> one native
transaction with update_parametric (+ name/class) per opening. Each part is its own undo step; a failure in a later
part deletes everything this call created (tools/create.py ``_compensate``)."""

from __future__ import annotations

from typing import Any, Optional

from . import ops
from .builders import CreatePlan
from .errors import VWError

KINDS = {"door", "window"}

NATIVE_RISK_WARNING = ("Doors/windows created through the native apply_operations path ({what}) have no wall break "
                       "record: Vectorworks may DELETE them when the wall is regenerated (wall class change, move, "
                       "reset). Use vw_create(objects=[{{\"type\":\"door\",\"wall\":\"<wall uuid or $id>\","
                       "\"at\":[x,y],\"width\":900}}]) for openings that must stay.")


def _is_opening(op: dict) -> bool:
    return op.get("type") == "create" and op["params"].get("object_type") in KINDS


def extract(plan: CreatePlan) -> list[dict]:
    """Take the door/window creates out of the native plan -> opening items (sizes in mm)."""
    items: list[dict] = []
    for name in ("primary", "hosted"):
        keep = []
        for op in getattr(plan, name):
            if _is_opening(op):
                items.append(_item(op, plan))
            else:
                keep.append(op)
        setattr(plan, name, keep)
    if not items:
        return items
    ids = {item["id"] for item in items}
    # the window sill fix-up is part of the opening's own parameter step now
    plan.fixups = [f for f in plan.fixups if str(f["params"].get("target", "")).lstrip("$") not in ids]
    styles = []
    by_id = {item["id"]: item for item in items}
    for local_id, style in plan.styles:
        if local_id in by_id:
            by_id[local_id]["style"] = style
        else:
            styles.append((local_id, style))
    plan.styles = styles
    return items


def _item(op: dict, plan: CreatePlan) -> dict:
    p = op["params"]
    params = []
    for n in range(1, int(p.get("parameter_count") or 0) + 1):
        ptype = p[f"parameter_{n}_type"]
        params.append({"id": p[f"parameter_{n}_name"], "type": ptype, "value": p[f"parameter_{n}_{ptype}"]})
    flags = plan.opening_flags.get(op["operation_id"]) or {}
    item = {"id": op["operation_id"], "kind": p["object_type"], "plugin": p["plugin_name"],
            "fingerprint": p["descriptor_fingerprint"], "wall": str(p["wall_uuid"]), "at": [p["x1"], p["y1"]],
            "width": float(p["width"]), "height": float(p["height"]), "parameters": params,
            "flip": bool(flags.get("flip")), "right": bool(flags.get("right"))}
    if p.get("sill_height") is not None:
        item["sill"] = float(p["sill_height"])
    if p.get("name"):
        item["name"] = p["name"]
    if p.get("class_name"):
        item["class"] = p["class_name"]
    return item


def script_args(items: list[dict], created_ids: dict[str, str]) -> list[dict]:
    """Opening items for the massing snippet; '$wall' refs -> the uuids of the walls created before."""
    out = []
    for item in items:
        wall = item["wall"]
        if wall.startswith("$"):
            uuid = created_ids.get(wall[1:])
            if not uuid:
                raise VWError("native_error", f"Wall {wall} of {item['kind']} {item['id']!r} was created but its uuid "
                              "was not returned; the doors/windows were not inserted.", commit_state="committed",
                              details={"created": dict(list(created_ids.items())[:50])})
            wall = uuid
        row: dict[str, Any] = {"id": item["id"], "kind": item["kind"], "plugin": item["plugin"], "wall": wall,
                               "at": item["at"], "width": item["width"], "flip": item["flip"], "right": item["right"]}
        if item.get("style"):
            row["style"] = item["style"]
        out.append(row)
    return out


# Live (VW 2024): AddSymToWall creates the plug-in with the document's record-format defaults, not the plug-in
# schema defaults the native create used:
# - Window: "Use Part Line Styles" on and every part line style 0 (= no line): the window is INVISIBLE in Top/Plan.
#   LW_LS_OverRide cannot be switched off (the plug-in writes True back), so the part line styles are set to solid.
# - Door: SideLights on: a 900 door got 300 mm side lights on both sides (a 1.66 m hole in the wall).
# Set unless the caller passed the parameter; only ids the plug-in's schema has are sent.
CREATE_DEFAULTS = {"Window": [(pid, "integer", 2) for pid in ("SashLS", "GlassLS", "TrimLS", "SillLS", "JambLS")],
                   "Door": [("SideLights", "boolean", False)]}


def _create_defaults(plugin: str, schema: Optional[dict], user: set) -> list[dict]:
    known = {e.get("id") for e in (schema or {}).get("parameters") or [] if isinstance(e, dict)}
    return [{"id": pid, "type": ptype, "value": value} for pid, ptype, value in CREATE_DEFAULTS.get(plugin, ())
            if pid in known and pid not in user]


def param_ops(items: list[dict], created_ids: dict[str, str], descriptors: dict[str, str],
              doc_mm_per_unit: float, schemas: Optional[dict] = None) -> list[dict]:
    """Native update_parametric (width, height, sill + the caller's parameters) and set_properties (name, class)
    for the inserted openings, normalised for apply_operations."""
    unit = doc_mm_per_unit or 1.0
    schemas = schemas or {}
    raw: list[dict] = []
    edits: list[dict] = []
    for item in items:
        uuid = created_ids.get(item["id"])
        if not uuid:
            continue
        ref = "uuid:" + uuid
        params = [{"id": "Width", "type": "real", "value": item["width"] / unit},
                  {"id": "Height", "type": "real", "value": item["height"] / unit}]
        if item["kind"] == "window" and item.get("sill") is not None:
            # Elevation is the sill only with "Elevation Set At" = "Sill of Window" (live VW 2024)
            params += [{"id": "ElevationSetAt", "type": "string", "value": "Sill of Window"},
                       {"id": "Elevation", "type": "real", "value": item["sill"] / unit}]
        user = {p["id"] for p in item["parameters"]}
        params = [p for p in params if p["id"] not in user] + list(item["parameters"])
        params += _create_defaults(item["plugin"], schemas.get(item["plugin"]), user)
        fingerprint = descriptors.get(item["plugin"]) or item.get("fingerprint") or "pending"
        raw.append({"type": "update_parametric", "params": {"target": ref, "plugin_name": item["plugin"],
                                                            "descriptor_fingerprint": fingerprint,
                                                            "parameters": params}})
        props = {k: item[k] for k in ("name", "class") if item.get(k)}
        if props:
            edits.append({"ref": ref, "properties": props})
    for start in range(0, len(edits), 50):  # <= 100 native set_property ops each (chunkable)
        raw.append({"type": "set_properties", "params": {"edits": edits[start:start + 50]}})
    if not raw:
        return []
    try:
        return ops._normalise_execute_operations(raw, coordinate_units="mm", max_operations=max(len(raw), 1) * 4)
    except ValueError as exc:
        raise VWError("validation_error", str(exc)) from None
