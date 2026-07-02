---
name: work
description: Work with Vectorworks through MCP tools for CAD/BIM tasks. Use when the user asks an MCP agent to draw, model, inspect, edit, export, import, screenshot, create walls/doors/windows/slabs/roofs, manage classes/layers, or automate Vectorworks 2024/2025.
---

# Vectorworks Work

Before changing the drawing, confirm the connection:

1. Call `vw_agent_context(profile="production")` when available; otherwise call `vw_preflight_for_cad` or `vw_ping`.
2. If `vw_ping` is unavailable, use `/vectorworks:ping` or `vectorworksctl ping`.
3. Confirm the ping/status payload reports `cad_api_safe=true` and `transport_only=false`. Native bridge status is preferred when it is compiled and smoke-tested; the Python dialog listener is an acceptable fallback only when it is CAD-safe.
4. Prefer the `vw_agent_context` payload for planning because it combines preflight, key capabilities, and compact drawing context in one call.
5. If `vw_agent_context` is unavailable, call `vw_capabilities`, `vw_tool_safety`, and `vw_drawing_summary`. For large projects, start summaries with `include_examples=false` or a low `example_limit`, then use `vw_lookup_objects` for compact refs/details before falling back to raw `vw_get_objects` or complex `vw_find_objects` criteria.

Use the MCP tools deliberately:

- Create basic geometry with `vw_create_object`.
- For repeated creation, prefer `vw_batch_create_objects` over many separate MCP calls. Use the default `atomic=true` when the native bridge reports `batch_create_objects`; phase 2 can atomically mix primitives, true walls, text, and linear dimensions. Use `atomic=false` only when deliberately accepting legacy non-atomic composition.
- For floor plans, use `vw_create_bim_floor_plan` when the target is true wall-based layout work with optional labels/dimensions. Use `vw_plan_schematic_floor_plan` first for dry-run drafting geometry, then `vw_create_schematic_floor_plan` for schematic multi-room layouts. Use `vw_create_schematic_room`, `vw_create_schematic_door`, and `vw_create_schematic_window` for focused 2D drafting edits.
- Use architectural tools deliberately: `vw_create_wall`, `vw_create_text`, and `vw_create_linear_dimension` are native phase-2 production tools; `vw_insert_door`, `vw_insert_window`, `vw_create_slab`, and `vw_create_roof` remain broader Python/legacy paths unless capabilities say otherwise.
- Inspect and find existing objects with `vw_drawing_summary`, `vw_lookup_objects`, `vw_get_objects`, `vw_find_objects`, and `vw_inspect_object`. Prefer `vw_drawing_summary(include_examples=false)` for large-project context, then `vw_lookup_objects` for token-efficient refs and exact-name criteria like `((N='Name'))` for deterministic follow-up edits.
- For property edits, prefer `vw_batch_set_object_properties` when `vw_agent_context` or `vw_capabilities` reports `verified_batch_property_editing=true`. `set_property` is a required native phase-2 production action. Use `uuid:...` refs first, `handle:...` only within the same live session, and `name:...` only when the name is known to be unique.
- Manage organization with `vw_manage_classes`, layers, names, and properties before bulk edits. `manage_classes` is a required native phase-2 production action for class list/create/delete.
- Use `vw_run_script` only for trusted local debugging after the environment was explicitly started with `VW_MCP_ENABLE_RUN_SCRIPT=1`; it still requires `confirm="RUN_TRUSTED_CODE"`.

Safety habits:

- If a tool returns `blocked: true`, stop and fix the listener/bridge status before retrying CAD work.
- If ping reports `native_phase: 0`, missing phase-2 actions such as `set_property` or `manage_classes`, or `transport_only: true`, do not call unsupported CAD handlers; run `vectorworksctl native-next --plan-only --json`.
- Ask before destructive edits such as delete, class-wide changes, overwrites, or exports over existing files.
- Destructive/code-execution/probing tools require explicit confirmation arguments such as `confirm="DELETE_SELECTED"`, `confirm="DELETE_EXACT_NAME"` for exact-name criteria cleanup, `confirm="DELETE_CLASS"`, `confirm="RUN_TRUSTED_CODE"`, or `confirm="PROBE_PLUGIN"`. `vw_run_script` also requires the `VW_MCP_ENABLE_RUN_SCRIPT=1` environment gate.
- If an operation reports unknown commit state, do not retry non-idempotent or destructive tools. Stabilize the connection, then inspect with read-only tools.
- State the assumed units when the user gives dimensions. Default to the document/user context; if unknown, use millimeters for architectural dimensions.
- Verify after changes with object queries, document info, or screenshot/export tools when available.

For tool details, read `references/tool-map.md` from this plugin.
