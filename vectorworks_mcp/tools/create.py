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


def _split_pio(items: list[dict]) -> tuple[list[dict], list[tuple[str, list, float]]]:
    """Plug-in objects (column, parametric fallback): their parameters are set while placing them (some, e.g.
    Column2 'OA Height', are only accepted before the object's first regeneration) AND again in a second script
    after it (the first regeneration re-initialises the others, e.g. shaft size, capital, base)."""
    deferred = []
    for item in items:
        if item["kind"] == "column":
            params = [{"id": k, "value": v, "mm": True} for k, v in item["lengths"].items()]
            params += [{"id": k, "value": v} for k, v in item["fields"].items()]
            deferred.append((item["id"], params, 1.0))
        elif item["kind"] == "pio" and item.get("parameters"):
            deferred.append((item["id"], list(item["parameters"]), item.get("scale_mm", 1.0)))
    return items, deferred


def _compensate(host, document, plan, created_ids: dict, exc: VWError, ctx, native_key: str) -> None:
    """The script part failed after the native part committed: delete the native objects again (all-or-nothing for
    the model). Doors/windows first (deleting a wall would cascade them). Only if that fails: commit_state partial."""
    hosted = {op["operation_id"] for op in plan.hosted}
    uuids = [created_ids[i] for i in created_ids if i in hosted] +             [created_ids[i] for i in created_ids if i not in hosted]
    try:
        target = host.session.target(document, write=True, bind_apply=True)
        ops = builders.normalise_edit_ops([{"type": "delete", "params": {"target": f"uuid:{u}"}} for u in uuids])
        key = host.session.idempotency_key(None, ops, target)
        _tx.run_general(host, target, ops, key, ctx)
    except VWError as comp:
        exc.message = (f"{exc.message} (the native objects of this call WERE created: {sorted(created_ids)}; the "
                       f"script part was rolled back and removing the native objects failed: {comp.message})")
        exc.commit_state = "partial"
        exc.next_step = ("The native objects listed in the message exist. Fix the named problem and create only the "
                         "extrudes/columns (or apply the styles with vw_edit) again; do not resend the whole call.")
        return
    exc.message = (f"{exc.message} (nothing from this call remains: the script part was rolled back and the "
                   f"{len(uuids)} native object(s) created first were deleted again)")
    exc.commit_state = "rolled_back"
    host.session.burn(native_key)
    exc.details = dict(exc.details or {}, compensated_uuids=uuids)
    exc.next_step = ("The document is unchanged (the compensating delete is one extra undo step). Fix the named "
                     "problem and send the whole call again (with a NEW idempotency_key if you passed one).")


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
            plan.script_objects += fplan.script_objects
            plan.styles += fplan.styles
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
        if (plan.script_objects or plan.styles) and not host.can_script(target.inst):
            raise host.bridge.missing_action(target.inst, "run_script")
    warnings.extend(dict.fromkeys(plan.warnings))
    if dry_run:
        out = {"ok": True, "dry_run": True, "units": units, "native_units": "mm",
               "operations": plan.primary + plan.hosted,
               "count": len(plan.primary) + len(plan.hosted) + len(plan.script_objects)}
        if plan.script_objects:
            out["script_objects"] = plan.script_objects
        if plan.styles:
            out["styles"] = [{"id": i, "style": st} for i, st in plan.styles]
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
    steps = 0
    if not (plan.primary or plan.hosted):
        pass  # script-only plan (extrudes / columns)
    elif plan.hosted and inst.v2:
        # v2: native resolves "$wall" refs inside one transaction (contract 2.3 vw_create).
        steps = 1
        summary = _tx.run_general(host, target, plan.primary + plan.hosted, key, ctx, join_walls=join_walls)
        created_ids.update(_tx.ids_from(summary))
        receipts += summary["receipts"]
        wall_joins += summary.get("wall_joins") or []
    else:
        steps = 1
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
            steps = 2
            target2 = host.session.target(a.get("document"), write=True, bind_apply=True)
            summary2 = _tx.run_general(host, target2, plan.hosted, key + "-h", ctx)
            created_ids.update(_tx.ids_from(summary2))
            receipts += summary2["receipts"]
    script_result: dict = {}
    restyle = [{"id": i, "uuid": created_ids[i], "style": st} for i, st in plan.styles if created_ids.get(i)]
    objects, deferred = _split_pio(plan.script_objects)
    if objects or restyle:
        if steps:
            target = host.session.target(a.get("document"), write=True)
        try:
            script_result = host.script(target, "massing", {"objects": objects, "restyle": restyle},
                                        label="MCP create", ctx=ctx) or {}
        except VWError as exc:
            if steps and exc.commit_state != "unknown":
                _compensate(host, a.get("document"), plan, created_ids, exc, ctx, key)
            raise
        warnings.extend(script_result.get("warnings") or [])
        steps += 1
        for row in script_result.get("created") or []:
            receipts.append({"operation_id": row["id"], "uuid": row["uuid"], "type": row["type"]})
            created_ids[row["id"]] = row["uuid"]
    if deferred:
        # Live (VW 2024, Column2): a new plug-in object initialises its record fields on its FIRST regeneration,
        # which runs only after the creating script returned, overwriting most values set in that script (but it
        # takes e.g. 'OA Height' from them). So the parameters are set again in a second script (+1 undo step).
        ops = [{"op": "pio", "targets": [f"uuid:{created_ids[i]}"], "parameters": params, "scale_mm": scale_mm}
               for i, params, scale_mm in deferred if created_ids.get(i)]
        target = host.session.target(a.get("document"), write=True)
        try:
            res = host.script(target, "edit_engine", {"ops": ops}, label="MCP create parameters", ctx=ctx) or {}
        except VWError as exc:
            if exc.commit_state != "unknown":
                _compensate(host, a.get("document"), plan, created_ids, exc, ctx, key)
            raise
        steps += 1
        checks = [{"uuid": r["uuid"], "expect": r["expect"]} for r in res.get("receipts") or [] if r.get("expect")]
        _tx.verify_pio(host, target, checks, warnings)
    created = []
    for r in receipts:
        entry = {"id": r.get("operation_id") or r.get("local_ref"), "uuid": r.get("uuid"), "type": r.get("type")}
        if r.get("parent_uuid"):
            entry["wall"] = r["parent_uuid"]
        created.append({k: v for k, v in entry.items() if v})
    out = {"ok": True, "created": created, "ids": created_ids, "count": len(created), "idempotency_key": key,
           "undo": {1: "one undo step", 2: "two undo steps"}.get(steps, f"{steps} undo steps")}
    if script_result.get("styled"):
        out["styled"] = script_result["styled"]
    if script_result.get("imported_resources"):
        out["imported_resources"] = script_result["imported_resources"]
    if steps > 1 and (plan.script_objects or restyle):
        warnings.append("Native objects and the script-made part (extrudes/columns/graphic styles) are separate "
                        "undo steps (native apply_operations and run_script cannot share one undo event).")
    if wall_joins:
        out["wall_joins"] = wall_joins
    if fp_counts:
        out["floor_plan"] = fp_counts
    if target.document:
        out["document"] = doc_brief(target.document)
    out["next_step"] = ("Done: this receipt is authoritative (every listed object exists). Do not re-create or re-check "
                        "each object; use vw_capture() once if a visual check is needed.")
    return out
