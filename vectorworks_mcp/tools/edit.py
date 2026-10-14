"""vw_edit: change existing objects (native apply_operations; mirror / layer / order / group / ungroup / record,
wall thickness/height/style, text content, Space room name and graphic styles (line type, hatch/gradient fill, text
font/size/style/alignment, markers) through the vetted run_script edit engine)."""

from __future__ import annotations

from .. import builders, routing
from .. import names as namehelp
from .. import coerce as C

from ..builders import SCRIPT_EDIT_OPS
from ..errors import VWError
from ..host import doc_brief, get_host
from . import _tx, preview

EXAMPLE = [{"op": "move", "target": "uuid:1A2B...", "by": [1000, 0]}]
SCRIPT_OPS = SCRIPT_EDIT_OPS | {"order", "wall", "text", "space_name"}


def _selection_refs(host, target) -> list[str]:
    refs: list[str] = []
    offset = 0
    while True:
        data = host.call(target, "query_objects", {"mode": "selection", "offset": offset, "limit": 1000, "layer": "",
                                                   "object_type": "", "criteria": "ALL", "field_count": 2,
                                                   "field_1": "uuid", "field_2": "type"})
        items = data.get("items") if isinstance(data, dict) else []
        refs += [f"uuid:{i['uuid']}" for i in items or [] if isinstance(i, dict) and i.get("uuid")]
        if not (isinstance(data, dict) and data.get("has_more")) or len(refs) >= 5000:
            return refs
        offset += len(items)


def target_records(host, target, refs: list[str]) -> dict[str, dict]:
    """ref (uuid:/name:) -> native object record (type, plugin_name, ...)."""
    out: dict[str, dict] = {}
    uuids = sorted({r[5:] for r in refs if r.startswith("uuid:")})
    if uuids:
        if target.inst.v2:
            data = host.call(target, "query_objects", {"mode": "objects", "uuids": uuids, "offset": 0,
                                                       "limit": min(1000, len(uuids) + 5), "layer": "",
                                                       "object_type": "", "criteria": "ALL", "field_count": 0})
            for item in (data.get("items") if isinstance(data, dict) else None) or []:
                if isinstance(item, dict) and item.get("uuid"):
                    out["uuid:" + str(item["uuid"])] = item
            # bridges return the uuid's own spelling; match case-insensitively
            lower = {k.lower(): v for k, v in out.items()}
            out.update({f"uuid:{u}": lower[f"uuid:{u}".lower()] for u in uuids if f"uuid:{u}".lower() in lower})
        else:
            from .read import _find_by_uuid
            for u in uuids:
                item = _find_by_uuid(host, target, u, [])
                if item:
                    out["uuid:" + u] = item
    for ref in refs:
        if ref.startswith("name:") and ref not in out:
            data = host.call(target, "query_objects", {"mode": "criteria", "offset": 0, "limit": 2,
                                                       "criteria": "((N='" + ref[5:].replace("'", "''") + "'))",
                                                       "layer": "", "object_type": "", "field_count": 0})
            items = (data.get("items") if isinstance(data, dict) else None) or []
            if items:
                out[ref] = items[0]
    return out


