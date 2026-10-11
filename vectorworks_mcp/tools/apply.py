"""vw_apply: the 0.7 mixed create+edit transaction grammar.

Forgiving for weak models: door/window creates accept the vw_create aliases (wall, at, plugin, sill, x1..x2 span)
and get default plugin names / heights / sill; the whole plan is validated at once and every problem is reported
in ONE validation_error (error.details.issues) instead of one round trip per missing field."""

from __future__ import annotations

import math
import re

from .. import builders
from .. import coerce as C
from .. import ops
from ..errors import VWError
from ..host import doc_brief, get_host
from . import _tx

EXAMPLE = [{"type": "create", "operation_id": "r1",
            "params": {"object_type": "rect", "x1": 0, "y1": 0, "x2": 1000, "y2": 500}}]
OPENING_EXAMPLE = [{"type": "create", "operation_id": "d1",
                    "params": {"object_type": "door", "wall_uuid": "<wall uuid or $wall_op_id>", "x": 3000, "y": 0,
                               "width": 900}}]
NEXT_STEP = ("Nothing was sent to Vectorworks. Fix EVERY item in error.details.issues in one go, then call again. "
             "For doors/windows prefer vw_create(objects=[{\"type\":\"door\",\"wall\":\"<wall uuid or $id>\","
             "\"at\":[x,y],\"width\":900}]) - it fills plugin, height and sill for you.")
_LABEL_RE = re.compile(r"^operations\[1\]")


def handle(args: dict, ctx, warnings: list) -> dict:
    a = C.lower_keys(args)
    units = C.units_name(a.get("coordinate_units") or a.get("units"))
    scale = C.scale_for(units)
    operations = C.parse_json_maybe(a.get("operations") or a.get("ops") or a.get("plan"))
    if operations in (None, "", []):
        raise VWError("validation_error", "operations is required", field="operations", example=EXAMPLE)
    if isinstance(operations, dict):
        operations = [operations]
    if not isinstance(operations, list):
        raise VWError("validation_error", "operations must be a list of {type, operation_id?, params}",
                      field="operations", example=EXAMPLE)
    join_walls = C.boolean(a["join_walls"], "join_walls") if a.get("join_walls") not in (None, "") else None
    host = get_host()
    target = None
    operations = forgive_plan(operations, scale, warnings)
    if _needs_descriptors(operations):
        # Discover the live descriptor (and parameter schema) for door/window/parametric creates.
        target = host.session.target(a.get("document"), write=True, bind_apply=True)
        operations = _prefill(host, target, operations, scale, warnings)
    normalised = validate_plan(operations, units)
    if a.get("target_binding"):
        warnings.append("target_binding is no longer needed and was ignored; the host pins the document automatically.")
    if target is None:
        target = host.session.target(a.get("document"), write=True, bind_apply=True)
    key = host.session.idempotency_key(a.get("idempotency_key"), normalised, target)
    if ops.is_documentation_plan(normalised):
        summary = _tx.run_documentation(host, target, normalised, key, ctx)
    else:
        summary = _tx.run_general(host, target, normalised, key, ctx, join_walls=join_walls)
    out = {"ok": True, "committed": summary["committed"], "applied": summary["applied"],
           "receipts": summary["receipts"], "idempotency_key": key, "coordinate_units": units}
    ids = _tx.ids_from(summary)
    if ids:
        out["ids"] = ids
    if summary.get("wall_joins"):
        out["wall_joins"] = summary["wall_joins"]
    cascaded = [u for r in summary["receipts"] for u in r.get("cascaded_inserts") or []]
    if cascaded:
        out["cascaded_inserts"] = cascaded
        warnings.append(f"Deleting wall(s) also deleted {len(cascaded)} inserted door/window/symbol object(s).")
    if summary.get("replayed"):
        out["replayed"] = True
    if target.document:
        out["document"] = doc_brief(target.document)
    return out


# ---------------------------------------------------------------------------
# forgiveness
# ---------------------------------------------------------------------------

