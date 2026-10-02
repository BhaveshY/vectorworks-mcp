"""vw_apply: the 0.7 mixed create+edit transaction grammar, kept verbatim."""

from __future__ import annotations

from .. import coerce as C
from .. import ops
from ..errors import VWError
from ..host import doc_brief, get_host
from . import _tx

EXAMPLE = [{"type": "create", "operation_id": "r1",
            "params": {"object_type": "rect", "x1": 0, "y1": 0, "x2": 1000, "y2": 500}}]


def handle(args: dict, ctx, warnings: list) -> dict:
    a = C.lower_keys(args)
    units = C.units_name(a.get("coordinate_units") or a.get("units"))
    operations = C.parse_json_maybe(a.get("operations") or a.get("ops") or a.get("plan"))
    if operations in (None, "", []):
        raise VWError("validation_error", "operations is required", field="operations", example=EXAMPLE)
    host = get_host()
    target = None
    try:
        normalised = ops.normalise_plan(operations, units)
    except ValueError as exc:
        if "descriptor_fingerprint" not in str(exc):
            raise _validation(exc) from None
        # Forgiving: discover the live descriptor for door/window/parametric creates and retry.
        target = host.session.target(a.get("document"), write=True, bind_apply=True)
        operations = _prefill(host, target, operations)
        try:
            normalised = ops.normalise_plan(operations, units)
        except ValueError as exc2:
            raise _validation(exc2) from None
    if a.get("target_binding"):
        warnings.append("target_binding is no longer needed and was ignored; the host pins the document automatically.")
    if target is None:
        target = host.session.target(a.get("document"), write=True, bind_apply=True)
    key = host.session.idempotency_key(a.get("idempotency_key"), normalised, target)
    if ops.is_documentation_plan(normalised):
        summary = _tx.run_documentation(host, target, normalised, key, ctx)
    else:
        summary = _tx.run_general(host, target, normalised, key, ctx)
    out = {"ok": True, "committed": summary["committed"], "applied": summary["applied"],
           "receipts": summary["receipts"], "idempotency_key": key, "coordinate_units": units}
    ids = _tx.ids_from(summary)
    if ids:
        out["ids"] = ids
    if summary.get("replayed"):
        out["replayed"] = True
    if target.document:
        out["document"] = doc_brief(target.document)
    return out


def _validation(exc: Exception) -> VWError:
    message = str(exc)
    field = message.split(" ", 1)[0] if message.startswith("operations[") else "operations"
    return VWError("validation_error", message, field=field, example=EXAMPLE)


def _prefill(host, target, operations):
    if isinstance(operations, dict):
        operations = [operations]
    out = []
    for op in operations if isinstance(operations, list) else []:
        op = ops._forgive_operation(op) if isinstance(op, dict) else op
        if isinstance(op, dict) and isinstance(op.get("params"), dict):
            p = dict(op["params"])
            kind = str(p.get("object_type") or p.get("type") or "").lower()
            plugin = p.get("plugin_name") or {"door": "Door", "window": "Window"}.get(kind)
            if plugin and not p.get("descriptor_fingerprint"):
                p["plugin_name"] = plugin
                p["descriptor_fingerprint"] = str(host.schema(target.inst, plugin).get("descriptor_fingerprint") or "")
            op = dict(op, params=p)
        out.append(op)
    return out
