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

- `vw_create_object`: rect, circle, oval, line, arc; polygon is listener-dependent and blocked by native phase 1.
- `vw_create_schematic_room`: rectangular schematic room from native 2D wall rectangles.
- `vw_create_schematic_door`: schematic door leaf and swing arc from native 2D primitives.
- `vw_create_schematic_window`: schematic double-line window marker from native 2D primitives.
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

## Safety Metadata

Agents should call `vw_tool_safety` as a normal planning step before CAD work,
especially before mixed, write, destructive, file, or trusted-code tools.

Rules:

- Call `vw_preflight_for_cad` before tools with `requires_cad_preflight: true`.
- Prefer `readOnlyHint: true` tools for discovery and verification.
- Ask for explicit confirmation before `destructiveHint: true` tools or variants.
- Never automatically retry `idempotentHint: false` operations after timeout,
  protocol failure, or unknown commit state.
- Treat `openWorldHint: true` tools as dependent on the current Vectorworks
  document, selected objects, filesystem, or bridge state.

| Tool | Category | Wire action | Read-only | Destructive | Idempotent | Open-world | CAD preflight |
|------|----------|-------------|-----------|-------------|------------|------------|---------------|
| `vw_bridge_status` | `health` | `ping` | `true` | `false` | `true` | `true` | `false` |
| `vw_create_object` | `document-write` | `create_object` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_roof` | `document-write` | `create_roof` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_slab` | `document-write` | `create_slab` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_schematic_door` | `schematic-floor-plan` | `create_object` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_schematic_room` | `schematic-floor-plan` | `create_object` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_schematic_window` | `schematic-floor-plan` | `create_object` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_wall` | `document-write` | `create_wall` | `false` | `false` | `false` | `true` | `true` |
| `vw_export` | `file-write` | `export` | `false` | `false` | `false` | `true` | `true` |
| `vw_find_objects` | `document-read` | `find_objects` | `true` | `false` | `true` | `true` | `true` |
| `vw_get_document_info` | `document-read` | `get_document_info` | `true` | `false` | `true` | `true` | `true` |
| `vw_get_layers` | `document-read` | `get_layers` | `true` | `false` | `true` | `true` | `true` |
| `vw_get_objects` | `document-read` | `get_objects` | `true` | `false` | `true` | `true` | `true` |
| `vw_import_file` | `document-write` | `import_file` | `false` | `false` | `false` | `true` | `true` |
| `vw_insert_door` | `document-write` | `insert_door` | `false` | `false` | `false` | `true` | `true` |
| `vw_insert_window` | `document-write` | `insert_window` | `false` | `false` | `false` | `true` | `true` |
| `vw_inspect_object` | `document-read` | `inspect_object` | `true` | `false` | `true` | `true` | `true` |
| `vw_manage_classes` | `mixed-destructive` | `manage_classes` | `false` | `true` | `false` | `true` | `true` |
| `vw_ping` | `health` | `ping` | `true` | `false` | `true` | `true` | `false` |
| `vw_preflight_for_cad` | `health` | `ping` | `true` | `false` | `true` | `true` | `false` |
| `vw_run_script` | `trusted-code` | `run_script` | `false` | `true` | `false` | `true` | `true` |
| `vw_screenshot` | `document-export` | `screenshot` | `false` | `false` | `false` | `true` | `true` |
| `vw_selection` | `mixed-destructive` | `selection` | `false` | `true` | `false` | `true` | `true` |
| `vw_set_object_property` | `document-write` | `set_property` | `false` | `false` | `false` | `true` | `true` |
| `vw_stop_listener` | `listener-control` | `stop` | `false` | `false` | `false` | `true` | `false` |
| `vw_symbol` | `mixed-document-write` | `symbol` | `false` | `false` | `false` | `true` | `true` |
| `vw_tool_safety` | `metadata` | `` | `true` | `false` | `true` | `false` | `false` |
| `vw_worksheet` | `mixed-document-write` | `worksheet` | `false` | `false` | `false` | `true` | `true` |

## Mixed Tool Actions

Tool-level MCP annotations stay conservative for mixed tools. Use these action
rows to choose the least risky variant before calling the tool.

| Tool action | Read-only | Destructive | Idempotent | Writes document | Writes selection | Writes files | Confirmation |
|-------------|-----------|-------------|------------|-----------------|------------------|--------------|--------------|
| `vw_manage_classes.create` | `false` | `false` | `false` | `true` | `false` | `false` | `false` |
| `vw_manage_classes.delete` | `false` | `true` | `false` | `true` | `false` | `false` | `true` |
| `vw_manage_classes.list` | `true` | `false` | `true` | `false` | `false` | `false` | `false` |
| `vw_selection.clear` | `false` | `false` | `false` | `false` | `true` | `false` | `false` |
| `vw_selection.delete` | `false` | `true` | `false` | `true` | `true` | `false` | `true` |
| `vw_selection.duplicate` | `false` | `false` | `false` | `true` | `false` | `false` | `false` |
| `vw_selection.get` | `true` | `false` | `true` | `false` | `false` | `false` | `false` |
| `vw_selection.move` | `false` | `false` | `false` | `true` | `false` | `false` | `false` |
| `vw_selection.select` | `false` | `false` | `false` | `false` | `true` | `false` | `false` |
| `vw_symbol.insert` | `false` | `false` | `false` | `true` | `false` | `false` | `false` |
| `vw_symbol.list` | `true` | `false` | `true` | `false` | `false` | `false` | `false` |
| `vw_worksheet.list` | `true` | `false` | `true` | `false` | `false` | `false` | `false` |
| `vw_worksheet.read` | `true` | `false` | `true` | `false` | `false` | `false` | `false` |
| `vw_worksheet.read_range` | `true` | `false` | `true` | `false` | `false` | `false` | `false` |
| `vw_worksheet.write` | `false` | `false` | `false` | `true` | `false` | `false` | `false` |