def handle(args: dict, ctx, warnings: list):
    a = C.lower_keys(args)
    raw_changes = a.get("changes") or a.get("edits") or a.get("operations")
    if raw_changes is None and ("op" in a or "action" in a):
        raw_changes = [{k: v for k, v in a.items() if k not in {"units", "document", "dry_run", "idempotency_key",
                                                                 "preview"}}]
    changes = C.obj_list(raw_changes, "changes", example=EXAMPLE)
    units = C.units_name(a.get("units") or a.get("coordinate_units"))
    scale = C.scale_for(units)
    dry_run = C.boolean(a.get("dry_run", False), "dry_run")
    want_preview = C.boolean(a["preview"], "preview") if a.get("preview") not in (None, "") else None
    host = get_host()
    plan = builders.compile_edit(changes, scale, schemas={}, descriptors={})  # validate before touching Vectorworks
    target = None
    descriptors: dict = {}
    if not dry_run:
        target = host.session.target(a.get("document"), write=True, bind_apply=True)
        native_order = _tx.has_feature(host, target.inst, _tx.FEATURE_ORDER)
        plugins = {str(C.lower_keys(c).get("plugin") or C.lower_keys(c).get("plugin_name")) for c in changes
                   if str(C.lower_keys(c).get("op", "")).lower() in {"parametric", "update_parametric", "params"}
                   and (C.lower_keys(c).get("plugin") or C.lower_keys(c).get("plugin_name"))}
        schemas: dict = {}
        if plugins:
            descriptors, schemas = _tx.load_schemas(host, target, plugins)
        if plugins or native_order:
            plan = builders.compile_edit(changes, scale, schemas=schemas, descriptors=descriptors,
                                         native_order=native_order)
    if dry_run:
        warnings.extend(plan.warnings)
        out = {"ok": True, "dry_run": True, "native_operations": plan.ops, "script_operations":
               [s for s in plan.script_ops if s["op"] in SCRIPT_OPS], "selection_operations": plan.selection_ops}
        if plan.typed:
            out["typed_values"] = [{"targets": t["targets"], "values": t.get("values"), "kind": t["kind"]}
                                   for t in plan.typed]
            out["note"] = "typed_values are routed by the target's type (door width, wall thickness, ...) when run."
        return out
    if plan.needs_selection:
        refs = _selection_refs(host, target)
        if not refs:
            raise VWError("not_found", "target='selection' but nothing is selected in Vectorworks.",
                          next_step="Select objects first (vw_edit op='select') or pass uuids.")
        builders.expand_selection(plan, refs)
    if plan.typed:
        refs = sorted({t for entry in plan.typed for t in entry["targets"]})
        info = target_records(host, target, refs)
        extra = routing.plugins_needed(plan, info) - set(descriptors)
        schemas_all: dict = {}
        if extra:
            more_desc, schemas_all = _tx.load_schemas(host, target, extra)
            descriptors.update(more_desc)
        routing.route_typed(plan, info, scale, schemas_all, descriptors)
    warnings.extend(dict.fromkeys(plan.warnings))
    namehelp.warn_new_classes(host, target, {e["properties"].get("class") for op in plan.ops if op.get("type") ==
                                          "set_properties" for e in op["params"]["edits"]}, warnings)
    out: dict = {"ok": True, "applied": 0, "receipts": []}
    inst = target.inst
    steps = 0
    max_ops = _tx.max_operations(host, inst)
    if plan.ops:
        normalised = builders.normalise_edit_ops(plan.ops, max_ops=max(max_ops, len(plan.ops)))
        _tx.fill_descriptors(normalised, descriptors)
        key = host.session.idempotency_key(a.get("idempotency_key"), normalised, target)
        retarget = lambda: host.session.target(a.get("document"), write=True, bind_apply=True)  # noqa: E731
        summary = _tx.run_chunked(host, target, normalised, key, ctx, retarget=retarget, max_ops=max_ops)
        out["applied"] += summary["applied"]
        out["receipts"] += summary["receipts"]
        out["idempotency_key"] = key
        cascaded = [u for r in summary["receipts"] for u in r.get("cascaded_inserts") or []]
        if cascaded:
            out["cascaded_inserts"] = cascaded
            warnings.append(f"Deleting wall(s) also deleted {len(cascaded)} inserted door/window/symbol object(s).")
        dup_ids = _tx.ids_from(summary)
        if dup_ids:
            out["ids"] = dup_ids
        steps += summary.get("undo_steps", 1)
    script_ops = [s for s in plan.script_ops if s["op"] in SCRIPT_OPS]
    if script_ops:
        if not host.can_script(inst):
            raise host.bridge.missing_action(inst, "run_script")
        if steps:
            target = host.session.target(a.get("document"), write=True)
        result = host.script(target, "edit_engine", {"ops": script_ops}, label="MCP edit", ctx=ctx)
        if isinstance(result, dict):
            out["applied"] += int(result.get("applied") or 0)
            out["receipts"] += result.get("receipts") or []
            if result.get("imported_resources"):
                out["imported_resources"] = result["imported_resources"]
            warnings.extend(result.get("warnings") or [])
            checks = [{"uuid": r["uuid"], "expect": r.pop("expect")} for r in out["receipts"]
                      if isinstance(r, dict) and r.get("op") == "pio" and r.get("expect")]
            not_kept = _tx.verify_pio(host, target, checks, warnings)
            if not_kept:
                out["not_kept"] = not_kept
            labels = [r for r in out["receipts"] if isinstance(r, dict) and r.get("op") == "space_name"]
            if labels:
                _verify_space_labels(host, target, labels, warnings)
        steps += 1
    for sel in plan.selection_ops:
        if sel["op"] == "deselect":
            host.call(target, "selection", {"action": "clear"}, kind="write")
        else:
            criteria = sel.get("criteria")
            if not criteria:
                names = [t[5:] for t in sel["targets"] if t.startswith("name:")]
                uuids = [t for t in sel["targets"] if not t.startswith("name:")]
                if uuids:
                    if not host.can_script(inst):
                        raise VWError("upgrade_required" if not inst.v2 else "unsupported",
                                      "Selecting by uuid needs run_script; select by name or criteria instead.")
                    host.call(target, "run_script", {"language": "python", "transaction": False, "undo_label": "Select",
                                                     "code": "_n = 0\nfor u in %r:\n    _h = vs.GetObjectByUuid(u[5:] if u.startswith('uuid:') else u)\n    if _h:\n        vs.SetSelect(_h)\n        _n += 1\nresult = _n" % (uuids,)},
                              kind="write")
                if names:
                    criteria = "(" + " | ".join(f"(N='{n.replace(chr(39), chr(39) * 2)}')" for n in names) + ")"
            if criteria:
                host.call(target, "selection", {"action": "select", "criteria": criteria}, kind="write")
        out["selection"] = "updated"
    if steps > 1:
        warnings.append(f"This edit ran as {'two' if steps == 2 else steps} undo steps (native edits and script "
                        "edits cannot share one).")
    out["undo_steps"] = steps
    if target.document:
        out["document"] = doc_brief(target.document)
    if len(out["receipts"]) > 20:
        out["receipts"] = {"count": len(out["receipts"]), "first": out["receipts"][:5]}
    touched = _touched_uuids(plan, out)
    if touched and preview.wanted(want_preview, len(touched), False):
        return preview.attach(host, a.get("document"), out, warnings, uuids=touched)
    return out