def _point(value):
    value = C.parse_json_maybe(value)
    if isinstance(value, dict):
        lowered = C.lower_keys(value)
        if "x" in lowered and "y" in lowered:
            return [lowered["x"], lowered["y"]]
    if isinstance(value, str) and "," in value:
        parts = [p.strip() for p in value.split(",")]
        if len(parts) == 2:
            return parts
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return list(value)
    return None


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def forgive_plan(operations: list, scale: float, warnings: list) -> list:
    """Apply vw_create-style aliases to create ops (door/window/parametric/wall). Returns new op dicts."""
    declared: dict[str, str] = {}
    out = []
    for op in operations:
        if not isinstance(op, dict):
            out.append(op)
            continue
        op = ops._forgive_operation(op)
        if str(op.get("type") or "").strip().lower() == "create" and isinstance(op.get("params"), dict):
            p = C.lower_keys(op["params"])
            kind = str(p.get("object_type") or p.get("type") or "").strip().lower()
            p.pop("type", None) if "object_type" in p else None
            kind = {"rectangle": "rect", "plugin": "parametric", "pio": "parametric"}.get(kind, kind)
            plugin = p.pop("plugin", None)
            if plugin is not None and not p.get("plugin_name"):
                p["plugin_name"] = plugin
            if kind == "parametric" and str(p.get("plugin_name") or "").strip().lower() in {"door", "window"} \
                    and any(k in p for k in ("wall", "wall_uuid", "host")):
                kind = str(p["plugin_name"]).strip().lower()
            if kind:
                p["object_type"] = kind
            if kind in {"door", "window"}:
                _forgive_opening(p, kind, scale, declared, warnings)
            if kind == "wall":
                for alias in ("join", "auto_join"):
                    if alias in p and "join_walls" not in p:
                        p["join_walls"] = p.pop(alias)
            op = dict(op, params=p)
            if op.get("operation_id"):
                declared[str(op["operation_id"])] = kind
        out.append(op)
    return out


def _forgive_opening(p: dict, kind: str, scale: float, declared: dict[str, str], warnings: list) -> None:
    defaults_mm = builders.DEFAULTS_MM
    p["plugin_name"] = kind.title() if not p.get("plugin_name") or str(p["plugin_name"]).lower() == kind else p["plugin_name"]
    wall = None
    for alias in ("wall_uuid", "wall", "host", "wall_id", "in_wall"):
        if p.get(alias) not in (None, ""):
            wall = p.pop(alias) if alias != "wall_uuid" else p["wall_uuid"]
            break
    for alias in ("wall", "host", "wall_id", "in_wall"):
        p.pop(alias, None)
    if isinstance(wall, str):
        wall = wall.strip()
        if wall.startswith("uuid:"):
            wall = wall[5:]
        elif not wall.startswith("$") and not builders.is_uuid(wall) and wall in declared:
            wall = "$" + wall
        p["wall_uuid"] = wall
    elif wall is not None:
        p["wall_uuid"] = wall
    # position: at / position / location / center, or the midpoint of an x1..x2 span
    if "x" not in p and "y" not in p:
        for alias in ("at", "position", "location", "point", "center", "insertion_point"):
            if alias in p:
                pt = _point(p.pop(alias))
                if pt is not None:
                    p["x"], p["y"] = pt
                break
    if "x" not in p and all(k in p for k in ("x1", "y1", "x2", "y2")):
        x1, y1, x2, y2 = (_num(p[k]) for k in ("x1", "y1", "x2", "y2"))
        if None not in (x1, y1, x2, y2):
            p["x"], p["y"] = (x1 + x2) / 2, (y1 + y2) / 2
            span = math.hypot(x2 - x1, y2 - y1)
            if "width" not in p and span > 0:
                p["width"] = span
            for k in ("x1", "y1", "x2", "y2"):
                p.pop(k)
            warnings.append(f"{kind} placed at the midpoint of x1,y1..x2,y2 (x/y is the opening's centre).")
    for alias, canonical in (("w", "width"), ("h", "height")):
        if alias in p and canonical not in p:
            p[canonical] = p.pop(alias)
    if kind == "window":
        for alias in ("sill", "sill_elevation", "elevation"):
            if alias in p and "sill_height" not in p:
                p["sill_height"] = p.pop(alias)
    defaults = {"width": defaults_mm[f"{kind}_width"], "height": defaults_mm[f"{kind}_height"]}
    if kind == "window":
        defaults["sill_height"] = defaults_mm["window_sill"]
    for key, mm in defaults.items():
        if p.get(key) in (None, ""):
            p[key] = mm / scale  # params are in coordinate_units
    params = p.get("parameters")
    if isinstance(params, (dict, str)):
        p["parameters"] = C.parse_json_maybe(params)


def _needs_descriptors(operations: list) -> bool:
    for op in operations:
        if isinstance(op, dict) and isinstance(op.get("params"), dict):
            p = op["params"]
            kind = str(p.get("object_type") or "").lower()
            if str(op.get("type") or "").lower() in {"create", "update_parametric"} and \
                    (kind in {"door", "window", "parametric"} or op.get("type") == "update_parametric") and \
                    (not p.get("descriptor_fingerprint") or isinstance(p.get("parameters"), dict)):
                return True
    return False


