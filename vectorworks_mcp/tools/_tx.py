"""Shared transaction runner for vw_create / vw_edit / vw_apply."""

from __future__ import annotations

from typing import Any, Callable, Optional

from .. import ops
from ..documents import Target, plan_hash
from ..errors import VWError
from ..host import Host

_TYPE_ALIASES = {"linear_dimension": {"linear_dimension", "dimension"}, "rect": {"rect", "rectangle", "box"},
                 "polygon": {"polygon", "polyline"}}
DEFAULT_MAX_OPERATIONS = 250  # bridge 1.0.0 (kMaxApplyOperations); newer bridges report limits.max_operations

# Optional native features (ping/capabilities feature lists, PROTOCOL.md). Each set lists the accepted spellings.
# Feature flags of bridge 1.0.x builds (PROTOCOL.md): op_layer (per-create-op `layer`), order_op (`order` op),
# auto_send_to_back (Spaces/slabs sent to the back on create, auto_order), capture_isolation, compact_import_receipts.
FEATURE_CREATE_LAYER = {"op_layer"}
FEATURE_ORDER = {"order_op"}
FEATURE_SPACES_BACK = {"auto_send_to_back"}
FEATURE_CAPTURE_ISOLATION = {"capture_isolation"}
FEATURE_COMPACT_IMPORT = {"compact_import_receipts"}


def native_features(host: Host, inst) -> set[str]:
    """Feature strings the bridge advertises (ping object_read_features + capabilities feature lists)."""
    feats: set[str] = set()
    for source in (inst.status, _caps(host, inst)):
        for key in ("object_read_features", "apply_features", "apply_operation_features", "features",
                    "create_features"):
            value = source.get(key) if isinstance(source, dict) else None
            if isinstance(value, list):
                feats |= {str(v) for v in value}
    caps = _caps(host, inst)
    for key in ("apply_operation_ops", "apply_ops", "operation_ops"):
        value = caps.get(key)
        if isinstance(value, list):
            feats |= {f"op:{v}" for v in value}
    return feats


def has_feature(host: Host, inst, names: set[str]) -> bool:
    feats = native_features(host, inst)
    return bool(feats & names) or any(f"op:{n}" in feats for n in names)


def max_operations(host: Host, inst) -> int:
    for source in (_caps(host, inst).get("limits"), _caps(host, inst), inst.status):
        if isinstance(source, dict):
            for key in ("max_operations", "max_apply_operations", "apply_operations_max"):
                value = source.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value
    return DEFAULT_MAX_OPERATIONS


def _caps(host: Host, inst) -> dict:
    if inst.capabilities is not None:
        return inst.capabilities
    try:
        return host.bridge.capabilities(inst) or {}
    except VWError:
        return {}


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


def run_general(host: Host, target: Target, normalised: list[dict], key: str, ctx, *, label: str = "",
                join_walls: Optional[bool] = None, max_ops: int = DEFAULT_MAX_OPERATIONS) -> dict:
    """Compile + send one apply_operations transaction; returns the compact receipt.

    join_walls: plan-level wall joining (native default true); sent only when given."""
    if not normalised:
        return {"committed": True, "applied": 0, "receipts": []}
    check_create_types(target, normalised)
    try:
        wire = ops.compile_general(normalised, max_operations=max_ops)
    except ValueError as exc:
        raise VWError("validation_error", str(exc)) from None
    digest_src: list = [normalised, (target.document or {}).get("fingerprint")]
    if join_walls is not None:
        digest_src.append({"join_walls": join_walls})
    digest = plan_hash(digest_src)
    host.session.check_plan(key, digest)
    params = ops.wire_params(wire, key, {"join_walls": bool(join_walls)} if join_walls is not None else None)
    result = host.call(target, "apply_operations", params, kind="write", ctx=ctx, idempotency_key=key)
    summary = ops.summarise_transaction(result, len(wire))
    if not summary["committed"]:
        raise VWError("native_rejected", "Vectorworks did not commit the transaction; the document is unchanged.",
                      commit_state="rolled_back", details={"reply": _short(result)})
    host.session.remember_plan(key, digest)
    return summary


def wire_count(op: dict) -> int:
    """Native operations one normalised op expands to (set_properties: one per edit and property)."""
    if op["type"] == "set_properties":
        return max(1, sum(len(e.get("properties") or {}) for e in op["params"].get("edits") or []))
    return 1


def split_chunks(normalised: list[dict], max_ops: int) -> list[list[dict]]:
    chunks: list[list[dict]] = [[]]
    count = 0
    for op in normalised:
        n = wire_count(op)
        if chunks[-1] and count + n > max_ops:
            chunks.append([])
            count = 0
        chunks[-1].append(op)
        count += n
    return chunks


def _rewrite_refs(chunk: list[dict], ids: dict[str, str]) -> list[dict]:
    """'$id' of an object created by an EARLIER transaction -> 'uuid:<u>' (doors' wall_uuid -> bare uuid)."""
    if not ids:
        return chunk
    out = []
    for op in chunk:
        params = dict(op["params"])
        for key in ("target", "wall_uuid"):
            value = params.get(key)
            if isinstance(value, str) and value.startswith("$") and value[1:] in ids:
                params[key] = ids[value[1:]] if key == "wall_uuid" else "uuid:" + ids[value[1:]]
        out.append({**op, "params": params})
    return out


