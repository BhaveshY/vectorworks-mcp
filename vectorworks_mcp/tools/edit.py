"""vw_edit: change existing objects (native apply_operations; mirror / layer /
group / ungroup / record through the vetted run_script edit engine)."""

from __future__ import annotations

from .. import builders
from .. import coerce as C

from ..builders import SCRIPT_EDIT_OPS
from ..errors import VWError
from ..host import doc_brief, get_host
from . import _tx

EXAMPLE = [{"op": "move", "target": "uuid:1A2B...", "by": [1000, 0]}]


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


def handle(args: dict, ctx, warnings: list) -> dict:
    a = C.lower_keys(args)
    raw_changes = a.get("changes") or a.get("edits") or a.get("operations")
    if raw_changes is None and ("op" in a or "action" in a):
        raw_changes = [{k: v for k, v in a.items() if k not in {"units", "document", "dry_run", "idempotency_key"}}]
    changes = C.obj_list(raw_changes, "changes", example=EXAMPLE)
    units = C.units_name(a.get("units") or a.get("coordinate_units"))
    scale = C.scale_for(units)
    dry_run = C.boolean(a.get("dry_run", False), "dry_run")
    host = get_host()
    plan = builders.compile_edit(changes, scale, schemas={}, descriptors={})  # validate before touching Vectorworks
    target = None
    descriptors: dict = {}
    if not dry_run:
        target = host.session.target(a.get("document"), write=True, bind_apply=True)
        plugins = {str(C.lower_keys(c).get("plugin") or C.lower_keys(c).get("plugin_name")) for c in changes
                   if str(C.lower_keys(c).get("op", "")).lower() in {"parametric", "update_parametric", "params"}
                   and (C.lower_keys(c).get("plugin") or C.lower_keys(c).get("plugin_name"))}
        if plugins:
            descriptors, schemas = _tx.load_schemas(host, target, plugins)
            plan = builders.compile_edit(changes, scale, schemas=schemas, descriptors=descriptors)
    if dry_run:
        return {"ok": True, "dry_run": True, "native_operations": plan.ops, "script_operations":
                [s for s in plan.script_ops if s["op"] in SCRIPT_EDIT_OPS], "selection_operations": plan.selection_ops}
    if plan.needs_selection:
        refs = _selection_refs(host, target)
        if not refs:
            raise VWError("not_found", "target='selection' but nothing is selected in Vectorworks.",
                          next_step="Select objects first (vw_edit op='select') or pass uuids.")
        builders.expand_selection(plan, refs)
    out: dict = {"ok": True, "applied": 0, "receipts": []}
    inst = target.inst
    steps = 0
    if plan.ops:
        normalised = builders.normalise_edit_ops(plan.ops)
        _tx.fill_descriptors(normalised, descriptors)
        key = host.session.idempotency_key(a.get("idempotency_key"), normalised, target)
        summary = _tx.run_general(host, target, normalised, key, ctx)
        out["applied"] += summary["applied"]
        out["receipts"] += summary["receipts"]
        out["idempotency_key"] = key
        dup_ids = _tx.ids_from(summary)
        if dup_ids:
            out["ids"] = dup_ids
        steps += 1
    script_ops = [s for s in plan.script_ops if s["op"] in SCRIPT_EDIT_OPS]
    if script_ops:
        if not host.can_script(inst):
            raise host.bridge.missing_action(inst, "run_script")
        if steps:
            target = host.session.target(a.get("document"), write=True)
        result = host.script(target, "edit_engine", {"ops": script_ops}, label="MCP edit", ctx=ctx)
        if isinstance(result, dict):
            out["applied"] += int(result.get("applied") or 0)
            out["receipts"] += result.get("receipts") or []
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
        warnings.append("Native edits and script edits ran as two undo steps.")
    if target.document:
        out["document"] = doc_brief(target.document)
    return out
