# Vectorworks MCP Tool Map

Core health and escape hatch:

- `vw_ping`: confirm the listener and MCP server are connected; check bridge mode and CAD safety before real work.
- `vw_bridge_status`: same listener status payload as `vw_ping`, named for agent preflight checks.
- `vw_preflight_for_cad`: structured JSON go/no-go check before real CAD/API handlers.
- `vw_tool_safety`: structured read/write/destructive metadata for every tool.
- `vw_run_script`: run trusted Python inside Vectorworks.
- `vw_stop_listener`: ask the Vectorworks listener to stop gracefully.

Document context:

- `vw_get_document_info`: active document metadata and object counts.
- `vw_get_layers`: list layers.
- `vw_get_objects`: list objects filtered by layer/type.
- `vw_find_objects`: Vectorworks criteria search, such as `T=WALL`.
- `vw_inspect_object`: discover object/plugin parameters.

Create and edit:

- `vw_create_object`: rect, circle, oval, line, arc, polygon.
- `vw_set_object_property`: name, class, color, line weight, opacity.
- `vw_selection`: get, select, clear, delete, move, or duplicate selected objects.

Architecture:

- `vw_create_wall`: parametric walls.
- `vw_insert_door`: parametric doors.
- `vw_insert_window`: parametric windows.
- `vw_create_slab`: slab from polygon footprint.
- `vw_create_roof`: roof from footprint.

Resources and files:

- `vw_manage_classes`: list/create/delete classes.
- `vw_worksheet`: read/write worksheet cells and ranges.
- `vw_symbol`: list and insert symbols.
- `vw_export`: export PDF, DXF, DWG, or image where supported.
- `vw_import_file`: import DXF, DWG, or image files.
- `vw_screenshot`: capture a viewport screenshot where supported.
