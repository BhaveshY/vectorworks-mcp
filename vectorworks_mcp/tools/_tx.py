"""Shared transaction runner for vw_create / vw_edit / vw_apply."""

from __future__ import annotations

from typing import Any, Optional

from .. import ops
from ..documents import Target, plan_hash
from ..errors import VWError
from ..host import Host

_TYPE_ALIASES = {"linear_dimension": {"linear_dimension", "dimension"}, "rect": {"rect", "rectangle", "box"},
                 "polygon": {"polygon", "polyline"}}


def check_create_types(target: Target, normalised: list[dict]) -> None:
    advertised = target.inst.status.get("create_object_types")
    if not isinstance(advertised, list) or not advertised:
        return
    have = {str(t) for t in advertised}
    requested = {op["params"].get("object_type") for op in normalised if op["type"] == "create"}
    missing = sorted(t for t in requested if t and not (have & _TYPE_ALIASES.get(t, {t})))
    if missing:
        err = target.inst.v2 and "unsupported" or "upgrade_required"
        raise VWError(err, f"This Vectorworks bridge cannot create: {', '.join(missing)}. Do not substitute other geometry.",
                      details={"supported_types": sorted(have)})


def run_general(host: Host, target: Target, normalised: list[dict], key: str, ctx, *, label: str = "") -> dict:
    """Compile + send one apply_operations transaction; returns the compact receipt."""
    if not normalised:
        return {"committed": True, "applied": 0, "receipts": []}
    check_create_types(target, normalised)
    try:
        wire = ops.compile_general(normalised)
    except ValueError as exc:
        raise VWError("validation_error", str(exc)) from None
    digest = plan_hash([normalised, (target.document or {}).get("fingerprint")])
    host.session.check_plan(key, digest)
    params = ops.wire_params(wire, key)
    result = host.call(target, "apply_operations", params, kind="write", ctx=ctx, idempotency_key=key)
    summary = ops.summarise_transaction(result, len(wire))
    if not summary["committed"]:
        raise VWError("native_rejected", "Vectorworks did not commit the transaction; the document is unchanged.",
                      commit_state="rolled_back", details={"reply": _short(result)})
    host.session.remember_plan(key, digest)
    return summary


def run_documentation(host: Host, target: Target, normalised: list[dict], key: str, ctx) -> dict:
    inst = target.inst
    if not inst.supports("apply_documentation_operations"):
        raise VWError("unsupported" if inst.v2 else "upgrade_required",
                      "Sheet layer / viewport / annotation writes are not enabled in this bridge. Documentation reads "
                      "(vw_read what=sheets|viewports|annotations) work; ask the user to make the sheet change manually.")
    wire = ops.compile_documentation(normalised)
    params = ops.wire_params(wire, key, {"plan_hash": plan_hash(normalised)})
    if not inst.v2:
        from ..documents import rev5_read_binding
        info = host.bridge.call(inst, "get_document_info", {})
        params.update(rev5_read_binding(info))
        binding = info.get("binding") if isinstance(info, dict) else {}
        if isinstance(binding, dict) and isinstance(binding.get("dirty"), bool):
            params["expected_dirty"] = binding["dirty"]
            params["expected_dirty_is_set"] = True
    result = host.call(target, "apply_documentation_operations", params, kind="write", ctx=ctx, idempotency_key=key)
    return ops.summarise_transaction(result, len(wire))


def ids_from(summary: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for receipt in summary.get("receipts", []):
        local = receipt.get("operation_id") or receipt.get("local_ref")
        if local and receipt.get("uuid"):
            out[str(local)] = str(receipt["uuid"])
    return out


def _short(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: v for k, v in list(value.items())[:12] if k != "timing"}
    return value


def fill_descriptors(normalised: list[dict], descriptors: dict[str, str]) -> None:
    for op in normalised:
        params = op["params"]
        if params.get("descriptor_fingerprint") == "pending":
            plugin = params.get("plugin_name")
            if plugin not in descriptors:
                raise VWError("validation_error", f"Could not discover the {plugin} plug-in schema.", field="plugin")
            params["descriptor_fingerprint"] = descriptors[plugin]


def load_schemas(host: Host, target: Optional[Target], plugins: set[str]) -> tuple[dict[str, str], dict[str, dict]]:
    descriptors: dict[str, str] = {}
    schemas: dict[str, dict] = {}
    if target is None:
        return descriptors, schemas
    for plugin in sorted(plugins):
        schema = host.schema(target.inst, plugin)
        schemas[plugin] = schema
        fingerprint = schema.get("descriptor_fingerprint") if isinstance(schema, dict) else None
        if fingerprint:
            descriptors[plugin] = str(fingerprint)
    return descriptors, schemas
