# Vectorworks MCP Tool Map

Core health and escape hatch:

- `vw_ping`: confirm the listener and MCP server are connected; check bridge mode and CAD safety before real work.
- `vw_bridge_status`: same listener status payload as `vw_ping`, named for agent preflight checks.
- `vw_preflight_for_cad`: structured JSON go/no-go check before real CAD/API handlers.
- `vw_agent_context`: one-call compact Codex planning snapshot with preflight, key capabilities, and bounded drawing summary.
- `vw_capabilities`: bridge capability report plus current native/tool support.
- `vw_tool_safety`: structured read/write/destructive metadata for every tool.
- `vw_run_script`: run trusted Python inside Vectorworks; requires `confirm="RUN_TRUSTED_CODE"`.
- `vw_stop_listener`: ask the Vectorworks listener to stop gracefully.

Document context:

- `vw_get_document_info`: active document metadata and object counts.
- `vw_get_layers`: list layers.
- `vw_get_objects`: list objects filtered by layer/type.
- `vw_drawing_summary`: bounded document/layer/object inventory for planning and verification. Use `include_examples=false` or a small `example_limit` for fast, token-efficient large-project context.
- `vw_find_objects`: Vectorworks criteria search, such as `T=WALL`. On the native bridge, simple `ALL`, `T=...`, `C=...`, and exact-name `((N='Name'))` criteria are resolved through bounded `get_objects` when the dedicated listener search handler is unavailable.
- `vw_inspect_object`: discover object/plugin parameters; plugin probing creates a temporary object and requires `confirm="PROBE_PLUGIN"`.

Create and edit:

- `vw_create_object`: rect, circle, oval, line, arc; polygon is listener-dependent and blocked by the native bridge.
- `vw_batch_create_objects`: create many native objects in one MCP call. Phase 1 supports primitives; phase 2 also supports walls, text, and linear dimensions. `atomic=true` requires the native `batch_create_objects` bridge action; `atomic=false` uses legacy non-atomic `create_object` composition.
- `vw_plan_schematic_floor_plan`: dry-run a multi-room schematic floor plan and return the primitives.
- `vw_create_schematic_floor_plan`: create a multi-room schematic floor plan from rooms, walls, doors, and windows.
- `vw_create_bim_floor_plan`: create true wall objects plus optional room text labels and linear dimensions from rooms/walls.
- `vw_create_schematic_room`: rectangular schematic room from native 2D wall rectangles.
- `vw_create_schematic_door`: schematic door leaf and swing arc from native 2D primitives.
- `vw_create_schematic_window`: schematic double-line window marker from native 2D primitives.
- `vw_set_object_property`: name, class, color, line weight, opacity.
- `vw_selection`: get, select, clear, delete, move, or duplicate selected objects; selected-object delete requires `confirm="DELETE_SELECTED"` and exact-name criteria delete requires `confirm="DELETE_EXACT_NAME"`.

Architecture:

- `vw_create_wall`: native true wall objects.
- `vw_create_text`: native text annotations.
- `vw_create_linear_dimension`: native linear dimensions.
- `vw_insert_door`: parametric doors through the legacy/Python path; native wall-hosted insertion is deferred pending plugin inspection.
- `vw_insert_window`: parametric windows through the legacy/Python path; native wall-hosted insertion is deferred pending plugin inspection.
- `vw_create_slab`: slab from polygon footprint.
- `vw_create_roof`: roof from footprint.

Resources and files:

- `vw_manage_classes`: list/create/delete classes; delete requires `confirm="DELETE_CLASS"`.
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
| `vw_agent_context` | `metadata` | `` | `true` | `false` | `true` | `true` | `false` |
| `vw_batch_create_objects` | `document-write` | `batch_create_objects` | `false` | `false` | `false` | `true` | `true` |
| `vw_bridge_status` | `health` | `ping` | `true` | `false` | `true` | `true` | `false` |
| `vw_capabilities` | `metadata` | `ping` | `true` | `false` | `true` | `true` | `false` |
| `vw_create_bim_floor_plan` | `bim-floor-plan` | `` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_linear_dimension` | `document-write` | `create_linear_dimension` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_object` | `document-write` | `create_object` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_roof` | `document-write` | `create_roof` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_slab` | `document-write` | `create_slab` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_schematic_door` | `schematic-floor-plan` | `` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_schematic_floor_plan` | `schematic-floor-plan` | `` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_schematic_room` | `schematic-floor-plan` | `` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_schematic_window` | `schematic-floor-plan` | `` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_text` | `document-write` | `create_text` | `false` | `false` | `false` | `true` | `true` |
| `vw_create_wall` | `document-write` | `create_wall` | `false` | `false` | `false` | `true` | `true` |
| `vw_drawing_summary` | `document-read` | `` | `true` | `false` | `true` | `true` | `true` |
| `vw_export` | `file-write` | `export` | `false` | `false` | `false` | `true` | `true` |
| `vw_find_objects` | `document-read` | `find_objects` | `true` | `false` | `true` | `true` | `true` |
| `vw_get_document_info` | `document-read` | `get_document_info` | `true` | `false` | `true` | `true` | `true` |
| `vw_get_layers` | `document-read` | `get_layers` | `true` | `false` | `true` | `true` | `true` |
| `vw_get_objects` | `document-read` | `get_objects` | `true` | `false` | `true` | `true` | `true` |
| `vw_import_file` | `document-write` | `import_file` | `false` | `false` | `false` | `true` | `true` |
| `vw_insert_door` | `document-write` | `insert_door` | `false` | `false` | `false` | `true` | `true` |
| `vw_insert_window` | `document-write` | `insert_window` | `false` | `false` | `false` | `true` | `true` |
| `vw_inspect_object` | `document-write` | `inspect_object` | `false` | `false` | `false` | `true` | `true` |
| `vw_manage_classes` | `mixed-destructive` | `manage_classes` | `false` | `true` | `false` | `true` | `true` |
| `vw_ping` | `health` | `ping` | `true` | `false` | `true` | `true` | `false` |
| `vw_plan_schematic_floor_plan` | `schematic-floor-plan` | `` | `true` | `false` | `true` | `false` | `false` |
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
For `vw_selection.delete`, current-selection delete requires
`confirm="DELETE_SELECTED"`; criteria delete is restricted to exact object-name
criteria such as `((N='Fixture'))` and requires `confirm="DELETE_EXACT_NAME"`.

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
