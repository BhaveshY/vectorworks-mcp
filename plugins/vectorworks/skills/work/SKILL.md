---
name: work
description: Work with Vectorworks through MCP tools for CAD/BIM tasks. Use when the user asks Claude to draw, model, inspect, edit, export, import, screenshot, create walls/doors/windows/slabs/roofs, manage classes/layers, or automate Vectorworks 2024/2025.
---

# Vectorworks Work

Before changing the drawing, confirm the connection:

1. Call `vw_preflight_for_cad` when available; otherwise call `vw_ping`.
2. If `vw_ping` is unavailable, use `/vectorworks:ping` or `vectorworksctl ping`.
3. Confirm the ping/status payload reports `cad_api_safe=true` and `transport_only=false`. Native bridge status is preferred when it is compiled and smoke-tested; the Python dialog listener is an acceptable fallback only when it is CAD-safe.
4. Call `vw_capabilities` and `vw_tool_safety` as normal planning steps and prefer read-only tools before write/destructive tools.
5. Get context with `vw_drawing_summary`; fall back to `vw_get_document_info`, `vw_get_layers`, and `vw_get_objects` for focused reads.

Use the MCP tools deliberately:

- Create basic geometry with `vw_create_object`.
- For repeated primitive creation, prefer `vw_batch_create_objects` over many separate MCP calls. Use the default `atomic=true` when the native bridge reports `batch_create_objects`; use `atomic=false` only when deliberately accepting legacy non-atomic composition.
- For native floor-plan drafting, use `vw_plan_schematic_floor_plan` first for dry-run geometry, then `vw_create_schematic_floor_plan` for multi-room layouts. Use `vw_create_schematic_room`, `vw_create_schematic_door`, and `vw_create_schematic_window` for focused edits. These tools create 2D schematic drafting geometry, not BIM objects, and their atomic creation path requires the native bridge.
- Use architectural tools for BIM elements: `vw_create_wall`, `vw_insert_door`, `vw_insert_window`, `vw_create_slab`, `vw_create_roof`.
- Inspect and find existing objects with `vw_get_objects`, `vw_find_objects`, and `vw_inspect_object`.
- Manage organization with `vw_manage_classes`, layers, names, and properties before bulk edits.
- Use `vw_run_script` only for trusted Python that the user would be comfortable running inside the active Vectorworks document; it requires `confirm="RUN_TRUSTED_CODE"`.

Safety habits:

- If a tool returns `blocked: true`, stop and fix the listener/bridge status before retrying CAD work.
- If ping reports `native_phase: 0` or `transport_only: true`, do not call CAD handlers; run `vectorworksctl native-next --plan-only --json`.
- Ask before destructive edits such as delete, class-wide changes, overwrites, or exports over existing files.
- Destructive/code-execution/probing tools require explicit confirmation arguments such as `confirm="DELETE_SELECTED"`, `confirm="DELETE_EXACT_NAME"` for exact-name criteria cleanup, `confirm="DELETE_CLASS"`, `confirm="RUN_TRUSTED_CODE"`, or `confirm="PROBE_PLUGIN"`.
- If an operation reports unknown commit state, do not retry non-idempotent or destructive tools. Stabilize the connection, then inspect with read-only tools.
- State the assumed units when the user gives dimensions. Default to the document/user context; if unknown, use millimeters for architectural dimensions.
- Verify after changes with object queries, document info, or screenshot/export tools when available.

For tool details, read `references/tool-map.md` from this plugin.
