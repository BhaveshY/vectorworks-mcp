---
name: work
description: Work with Vectorworks through the compact fast-native MCP tools for CAD and BIM tasks in Vectorworks 2024/2025.
---

# Vectorworks Work

## Use the grouped native surface

Normal work requires the non-modal native SDK bridge. The bridge must report
`native_phase >= 4`, `capability_revision >= 4`, a non-empty
`capability_fingerprint`, `cad_api_safe=true`, `transport_only=false`, and a
ready main-context pump. Treat a missing or stale manifest as an upgrade or
restart error. Never infer support from the native phase.

The fast-native profile exposes only these tools:

- `vw_status` for health and compact document context.
- `vw_read` for document, layer, summary, object query, and selection reads.
- `vw_catalog` for capabilities, classes, symbols, parametric schemas,
  worksheets, and resources.
- `vw_apply` for one atomic mutation plan.
- `vw_execute_operations` as the same atomic engine under its established
  name. It is not a fallback path.
- `vw_io` for advertised native import, export, and capture actions.
- `vw_view` for advertised native get, set, fit, and capture actions.
- `vw_document` for advertised native info, save, export, and open actions.
- `vw_tool_safety` for exact grouped action safety and retry metadata.

Do not switch to compatibility tools, a Python listener, mouse automation,
schematic geometry, per-object decomposition, or another native action after a
capability failure.

## Choose the shortest correct workflow

For a fully specified create or edit, call `vw_apply` immediately with one
complete plan and a unique `idempotency_key`. The write core performs its own
native preflight. Use `vw_status(action="context")` first when the operation
depends on the active document or when the manifest identity is not known.

Use `vw_read` when the plan depends on existing state. Supply `layer` and
`object_type` with `action="query"` when either filter matters. Request only the
fields needed for the next decision.

Use `vw_catalog(action="capabilities")` to inspect the exact native manifest.
Use `vw_catalog(action="parametric_schemas", query="<universal plugin name>")`
before creating or updating a generic parametric object. Use universal
parameter IDs and the returned descriptor fingerprint. Never guess fields from
localized labels.

Trust a successful semantic receipt. Verify with one focused `vw_read` only
when the receipt is incomplete, the edit depends on existing state, or the user
asks for verification. Do not add a screenshot or full-document scan after
every write.

## Build atomic BIM plans

Each operation has `type`, optional `operation_id`, and `params`. Creates use
the exact `object_type` advertised by the manifest. Later operations can refer
to an earlier result as `$<operation_id>`. Existing objects require an explicit
`uuid:`, `name:`, or `handle:` reference.

Supported operation types are `create`, `set_properties`, `transform`,
`reshape`, `update_parametric`, `duplicate`, and `delete`. Always state
`coordinate_units`; the connector normalizes typed geometry to millimetres.

Use true native object types only. A Space request includes a closed boundary,
height, name, and room ID. Slab and roof requests include their real footprint
and semantic parameters. Dedicated `door` and `window` creates require the
exact universal `plugin_name` (`Door` or `Window`), the live
`descriptor_fingerprint`, an exact raw `wall_uuid`, explicit insertion `x`/`y`,
width, and height. A window also requires `sill_height`. Both types
force verified wall hosting and can include the same typed universal parameter
list as a generic parametric create, except parameters that duplicate the
dedicated width/height or window elevation fields. If the bridge does not advertise the
needed type or action, stop. Do not substitute rectangles, extrusions, symbols,
or unhosted plug-in objects.

Reuse an `idempotency_key` only for the identical atomic plan. `vw_apply` and
`vw_execute_operations` share one implementation and one native
`apply_operations` transaction. Neither tool decomposes the plan.

## Handle files and document lifecycle safely

`vw_io` and `vw_document` do not offer idempotency keys. Their state-changing
actions are not safe to retry after send. Check `vw_tool_safety` for the exact
action before import, export, capture, save, open, or new-document work.

If a grouped result reports `error.code="unknown_commit_state"`,
`commit_state="unknown"`, or `retry_policy="never_after_send"`, do not repeat
the action. Reconnect, inspect the active document and the target file with
read-only calls, then decide what remains. A request with
`error.code="request_not_sent"` did not cross the transport boundary and is
safe to retry.

Ask before destructive document changes, file replacement, or a document
switch that can discard unsaved work. State the assumed units for dimensional
work.

For exact action and parameter mappings, read `references/tool-map.md` from
this plugin.
