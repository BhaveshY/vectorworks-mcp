# Vectorworks MCP Tool Map

## Production contract

The `fast-native` profile is the only production workflow. Every grouped call
requires the native SDK bridge, phase 4, capability revision 4 or newer, a
non-empty capability fingerprint, CAD-safe dispatch, and a ready main-context
pump. The host uses only `implemented_actions` and `create_object_types` from
the bridge manifest. It does not derive support from the phase number.

The profile has nine top-level tools. No capability adds another MCP tool.

| Tool | Actions | Purpose |
|------|---------|---------|
| `vw_status` | `health`, `context` | Bridge health or compact document context. |
| `vw_read` | `document`, `layers`, `summary`, `query`, `selection`, `sheet_layers`, `viewports`, `viewport_annotations` | Read-only document data with field projection and paging; documentation reads require revision 5 and exact target binding. |
| `vw_catalog` | `capabilities`, `classes`, `symbols`, `parametric_schemas`, `worksheets`, `resources` | Manifest and parameter discovery. |
| `vw_apply` | atomic plan | Canonical atomic document mutation entry. |
| `vw_execute_operations` | atomic plan | The same write core and native transaction as `vw_apply`. |
| `vw_io` | `import`, `export`, `capture` | Advertised native file and capture actions. |
| `vw_view` | `get`, `set`, `fit`, `capture` | Advertised native view actions; fit clears selection by default and fits all objects. |
| `vw_document` | `info`, `save`, `export`, `open` | Advertised document lifecycle actions. |
| `vw_tool_safety` | none | Exact action safety, effects, and retry policy. |

The server publishes this top-level safety metadata for the production tools:

| Tool | Category | Wire action | Read-only | Destructive | Idempotent | Open-world | CAD preflight |
|------|----------|-------------|-----------|-------------|------------|------------|---------------|
| `vw_apply` | `grouped-atomic-write` | `` | `false` | `true` | `true` | `true` | `true` |
| `vw_catalog` | `grouped-catalog` | `` | `true` | `false` | `true` | `true` | `true` |
| `vw_document` | `grouped-native-document` | `` | `false` | `true` | `false` | `true` | `true` |
| `vw_execute_operations` | `document-write` | `` | `false` | `true` | `true` | `true` | `true` |
| `vw_io` | `grouped-native-io` | `` | `false` | `false` | `false` | `true` | `true` |
| `vw_read` | `grouped-read` | `` | `true` | `false` | `true` | `true` | `true` |
| `vw_status` | `grouped-status` | `` | `true` | `false` | `true` | `true` | `true` |
| `vw_tool_safety` | `metadata` | `` | `true` | `false` | `true` | `false` | `false` |
| `vw_view` | `grouped-native-view` | `` | `false` | `false` | `false` | `true` | `true` |

The compatibility profile and the modal Python listener are administrator
diagnostics. They are not fallback paths.

## Reads

`vw_read(action="query")` forwards `criteria`, `layer`, `object_type`, and the
bounded native limit. Use all three filters when they narrow the result. The
cursor is an offset for the returned native collection. Use `fields` to keep
the response compact.

`vw_catalog(action="parametric_schemas", query="Space")` sends `Space` as the
native `plugin_name`. The result includes the universal plug-in name,
descriptor fingerprint, and universal parameter descriptors. Use that schema
before a generic parametric write.

`vw_catalog(action="capabilities")` returns both
`capability_revision` and `capability_fingerprint`. The manifest response must
match the identity reported by ping. A mismatch blocks work until the native
bridge is restarted or upgraded.

## Atomic writes

`vw_apply` and `vw_execute_operations` accept the same arguments:

```json
{
  "operations": [
    {
      "type": "create",
      "operation_id": "living",
      "params": {
        "object_type": "space",
        "points": [[200, 200], [6300, 200], [6300, 4300], [200, 4300]],
        "closed": true,
        "height": 3000,
        "name": "Living / Dining",
        "room_id": "LIVING"
      }
    }
  ],
  "idempotency_key": "floor-plan-2026-08-18-a"
}
```

