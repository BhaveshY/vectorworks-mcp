"""vw_create: typed, flat, unit-aware object creation in one undo step (big plans: consecutive transactions)."""

from __future__ import annotations

from collections import Counter

from .. import builders, openings, ops
from .. import names as namehelp
from .. import coerce as C
from .. import floorplan
from ..errors import VWError, validation
from ..host import doc_brief, get_host
from . import _tx, preview

COMPACT_OVER = 20      # receipts for more objects: count + ids map + type counts (no per-object list)
# idempotency key -> script receipt rows of the doors/windows that call inserted. A resend with the same key replays
# the native part; the openings must not be inserted a second time (the native replay used to cover them).
_OPENINGS_DONE: dict[str, list[dict]] = {}
_KIND_TYPES = {"room": "wall"}


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


def _compensate(host, document, plan, created_ids: dict, exc: VWError, ctx, native_key: str, max_ops: int) -> None:
    """A later part failed after the native part committed: delete the native objects again (all-or-nothing for
    the model). Doors/windows first (deleting a wall would cascade them)."""
    hosted = {op["operation_id"] for op in plan.hosted} | set(plan.opening_flags)
    uuids = [created_ids[i] for i in created_ids if i in hosted] + \
        [created_ids[i] for i in created_ids if i not in hosted]
    retarget = lambda: host.session.target(document, write=True, bind_apply=True)  # noqa: E731
    if _tx.delete_uuids(host, retarget, uuids, native_key, ctx, exc, max_ops=max_ops, what="the native objects"):
        host.session.burn(native_key)


def _design_layers(host, target) -> tuple[list[dict], str]:
    """[{name, kind}], active layer name."""
    if target.inst.supports("layers"):
        data = host.call(target, "layers", {"action": "list"})
        items = data.get("layers") if isinstance(data, dict) else data
        active = data.get("active_layer") if isinstance(data, dict) else None
    else:
        items = host.call(target, "get_layers", {})
        active = None
    layers = [i for i in items or [] if isinstance(i, dict) and i.get("name")]
    if not active:
        active = next((l["name"] for l in layers if l.get("active")), None)
    if not active:
        info = host.call(target, "get_document_info", {})
        active = (info or {}).get("active_layer_name") if isinstance(info, dict) else None
    return layers, str(active or "")


def _resolve_layers(host, target, wanted: set[str], create_missing: bool, warnings: list) -> str:
    """Check every requested layer exists as a design layer (create it when create_layer:true); returns the active
    layer name."""
    layers, active = _design_layers(host, target)
    by_name = {l["name"]: l for l in layers}
    missing = sorted(n for n in wanted if n not in by_name)
    sheets = sorted(n for n in wanted if n in by_name and by_name[n].get("kind", "design") == "sheet")
    if sheets:
        raise validation(f"layer {sheets[0]!r} is a sheet layer; objects are created on design layers (sheets take "
                         "viewports: vw_organize(kind='viewport'))", "layer", "Design Layer-1")
    if missing and not create_missing:
        names = [l["name"] for l in layers if l.get("kind", "design") == "design"]
        close = builders.did_you_mean(missing[0], names)
        raise VWError("not_found", f"No design layer named {missing[0]!r}" +
                      (f"; did you mean {', '.join(repr(n) for n in close)}?" if close else "") +
                      f" (design layers: {', '.join(names[:12])}{' ...' if len(names) > 12 else ''}).",
                      field="layer", details={"design_layers": names[:50]},
                      next_step="Use an existing layer name, or pass create_layer=true to create the missing layer "
                                "first. Nothing was created.")
    for name in missing:
        if not target.inst.supports("layers"):
            raise host.bridge.missing_action(target.inst, "layers")
        host.call(target, "layers", {"action": "create", "name": name, "kind": "design"}, kind="write")
        warnings.append(f"Created design layer {name!r} (create_layer=true; its own undo step).")
    return active