def run_chunked(host: Host, target: Target, normalised: list[dict], key: str, ctx, *,
                retarget: Callable[[], Target], join_walls: Optional[bool] = None,
                max_ops: int = DEFAULT_MAX_OPERATIONS) -> dict:
    """One transaction when the plan fits the native cap, else consecutive transactions (undo_steps > 1).

    All-or-nothing where possible: when a later transaction fails, the objects the earlier ones CREATED are deleted
    again (compensation, one more undo step); edits of existing objects cannot be compensated (commit_state
    partial)."""
    chunks = split_chunks(normalised, max_ops)
    if len(chunks) == 1:
        summary = run_general(host, target, normalised, key, ctx, join_walls=join_walls, max_ops=max_ops)
        summary["undo_steps"] = 1
        return summary
    receipts: list = []
    joins: list = []
    ids: dict[str, str] = {}
    created: list[str] = []
    applied = 0
    only_creates = True
    for index, chunk in enumerate(chunks):
        chunk = _rewrite_refs(chunk, ids)
        if index:
            target = retarget()
        try:
            summary = run_general(host, target, chunk, f"{key}-t{index + 1}", ctx, join_walls=join_walls,
                                  max_ops=max_ops)
        except VWError as exc:
            if index and exc.commit_state != "unknown":
                if only_creates and created:
                    delete_uuids(host, retarget, created, key, ctx, exc, max_ops=max_ops,
                                 what=f"transactions 1-{index} of {len(chunks)}")
                else:
                    exc.commit_state = "partial"
                    exc.message += (f" (transactions 1-{index} of {len(chunks)} WERE applied: "
                                    f"{applied} operations; the rest was not)")
            raise
        applied += summary["applied"]
        receipts += summary["receipts"]
        joins += summary.get("wall_joins") or []
        new_ids = ids_from(summary)
        ids.update(new_ids)
        created += [r["uuid"] for r in summary["receipts"] if r.get("op", "create") == "create" and r.get("uuid")]
        only_creates = only_creates and all(op["type"] == "create" for op in chunk)
    out = {"committed": True, "applied": applied, "receipts": receipts, "undo_steps": len(chunks)}
    if joins:
        out["wall_joins"] = joins
    return out


def delete_uuids(host: Host, retarget: Callable[[], Target], uuids: list[str], key: str, ctx, exc: VWError, *,
                 max_ops: int = DEFAULT_MAX_OPERATIONS, what: str = "the native part") -> bool:
    """Compensating delete of objects this call created (doors/windows first: deleting a wall cascades them).
    Updates exc (message, commit_state, next_step); returns True when everything was removed."""
    try:
        normalised = ops._normalise_execute_operations(
            [{"type": "delete", "params": {"target": f"uuid:{u}"}} for u in uuids], coordinate_units="mm",
            max_operations=max(len(uuids), 1))
        for index, chunk in enumerate(split_chunks(normalised, max_ops)):
            target = retarget()
            run_general(host, target, chunk, host.session.idempotency_key(None, chunk, target), ctx, max_ops=max_ops)
    except (VWError, ValueError) as comp:
        exc.message = (f"{exc.message} ({what} WERE created ({len(uuids)} objects) and removing them again failed: "
                       f"{getattr(comp, 'message', comp)})")
        exc.commit_state = "partial"
        exc.details = dict(exc.details or {}, created_uuids=uuids[:300])
        exc.next_step = ("The objects in details.created_uuids exist. Fix the named problem and create only the "
                         "missing part; do not resend the whole call.")
        return False
    exc.message = (f"{exc.message} (nothing from this call remains: {what} created {len(uuids)} object(s) that "
                   "were deleted again)")
    exc.commit_state = "rolled_back"
    exc.details = dict(exc.details or {}, compensated=len(uuids))
    exc.next_step = ("The document is unchanged (the compensating delete is one extra undo step). Fix the named "
                     "problem and send the whole call again (with a NEW idempotency_key if you passed one).")
    return True


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


def verify_pio(host: Host, target: Target, items: list[dict], warnings: list) -> list[str]:
    """Plug-ins regenerate after the script returns: re-read the parametric-record fields set through the fallback
    and report values the plug-in changed back (the edit itself is committed)."""
    if not items:
        return []
    try:
        data = host.script(target, "pio_schema", {"verify": items}, label="Verify parameters", kind="read",
                           transaction=False) or {}
    except VWError as exc:
        warnings.append(f"Could not re-check the plug-in parameters after regeneration: {exc.message}")
        return []
    problems = list(data.get("problems") or [])
    if problems:
        warnings.append("The plug-in regenerated and did NOT keep: " + "; ".join(problems) +
                        ". It recomputes these from other parameters; set those instead, or delete the object and "
                        "create it again with the values (creation-time values are kept).")
    return problems


def doc_mm_per_unit(target: Optional[Target]) -> float:
    """Millimetres per document length unit, when the bridge reports it (document record `mm_per_unit`)."""
    if target is None:
        return 1.0
    for doc in (target.document, target.inst.status.get("active_document")):
        value = doc.get("mm_per_unit") if isinstance(doc, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    return 1.0


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
