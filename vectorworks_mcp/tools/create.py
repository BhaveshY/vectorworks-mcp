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
                plugin = str(plugin).strip()
                needed.add({"door": "Door", "window": "Window"}.get(plugin.lower(), plugin))
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
    join_walls = C.boolean(a["join_walls"], "join_walls") if a.get("join_walls") not in (None, "") else None
    host = get_host()
    all_wall_ids = builders.wall_ids_of(objects) | builders.wall_ids_of(fp_objects)

    def compile_all(descriptors: dict, schemas: dict, mm_per_unit: float = 1.0) -> builders.CreatePlan:
        plan = builders.compile_create(objects, scale, descriptors=descriptors, schemas=schemas,
                                       extra_wall_ids=all_wall_ids, doc_mm_per_unit=mm_per_unit) \
            if objects else builders.CreatePlan()
        if fp_objects:
            fplan = builders.compile_create(fp_objects, 1.0, descriptors=descriptors, schemas=schemas,
                                            extra_wall_ids=all_wall_ids, doc_mm_per_unit=mm_per_unit)
            clash = set(plan.ids) & set(fplan.ids)
            if clash:
                raise validation(f"ids {sorted(clash)} are used both in objects and floor_plan", "objects", "w1")
            plan.primary += fplan.primary
            plan.hosted += fplan.hosted
            plan.ids += fplan.ids
            plan.warnings += fplan.warnings
        return plan

    plan = compile_all({}, {})  # validate everything before touching Vectorworks
    target = None
    descriptors: dict = {}
    if not dry_run:
        target = host.session.target(a.get("document"), write=True, bind_apply=True)
        needed = _plugins_needed(objects + fp_objects)
        if needed:
            descriptors, schemas = _tx.load_schemas(host, target, needed)
            plan = compile_all(descriptors, schemas, _tx.doc_mm_per_unit(target))
    warnings.extend(dict.fromkeys(plan.warnings))
    if dry_run:
        out = {"ok": True, "dry_run": True, "units": units, "native_units": "mm",
               "operations": plan.primary + plan.hosted, "count": len(plan.primary) + len(plan.hosted)}
        if join_walls is not None:
            out["join_walls"] = join_walls
        if fp_counts:
            out["floor_plan"] = fp_counts
        return out

    _tx.fill_descriptors(plan.primary + plan.hosted, descriptors)
    key = host.session.idempotency_key(a.get("idempotency_key"), [plan.primary, plan.hosted], target)
    inst = target.inst
    created_ids: dict[str, str] = {}
    receipts: list = []
    wall_joins: list = []
    if plan.hosted and inst.v2:
        # v2: native resolves "$wall" refs inside one transaction (contract 2.3 vw_create).
        summary = _tx.run_general(host, target, plan.primary + plan.hosted, key, ctx, join_walls=join_walls)
        created_ids.update(_tx.ids_from(summary))
        receipts += summary["receipts"]
        wall_joins += summary.get("wall_joins") or []
    else:
        summary = _tx.run_general(host, target, plan.primary, key, ctx, join_walls=join_walls)
        created_ids.update(_tx.ids_from(summary))
        receipts += summary["receipts"]
        wall_joins += summary.get("wall_joins") or []
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
        if r.get("parent_uuid"):
            entry["wall"] = r["parent_uuid"]
        created.append({k: v for k, v in entry.items() if v})
    out = {"ok": True, "created": created, "ids": created_ids, "count": len(created), "idempotency_key": key,
           "undo": "one undo step" if not (plan.hosted and not inst.v2) else "two undo steps"}
    if wall_joins:
        out["wall_joins"] = wall_joins
    if fp_counts:
        out["floor_plan"] = fp_counts
    if target.document:
        out["document"] = doc_brief(target.document)
    out["next_step"] = ("Done: this receipt is authoritative (every listed object exists). Do not re-create or re-check "
                        "each object; use vw_capture() once if a visual check is needed.")
    return out