Supported operation families are `create`, `set_properties`, `transform`,
`reshape`, `update_parametric`, `duplicate`, and `delete`. Geometry is supplied
with explicit `coordinate_units` and normalized to native millimetres. The grouped write uses one native
`apply_operations` transaction. Native apply prevalidates the whole plan, runs one
undo transaction, registers each created object with undo, and returns compact
semantic receipts. The host never retries through another action.

Create only object types present in the manifest. The production bridge can
advertise primitives, walls, text, dimensions, true slabs, true roofs, true
Spaces, hosted doors and windows, symbols, and generic parametric objects.
Availability comes from the loaded binary, not this list.

Revision-5 documentation plans use `create_sheet_layer`, `update_sheet_layer`,
`delete_sheet_layer`, `create_viewport`, `update_viewport`, `delete_viewport`,
`create_viewport_annotation`, `update_viewport_annotation`, and
`delete_viewport_annotation`. They require the exact saved-file,
document-fingerprint, active-document-generation, bridge-session, and dirty
binding from `vw_read(action="document")`. They route as one native
`apply_documentation_operations` transaction and cannot mix with general
operations. See `DOCUMENTATION_WORKFLOW.md` for field schemas, paging, review,
and confirmation tokens.

Dedicated hosted openings use this normalized wire shape inside the atomic
plan:

```json
{
  "op": "create",
  "object_type": "window",
  "plugin_name": "Window",
  "descriptor_fingerprint": "<live schema fingerprint>",
  "x1": 3200.0,
  "y1": 0.0,
  "rotation": 0.0,
  "require_wall_host": true,
  "wall_uuid": "<raw Vectorworks wall UUID>",
  "width": 1200.0,
  "height": 1500.0,
  "sill_height": 900.0,
  "parameter_count": 0,
  "local_ref": "living-window"
}
```

A door has the same shape without `sill_height`. `plugin_name` must be exactly
`Door` or `Window`, and insertion `x`/`y`, width, height, and window sill height
are explicit rather than defaulted. Optional typed parameters use
`parameter_count` plus `parameter_N_name`, `parameter_N_type`, and the matching
typed value field. The host emits this flattened wire form from the public
`parameters` list, but rejects parameters duplicating dedicated width/height or
window elevation semantics. A missing wall UUID, stale schema fingerprint, failed host
verification, or absent `door` or `window` manifest type is a hard failure.

## Safety and retry policy

`vw_tool_safety` includes action-level `retryPolicy`,
`unknownCommitState`, `writesDocument`, `writesFiles`, and confirmation
metadata.

| Grouped action | Effect | Retry policy after send |
|----------------|--------|-------------------------|
| `vw_status.*`, `vw_read.*`, `vw_catalog.*`, `vw_view.get` | Read only | `safe` |
| `vw_apply`, `vw_execute_operations` | Atomic document write | Reuse the same key only for the identical plan after state inspection. |
| `vw_io.import` | Document write | `never_after_send` |
| `vw_io.export`, `vw_io.capture` | File write | `never_after_send` |
| `vw_view.set`, `vw_view.fit` | View-state write | `never_after_send` |
| `vw_view.capture` | File write | `never_after_send` |
| `vw_document.save`, `vw_document.export` | File write | `never_after_send` |
| `vw_document.open` | Destructive document lifecycle | `never_after_send` |

`vw_io` and `vw_document` do not accept `idempotency_key`. The native bridge
does not provide a durable replay ledger for those actions.

Grouped errors distinguish transport state:

- `request_not_sent` means that no native work started. Retry is safe.
- `preflight_failed` means that no native work started. Repair the bridge, then
  retry.
- `capability_unavailable` and `capability_manifest_mismatch` mean that no
  fallback was attempted.
- `unknown_commit_state` means that Vectorworks accepted a non-retryable action
  but the host did not receive a reliable result. Do not retry. Inspect the
  document and the target file through read-only calls.
