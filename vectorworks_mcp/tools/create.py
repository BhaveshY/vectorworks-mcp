"""vw_create: typed, flat, unit-aware object creation in one undo step."""

from __future__ import annotations

from .. import builders
from .. import coerce as C
from .. import floorplan
from ..errors import VWError, validation
from ..host import doc_brief, get_host
from . import _tx


def _plugins_needed(objects: list[dict]) -> set[str]:
    needed: set[str] = set()
    for raw in objects:
        src = C.lower_keys(raw)
        kind = str(src.get("type") or src.get("object_type") or "").strip().lower()
        if src.get("descriptor_fingerprint"):
            continue
        if kind == "door":
            needed.add("Door")
        elif kind == "window":
            needed.add("Window")
        elif kind in {"parametric", "plugin", "pio"}:
            plugin = src.get("plugin") or src.get("plugin_name")
            if plugin:
                needed.add(str(plugin))
    return needed


def handle(args: dict, ctx, warnings: list) -> dict:
    a = C.lower_keys(args)
    units = C.units_name(a.get("units") or a.get("coordinate_units"))
    scale = C.scale_for(units)
    objects = C.obj_list(a["objects"], "objects", example=[builders._example("wall")]) if a.get("objects") not in (None, "", []) else []
    fp_counts = None
    if a.get("floor_plan") not in (None, "", {}):
        fp_objects, fp_warnings, fp_counts = floorplan.build(a["floor_plan"], scale)
        warnings.extend(fp_warnings)
        # floor-plan objects are already in mm; compile them separately with scale 1
    else:
        fp_objects = []
    if not objects and not fp_objects:
        raise validation("objects (or floor_plan) is required", "objects", [builders._example("wall")])
    dry_run = C.boolean(a.get("dry_run", False), "dry_run")
    host = get_host()

    def compile_all(descriptors: dict, schemas: dict) -> builders.CreatePlan:
        plan = builders.compile_create(objects, scale, descriptors=descriptors, schemas=schemas) if objects else builders.CreatePlan()
        if fp_objects:
            fplan = builders.compile_create(fp_objects, 1.0, descriptors=descriptors, schemas=schemas)
            clash = set(plan.ids) & set(fplan.ids)
            if clash:
                raise validation(f"ids {sorted(clash)} are used both in objects and floor_plan", "objects", "w1")
            plan.primary += fplan.primary
            plan.hosted += fplan.hosted
            plan.ids += fplan.ids
        return plan

    plan = compile_all({}, {})  # validate everything before touching Vectorworks
    target = None
    descriptors: dict = {}
    if not dry_run:
        target = host.session.target(a.get("document"), write=True, bind_apply=True)
        needed = _plugins_needed(objects + fp_objects)
        if needed:
            descriptors, schemas = _tx.load_schemas(host, target, needed)
            plan = compile_all(descriptors, schemas)
    if dry_run:
        out = {"ok": True, "dry_run": True, "units": units, "native_units": "mm",
               "operations": plan.primary + plan.hosted, "count": len(plan.primary) + len(plan.hosted)}
        if fp_counts:
            out["floor_plan"] = fp_counts
        return out

    _tx.fill_descriptors(plan.primary + plan.hosted, descriptors)
    key = host.session.idempotency_key(a.get("idempotency_key"), [plan.primary, plan.hosted], target)
    inst = target.inst
    created_ids: dict[str, str] = {}
    receipts: list = []
    if plan.hosted and inst.v2:
        # v2: native resolves "$wall" refs inside one transaction (contract 2.3 vw_create).
        summary = _tx.run_general(host, target, plan.primary + plan.hosted, key, ctx)
        created_ids.update(_tx.ids_from(summary))
        receipts += summary["receipts"]
    else:
        summary = _tx.run_general(host, target, plan.primary, key, ctx)
        created_ids.update(_tx.ids_from(summary))
        receipts += summary["receipts"]
        if plan.hosted:
            for op in plan.hosted:
                ref = op["params"]["wall_uuid"]
                uuid = created_ids.get(ref.lstrip("$"))
                if not uuid:
                    raise VWError("native_error", f"Wall {ref} was created but its uuid was not returned; doors/windows "
                                  "were not placed.", commit_state="committed", details={"created": created_ids})
                op["params"]["wall_uuid"] = uuid
            warnings.append("Pre-1.0 bridge: walls and their doors/windows were created in two undo steps.")
            target2 = host.session.target(a.get("document"), write=True, bind_apply=True)
            summary2 = _tx.run_general(host, target2, plan.hosted, key + "-h", ctx)
            created_ids.update(_tx.ids_from(summary2))
            receipts += summary2["receipts"]
    created = []
    for r in receipts:
        entry = {"id": r.get("operation_id") or r.get("local_ref"), "uuid": r.get("uuid"), "type": r.get("type")}
        created.append({k: v for k, v in entry.items() if v})
    out = {"ok": True, "created": created, "ids": created_ids, "count": len(created), "idempotency_key": key,
           "undo": "one undo step" if not (plan.hosted and not inst.v2) else "two undo steps"}
    if fp_counts:
        out["floor_plan"] = fp_counts
    if target.document:
        out["document"] = doc_brief(target.document)
    out["next_step"] = "Verify with vw_capture(view='objects', objects=[...uuids]) or vw_read(what='query')."
    return out