def _prefill(host, target, operations, scale: float, warnings: list):
    out = []
    for op in operations:
        if isinstance(op, dict) and isinstance(op.get("params"), dict):
            p = dict(op["params"])
            kind = str(p.get("object_type") or "").lower()
            plugin = p.get("plugin_name") or {"door": "Door", "window": "Window"}.get(kind)
            if plugin and isinstance(plugin, str) and (kind in {"door", "window", "parametric"} or op.get("type") == "update_parametric"):
                try:
                    schema = host.schema(target.inst, plugin)
                except VWError as exc:
                    if exc.code not in {"not_found", "native_error", "native_rejected"}:
                        raise
                    schema = {}
                    warnings.append(f"Could not read the {plugin!r} plug-in schema: {exc.message}")
                p["plugin_name"] = plugin
                if not p.get("descriptor_fingerprint") and schema.get("descriptor_fingerprint"):
                    p["descriptor_fingerprint"] = str(schema["descriptor_fingerprint"])
                if isinstance(p.get("parameters"), dict):
                    p["parameters"] = builders.parametric_parameters(p["parameters"], "parameters", scale, schema,
                                                                     warnings=warnings,
                                                                     doc_mm_per_unit=_tx.doc_mm_per_unit(target))
            op = dict(op, params=p)
        out.append(op)
    return out


# ---------------------------------------------------------------------------
# whole-plan validation
# ---------------------------------------------------------------------------

def validate_plan(operations: list, units: str) -> list:
    """Normalise every operation; collect ALL problems into one validation_error."""
    issues: list[dict] = []
    declared: dict[str, int] = {}
    for index, op in enumerate(operations, start=1):
        label = f"operations[{index}]"
        if isinstance(op, dict) and isinstance(op.get("params"), dict):
            p = op["params"]
            kind = str(p.get("object_type") or "").lower()
            if kind in {"door", "window"}:
                if p.get("wall_uuid") in (None, ""):
                    issues.append({"field": f"{label}.params.wall_uuid",
                                   "message": f"{label}: {kind} needs the host wall: wall_uuid (alias wall) = the wall's "
                                              "uuid or \"$<operation_id>\" of a wall created earlier in this plan"})
                elif isinstance(p["wall_uuid"], str) and p["wall_uuid"].startswith("$") and \
                        p["wall_uuid"][1:] not in declared:
                    issues.append({"field": f"{label}.params.wall_uuid",
                                   "message": f"{label}: wall_uuid {p['wall_uuid']!r} does not name an earlier create "
                                              f"operation_id (declared: {sorted(declared) or 'none'})"})
                if "x" not in p and "x1" not in p:
                    issues.append({"field": f"{label}.params.x",
                                   "message": f"{label}: {kind} needs its position on the wall: x, y (alias at=[x,y])"})
            for key in ("target", "ref"):
                ref = p.get(key)
                if isinstance(ref, str) and ref.startswith("$") and ref[1:] not in declared:
                    issues.append({"field": f"{label}.params.{key}",
                                   "message": f"{label}: {key} {ref!r} does not name an earlier create/duplicate operation_id"})
        try:
            ops.normalise_plan([op], units)
        except ValueError as exc:
            message = _LABEL_RE.sub(label, str(exc))
            field = message.split(" ", 1)[0] if message.startswith("operations[") else label
            if not any(i["field"] == field or i["field"].startswith(field + ".") or field.startswith(i["field"])
                       for i in issues):
                issues.append({"field": field, "message": message})
        if isinstance(op, dict):
            op_id = op.get("operation_id") or (op.get("id") if isinstance(op.get("id"), str) else None)
            if op_id:
                declared[str(op_id)] = index
    if not issues:
        try:
            return ops.normalise_plan(operations, units)
        except ValueError as exc:
            message = str(exc)
            issues.append({"field": message.split(" ", 1)[0] if message.startswith("operations[") else "operations",
                           "message": message})
    opening = any(isinstance(op, dict) and isinstance(op.get("params"), dict) and
                  str(op["params"].get("object_type") or "").lower() in {"door", "window"} for op in operations)
    count = len(issues)
    summary = issues[0]["message"] if count == 1 else f"{count} problems: " + "; ".join(i["message"] for i in issues[:6])
    raise VWError("validation_error", summary, field=issues[0]["field"], example=OPENING_EXAMPLE if opening else EXAMPLE,
                  details={"issues": issues}, next_step=NEXT_STEP if opening else
                  "Nothing was sent to Vectorworks. Fix EVERY item in error.details.issues in one go, then call again.")