def handle(args: dict, ctx, warnings: list):
    a = C.lower_keys(args)
    units = C.units_name(a.get("units") or a.get("coordinate_units"))
    scale = C.scale_for(units)
    objects = C.obj_list(a["objects"], "objects", example=[builders._example("wall")], maximum=5000) if a.get("objects") not in (None, "", []) else []
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
    call_layer = C.text(a["layer"], "layer", max_len=255).strip() if a.get("layer") not in (None, "") else None
    create_layer = C.boolean(a.get("create_layer", False), "create_layer")
    want_preview = C.boolean(a["preview"], "preview") if a.get("preview") not in (None, "") else None
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
            for name in ("primary", "hosted", "ids", "warnings", "script_objects", "styles", "fixups"):
                getattr(plan, name).extend(getattr(fplan, name))
            plan.layers.update(fplan.layers)
            plan.kinds.update(fplan.kinds)
            plan.index_of.update({i: len(objects) + v for i, v in fplan.index_of.items()})
        if call_layer:
            for local_id in plan.ids:
                plan.layers.setdefault(local_id, call_layer)
            for item in plan.script_objects:
                item.setdefault("layer", call_layer)
        return plan

    plan = compile_all({}, {})  # validate everything before touching Vectorworks
    target = None
    opening_items: list[dict] = []
    descriptors: dict = {}
    schemas: dict = {}
    if not dry_run:
        target = host.session.target(a.get("document"), write=True, bind_apply=True)
        needed = _plugins_needed(objects + fp_objects)
        if needed:
            descriptors, schemas = _tx.load_schemas(host, target, needed)
            plan = compile_all(descriptors, schemas, _tx.doc_mm_per_unit(target))
        if (plan.script_objects or plan.styles) and not host.can_script(target.inst):
            raise host.bridge.missing_action(target.inst, "run_script")
        if host.can_script(target.inst):
            # doors/windows: inserted by script (real wall break), sized natively afterwards (openings.py)
            opening_items = openings.extract(plan)
        elif any(op["params"].get("object_type") in openings.KINDS for op in plan.primary + plan.hosted):
            warnings.append(openings.NATIVE_RISK_WARNING.format(what="this bridge has no run_script"))
    warnings.extend(dict.fromkeys(plan.warnings))
    hosted_ids = {op["operation_id"] for op in plan.hosted} | {item["id"] for item in opening_items}
    wanted_layers = {l for i, l in plan.layers.items() if i not in hosted_ids}
    if dry_run:
        out = {"ok": True, "dry_run": True, "units": units, "native_units": "mm",
               "operations": plan.primary + plan.hosted,
               "count": len(plan.primary) + len(plan.hosted) + len(plan.script_objects)}
        if plan.script_objects:
            out["script_objects"] = plan.script_objects
        if plan.styles:
            out["styles"] = [{"id": i, "style": st} for i, st in plan.styles]
        if plan.layers:
            out["layers"] = dict(Counter(plan.layers.values()))
        if join_walls is not None:
            out["join_walls"] = join_walls
        if fp_counts:
            out["floor_plan"] = fp_counts
        if plan.opening_flags:
            out["doors_windows"] = ("inserted into their walls by script (vs.AddSymToWall: real wall break), then "
                                    "sized natively (update_parametric); the native create ops above are only used "
                                    "by bridges without run_script")
        return out

    inst = target.inst
    max_ops = _tx.max_operations(host, inst)
    active_layer = _resolve_layers(host, target, wanted_layers, create_layer, warnings) if wanted_layers else ""
    native_layer = bool(wanted_layers) and _tx.has_feature(host, inst, _tx.FEATURE_CREATE_LAYER)
    if native_layer:
        for op in plan.primary + plan.hosted:
            layer = plan.layers.get(op["operation_id"])
            if layer and op["operation_id"] not in hosted_ids:
                op["params"]["layer"] = layer
    # objects that still have to be moved to their layer by script (fallback for bridges without per-op layer)
    moves: dict[str, list[str]] = {}
    if wanted_layers and not native_layer:
        for local_id, layer in plan.layers.items():
            if local_id not in hosted_ids and layer != active_layer:
                moves.setdefault(layer, []).append(local_id)
    script_ids = {item["id"] for item in plan.script_objects}
    spaces = [i for i, k in plan.kinds.items() if k == "space"]
    spaces_native_back = _tx.has_feature(host, inst, _tx.FEATURE_SPACES_BACK)
    post: list[dict] = []
    for layer, ids_ in moves.items():
        post.append({"op": "layer", "layer": layer, "ids": ids_})
    if spaces and not spaces_native_back:
        post.append({"op": "order", "position": "back", "ids": spaces})
    if post and not host.can_script(inst):
        if moves:
            raise VWError("upgrade_required", "This bridge can neither create objects on a given layer nor move them "
                          "by script; nothing was created.", field="layer",
                          next_step="Make the layer active first (vw_view(layer=...)) and call vw_create without layer.")
        post = [p for p in post if p["op"] != "order"]
        warnings.append("Spaces were not sent behind the walls (bridge without run_script); use vw_edit op='order'.")

    namehelp.warn_new_classes(host, target, {op["params"].get("class_name") for op in plan.primary + plan.hosted} |
                           {(item.get("style") or {}).get("class") for item in plan.script_objects} |
                           {item.get("class") for item in opening_items}, warnings)
    _tx.fill_descriptors(plan.primary + plan.hosted, descriptors)
    fixups: list[dict] = []
    if plan.fixups:
        try:
            fixups = ops._normalise_execute_operations(plan.fixups, coordinate_units="mm",
                                                       max_operations=max(len(plan.fixups), 1))
        except ValueError as exc:
            raise VWError("validation_error", str(exc)) from None
        _tx.fill_descriptors(fixups, descriptors)
    key = host.session.idempotency_key(a.get("idempotency_key"), [plan.primary, plan.hosted] +
                                       ([opening_items] if opening_items else []), target)
    created_ids: dict[str, str] = {}
    receipts: list = []
    replayed = False
    wall_joins: list = []
    steps = 0
    retarget = lambda: host.session.target(a.get("document"), write=True, bind_apply=True)  # noqa: E731
    native_ops = plan.primary + plan.hosted + fixups
    if not (plan.primary or plan.hosted):
        pass  # script-only plan (extrudes / columns)
    elif inst.v2:
        # v2: native resolves "$wall" refs inside one transaction (contract 2.3 vw_create); plans above the native
        # cap run as consecutive transactions (refs to earlier ones are rewritten to uuids).
        try:
            summary = _tx.run_chunked(host, target, native_ops, key, ctx, retarget=retarget, join_walls=join_walls,
                                      max_ops=max_ops)
        except VWError as exc:
            namehelp.enrich_symbol_error(host, target, exc)
            raise
        steps = summary["undo_steps"]
        replayed = bool(summary.get("replayed"))
        created_ids.update(_tx.ids_from(summary))
        receipts += [r for r in summary["receipts"] if r.get("op", "create") == "create"]
        wall_joins += summary.get("wall_joins") or []
        if steps > 1:
            warnings.append(f"The plan has more than {max_ops} native operations (the bridge's per-transaction cap): it "
                            f"ran as {steps} consecutive transactions ({steps} undo steps).")
    else:
        steps = 1
        summary = _tx.run_general(host, target, plan.primary, key, ctx, join_walls=join_walls, max_ops=max_ops)
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
            summary2 = _tx.run_general(host, target2, plan.hosted + fixups, key + "-h", ctx, max_ops=max_ops)
            created_ids.update(_tx.ids_from(summary2))
            receipts += [r for r in summary2["receipts"] if r.get("op", "create") == "create"]
    native_steps = steps
    if opening_items and key in _OPENINGS_DONE and (replayed or not (plan.primary or plan.hosted)):
        # same call again (same idempotency key): report the openings it inserted instead of inserting them twice
        for row in _OPENINGS_DONE[key]:
            receipts.append(dict(row))
            created_ids[row["operation_id"]] = row["uuid"]
        warnings.append("Replayed: this call was already applied (same idempotency key); nothing new was created.")
        opening_items = []
    script_result: dict = {}
    restyle = [{"id": i, "uuid": created_ids[i], "style": st} for i, st in plan.styles if created_ids.get(i)]
    script_objects, deferred = _split_pio(plan.script_objects)
    post_ops = []
    for item in post:
        refs = [("uuid:" + created_ids[i]) if i in created_ids else ("$" + i) for i in item["ids"]
                if i in created_ids or i in script_ids]
        if refs:
            post_ops.append({k: v for k, v in item.items() if k != "ids"} | {"targets": refs})
    if script_objects or restyle or post_ops or opening_items:
        if steps:
            target = host.session.target(a.get("document"), write=True)
        try:
            script_args = {"objects": script_objects, "restyle": restyle, "post": post_ops}
            if opening_items:
                script_args["openings"] = openings.script_args(opening_items, created_ids)
            script_result = host.script(target, "massing", script_args, label="MCP create", ctx=ctx) or {}
        except VWError as exc:
            if steps and exc.commit_state != "unknown":
                _compensate(host, a.get("document"), plan, created_ids, exc, ctx, key, max_ops)
            raise
        warnings.extend(script_result.get("warnings") or [])
        steps += 1
        for row in script_result.get("created") or []:
            receipt = {"operation_id": row["id"], "uuid": row["uuid"], "type": row["type"]}
            if row.get("wall"):
                receipt["parent_uuid"] = row["wall"]
            receipts.append(receipt)
            created_ids[row["id"]] = row["uuid"]
    if opening_items:
        # sizes + parameters + name/class of the inserted doors/windows: native update_parametric (vw_edit's path)
        param_ops = openings.param_ops(opening_items, created_ids, descriptors, _tx.doc_mm_per_unit(target),
                                        schemas)
        if param_ops:
            target = host.session.target(a.get("document"), write=True, bind_apply=True)
            try:
                psum = _tx.run_chunked(host, target, param_ops, key + "-p", ctx, retarget=retarget, max_ops=max_ops)
            except VWError as exc:
                if exc.commit_state != "unknown":
                    _compensate(host, a.get("document"), plan, created_ids, exc, ctx, key, max_ops)
                raise
            steps += psum["undo_steps"]
        done_ids = {item["id"] for item in opening_items}
        _OPENINGS_DONE[key] = [r for r in receipts if r.get("operation_id") in done_ids]
        while len(_OPENINGS_DONE) > 64:
            _OPENINGS_DONE.pop(next(iter(_OPENINGS_DONE)))
        warnings.append(f"{len(opening_items)} door(s)/window(s) were inserted into their walls by script (real wall "
                        "breaks: they survive wall class changes, moves and resets), then sized natively: the "
                        "walls, the insertion and the sizing are separate undo steps (see undo_steps).")
    if deferred:
        # Live (VW 2024, Column2): a new plug-in object initialises its record fields on its FIRST regeneration,
        # which runs only after the creating script returned, overwriting most values set in that script (but it
        # takes e.g. 'OA Height' from them). So the parameters are set again in a second script (+1 undo step).
        pio_ops = [{"op": "pio", "targets": [f"uuid:{created_ids[i]}"], "parameters": params, "scale_mm": scale_mm}
                   for i, params, scale_mm in deferred if created_ids.get(i)]
        target = host.session.target(a.get("document"), write=True)
        try:
            res = host.script(target, "edit_engine", {"ops": pio_ops}, label="MCP create parameters", ctx=ctx) or {}
        except VWError as exc:
            if exc.commit_state != "unknown":
                _compensate(host, a.get("document"), plan, created_ids, exc, ctx, key, max_ops)
            raise
        steps += 1
        checks = [{"uuid": r["uuid"], "expect": r["expect"]} for r in res.get("receipts") or [] if r.get("expect")]
        _tx.verify_pio(host, target, checks, warnings)
    out = _receipt(plan, receipts, created_ids, key, steps, call_layer, active_layer, native_layer)
    if script_result.get("styled"):
        out["styled"] = script_result["styled"] if len(script_result["styled"]) <= COMPACT_OVER else \
            len(script_result["styled"])
    if script_result.get("imported_resources"):
        out["imported_resources"] = script_result["imported_resources"]
    moved = [r for r in script_result.get("post") or [] if r.get("op") == "layer"]
    if moved:
        out["layer_fallback"] = (f"{len(moved)} object(s) moved to their layer by script in the same call (this "
                                 "bridge cannot create on a given layer natively)")
    if spaces and (spaces_native_back or any(r.get("op") == "order" for r in script_result.get("post") or [])):
        # bridge 1.0.x: auto_order sends spaces (and slabs) behind walls inside the same transaction
        out["spaces"] = "sent to the back of their layer (behind walls, doors and windows)"
    if steps > native_steps and native_steps and (plan.script_objects or restyle or post_ops) and not opening_items:
        warnings.append("Native objects and the script-made part (extrudes/columns/graphic styles/layer moves/space "
                        "order) are separate undo steps (native apply_operations and run_script cannot share one "
                        "undo event).")
    if wall_joins:
        out["wall_joins"] = wall_joins if len(wall_joins) <= COMPACT_OVER else \
            {"count": len(wall_joins), "joined": sum(1 for j in wall_joins if j.get("joined", True))}
    if fp_counts:
        out["floor_plan"] = fp_counts
    if target.document:
        out["document"] = doc_brief(target.document)
    three_d = any(k in preview.THREE_D_KINDS for k in plan.kinds.values()) or bool(plan.script_objects)
    out["next_step"] = ("Done: this receipt is authoritative (every listed object exists). Do not re-create or re-check "
                        "each object; " + ("LOOK at the preview image before reporting success." if
                                           preview.wanted(want_preview, out["count"], three_d) else
                                           "use vw_capture() once if a visual check is needed."))
    if preview.wanted(want_preview, out["count"], three_d):
        top = [created_ids[i] for i in created_ids if i not in hosted_ids]
        return preview.attach(host, a.get("document"), out, warnings, uuids=top, three_d=three_d)
    return out


