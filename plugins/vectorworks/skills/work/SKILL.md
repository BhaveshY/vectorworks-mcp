---
name: work
description: Work with Vectorworks through MCP tools for CAD/BIM tasks. Use when the user asks an MCP agent to draw, model, inspect, edit, export, import, screenshot, create walls/doors/windows/slabs/roofs, manage classes/layers, or automate Vectorworks 2024/2025.
---

# Vectorworks Work

## Choose the shortest correct workflow

The `fast-native` profile is mandatory for normal agent work. It exposes only
the compact native production surface and keeps Vectorworks non-modal so the
user can continue working manually between agent operations. If a requested
operation is not reported by the native bridge, stop and report the unsupported
capability; do not switch profiles, start a modal listener, decompose the
request into a different drawing, or route through a legacy handler.

- For a self-contained create request whose target and geometry are fully
  specified, call `vw_execute_operations` immediately. Do not front-load
  `vw_agent_context`, `vw_ping`, `vw_capabilities`, `vw_tool_safety`, or a
  drawing summary merely to create known geometry. The tool performs its own
  internal CAD preflight and returns `blocked: true` without writing when the
  bridge is unsafe or the action/variant is unsupported.
- Use focused context only when the operation depends on existing document
  state: locating or editing an object, choosing a layer/class, avoiding
  collisions, deriving geometry, or planning a multi-step change. Start with
  the smallest useful call (`vw_lookup_objects`, `vw_get_document_info`, or
  `vw_drawing_summary(include_examples=false)`). Use
  `vw_agent_context(profile="production")` for genuinely broad planning, not as
  a mandatory preamble to every write.
- Use explicit `vw_ping`, `vw_preflight_for_cad`, or `/vectorworks:ping` for
  diagnosis, reconnects, or when the user asks for a connection check. Confirm
  `cad_api_safe=true` and `transport_only=false` before resuming after a
  connectivity or capability failure.
- Trust a successful, self-verifying tool response. A returned handle/UUID,
  exact created count, atomic batch result, or verified property readback is
  sufficient unless the user asks for visual confirmation or the result is
  ambiguous. Do not automatically add a screenshot, full drawing summary, or
  redundant object query after every successful write.

The `compat` profile is an administrator-only diagnostic surface. It is not a
fallback workflow for agent drawing work and must never be selected
automatically. Starting the separately authorized Python dialog is also an
administrator diagnostic action and blocks manual Vectorworks UI use. A
fast-native capability failure remains a failure until the native bridge is
upgraded or the request changes explicitly.

Use the MCP tools deliberately:

- Send a complete create-and-property-edit plan in one
  `vw_execute_operations` call. Supply
  a stable caller-generated `idempotency_key` and canonical
  operations shaped as `{"type":"create","operation_id":"room","params":
  {"object_type":"rect",...}}` or `{"type":"set_properties","params":
  {"edits":[{"ref":"$room","properties":{"name":"Room 101"}}]}}`. Existing
  targets can use `uuid:...`, `name:...`, or `handle:...` refs; `$<operation_id>`
  targets an object created earlier in the same transaction. Reuse the key
  only for the identical plan. The host validates the entire plan before
  writing, requires native phase-4 `apply_operations`, and returns a compact
  self-verifying result. Missing phase-4 support is a hard upgrade/restart
  failure; it never routes to a legacy, decomposed, batch, or modal fallback.
  The contract accepts `create` and `set_properties`; do not invent delete or
  other operation types.
- Polygon and polyline creation require the native bridge to report those
  object types (phase 4). If it does not, stop with the capability error. Do not
  substitute independent lines or retry through compatibility mode.
- Express a fully specified floor-plan create request as one explicit
  `vw_execute_operations` plan using supported wall, text, and dimension
  operation types. Do not call hidden schematic/legacy helpers or derive a
  substitute representation after a native capability failure.
- Walls, text, and linear dimensions are supported create-operation types on an
  appropriate native bridge. Doors/windows, slabs/roofs, worksheets, symbols,
  import/export, screenshots, inspection, and trusted Python are outside the
  mandatory fast-native work surface; do not call or automatically enable them.
- Inspect existing objects with `vw_drawing_summary` and `vw_lookup_objects`.
  Prefer `vw_drawing_summary(include_examples=false)` for large-project context,
  then `vw_lookup_objects` for token-efficient refs and exact-name criteria like
  `((N='Name'))` for deterministic follow-up edits.
- Manage classes through `vw_manage_classes`; use `vw_selection` only for its
  native-supported variants. If the required edit is absent from the
  fast-native surface, report it as unsupported.

Safety habits:

- If a tool returns `blocked: true`, stop and fix the listener/bridge status before retrying CAD work.
- If ping reports `native_phase < 4`, missing `apply_operations`, missing
  focused actions such as `set_property` or `manage_classes`, or
  `transport_only: true`, do not call unsupported CAD handlers; run
  `vectorworksctl native-next --plan-only --json`.
- Ask before destructive edits such as delete, class-wide changes, overwrites, or exports over existing files.
- Destructive fast-native variants require explicit confirmation arguments such
  as `confirm="DELETE_SELECTED"`, `confirm="DELETE_EXACT_NAME"` for exact-name
  cleanup, or `confirm="DELETE_CLASS"`.
- If an operation reports unknown commit state, do not retry non-idempotent or destructive tools. Stabilize the connection, then inspect with read-only tools.
- State the assumed units when the user gives dimensions. Default to the document/user context; if unknown, use millimeters for architectural dimensions.
- Verify after changes only when the response is not self-verifying, the edit
  depends on existing state, or the user requests verification. Prefer a
  focused object query over a screenshot or full-document scan.

For tool details, read `references/tool-map.md` from this plugin.