def _touched_uuids(plan, out: dict) -> list[str]:
    uuids: list[str] = []
    for item in plan.script_ops:
        uuids += [t[5:] for t in item.get("targets", []) if t.startswith("uuid:")]
    for op in plan.ops:
        params = op.get("params") or {}
        for ref in [params.get("target")] + [e.get("ref") for e in params.get("edits") or []]:
            if isinstance(ref, str) and ref.startswith("uuid:") and op.get("type") != "delete":
                uuids.append(ref[5:])
    uuids += list((out.get("ids") or {}).values())
    return list(dict.fromkeys(uuids))


def _verify_space_labels(host, target, labels: list[dict], warnings: list) -> None:
    """The Space regenerates after the script returns: read the displayed label back."""
    try:
        data = host.script(target, "object_detail", {"labels": [r["uuid"] for r in labels]}, label="Read labels",
                           kind="read", transaction=False) or {}
    except VWError as exc:
        warnings.append(f"Could not re-read the Space labels: {exc.message}")
        return
    got = data.get("labels") or {}
    for row in labels:
        row["room_name"] = got.get(row["uuid"], row.get("room_name"))
        want = row.pop("expect_label", None)
        if want is not None and row["room_name"] != want:
            warnings.append(f"Space {row['uuid']} shows {row['room_name']!r} after regeneration, not {want!r}.")