def _auto_id(local_id: str) -> bool:
    return local_id[:1] == "o" and local_id[1:].isdigit()


def _receipt(plan, receipts: list, created_ids: dict, key: str, steps: int, call_layer, active_layer: str,
             native_layer: bool) -> dict:
    hosted = {op["operation_id"] for op in plan.hosted} | set(plan.opening_flags)
    count = len(created_ids)
    out: dict = {"ok": True, "count": count}
    actual = {(r.get("operation_id") or r.get("local_ref")): r["layer"] for r in receipts if r.get("layer")}
    layers_used = {i: actual.get(i) or plan.layers.get(i) or active_layer for i in created_ids if i not in hosted}
    distinct = sorted({l for l in layers_used.values() if l})
    if count <= COMPACT_OVER:
        created = []
        for r in receipts:
            local = r.get("operation_id") or r.get("local_ref")
            entry = {"id": local, "uuid": r.get("uuid"),
                     "type": r.get("type") or _KIND_TYPES.get(plan.kinds.get(local, ""), plan.kinds.get(local))}
            if r.get("parent_uuid"):
                entry["wall"] = r["parent_uuid"]
            if len(distinct) > 1 and local in layers_used:
                entry["layer"] = layers_used[local]
            created.append({k: v for k, v in entry.items() if v})
        out["created"] = created
        out["ids"] = created_ids
    else:
        # big plans: the uuids ONCE, in the order of the objects list (+ the ids the caller named)
        out["types"] = dict(Counter(plan.kinds.get(i) or "object" for i in created_ids))
        order = sorted(created_ids, key=lambda i: (plan.index_of.get(i, 1e9), i))
        out["uuids"] = [created_ids[i] for i in order]
        named = {i: n for n, i in enumerate(order) if not _auto_id(i)}
        if named:
            out["named"] = named
        out["uuids_order"] = "objects list order; named = your id -> index in uuids"
    if len(distinct) == 1:
        out["layer"] = distinct[0]
    elif distinct:
        out["layers"] = dict(Counter(layers_used.values()))
    out["idempotency_key"] = key
    out["undo"] = {1: "one undo step", 2: "two undo steps"}.get(steps, f"{steps} undo steps")
    out["undo_steps"] = steps
    return out
