---
name: work
description: Work with Vectorworks through MCP tools for CAD/BIM tasks. Use when the user asks Claude to draw, model, inspect, edit, export, import, screenshot, create walls/doors/windows/slabs/roofs, manage classes/layers, or automate Vectorworks 2024/2025.
---

# Vectorworks Work

Before changing the drawing, confirm the connection:

1. Call `vw_preflight_for_cad` when available; otherwise call `vw_ping`.
2. If `vw_ping` is unavailable, use `/vectorworks:ping` or the raw listener ping script.
3. Confirm the ping/status payload does not report `cad_api_safe: false` or `transport_only: true`.
4. For unfamiliar or risky commands, call `vw_tool_safety` and prefer read-only tools before write/destructive tools.
5. Get context with `vw_get_document_info` and `vw_get_layers` for non-trivial work.

Use the MCP tools deliberately:

- Create basic geometry with `vw_create_object`.
- Use architectural tools for BIM elements: `vw_create_wall`, `vw_insert_door`, `vw_insert_window`, `vw_create_slab`, `vw_create_roof`.
- Inspect and find existing objects with `vw_get_objects`, `vw_find_objects`, and `vw_inspect_object`.
- Manage organization with `vw_manage_classes`, layers, names, and properties before bulk edits.
- Use `vw_run_script` only for trusted Python that the user would be comfortable running inside the active Vectorworks document.

Safety habits:

- Ask before destructive edits such as delete, class-wide changes, overwrites, or exports over existing files.
- State the assumed units when the user gives dimensions. Default to the document/user context; if unknown, use millimeters for architectural dimensions.
- Verify after changes with object queries, document info, or screenshot/export tools when available.

For tool details, read `references/tool-map.md` from this plugin.
