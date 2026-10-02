# 0.7 → 1.0 tool parity

Every capability of the 0.7 host (47-tool `compat` profile and 9-tool `fast-native` profile) is reachable from the
1.0 default surface. The frozen 0.7 server itself is still available with `VW_MCP_TOOL_PROFILE=legacy`
(`legacy/server_v07.py`).

"Native" = a native bridge action; "script" = a vetted snippet from `vectorworks_mcp/scripts_lib/` run through native
`run_script` (needs the 1.0 bridge; listed under `script_features` in `vw_read(what="capabilities")`, not yet
live-tested in Vectorworks). "rev5" = works on the currently deployed pre-1.0 bridge (capability revision 5).

## fast-native profile (0.7 default)

| 0.7 tool / action | 1.0 | Notes |
|---|---|---|
| `vw_status(action=health)` | `vw_status()` | rev5 ✓; also instances, documents, ui_state, setup problems |
| `vw_status(action=context)` | `vw_status(detail=true)` / `vw_read(what="summary")` | rev5 ✓ |
| `vw_status(action=transaction, idempotency_key)` | `vw_status(idempotency_key=...)` | rev5 ✓ |
| `vw_read(action=document)` | `vw_read(what="document")` | rev5 ✓ |
| `vw_read(action=layers)` | `vw_read(what="layers")` | rev5 ✓ (`get_layers`), v2 `layers` |
| `vw_read(action=summary)` | `vw_read(what="summary", layer, type)` | rev5 ✓ |
| `vw_read(action=query, criteria, layer, object_type, fields, cursor)` | `vw_read(what="query", criteria, layer, type, class, name, fields, cursor)` | rev5 ✓ |
| `vw_read(action=selection)` | `vw_read(what="selection")` | rev5 ✓ |
| `vw_read(action=sheet_layers / viewports / viewport_annotations)` | `vw_read(what="sheets" / "viewports" / "annotations")` | rev5 ✓ (saved documents) |
| `vw_read(action=plan_quality, plan)` | `vw_read(what="plan_quality", plan)` | host-only, lazy import |
| `vw_catalog(capabilities)` | `vw_read(what="capabilities")` | + tool annotations, script features |
| `vw_catalog(classes / symbols / worksheets / resources)` | `vw_read(what="classes" / "symbols" / "worksheets" / "resources")` | rev5 ✓; other resource types (hatches, line types, text/wall/slab styles …) via script |
| `vw_catalog(parametric_schemas, query)` | `vw_read(what="parametric_schema", target)` | rev5 ✓ |
| `vw_apply(operations, idempotency_key, coordinate_units, target_binding)` | `vw_apply(operations, coordinate_units, idempotency_key?, document?)` | grammar unchanged; key optional; binding automatic; `op`/`type`, numeric strings, flat params accepted; door/window descriptor auto-filled |
| `vw_execute_operations` | `vw_apply` | same engine |
| `vw_io(import, dwg)` | `vw_document(action="import", path)` | rev5 ✓ (`import_dwg`); IFC v2; images via script |
| `vw_io(export, pdf/dwg/vwx/image)` | `vw_document(action="export", path, format, range, dpi)` | rev5 ✓; IFC v2; async job + progress on v2 |
| `vw_io(capture)` / `vw_view(capture)` | `vw_capture(...)` | rev5: current view only (old fit mutated selection); v2: fit/objects/layer/sheet/saved view/standard view/render mode, restored |
| `vw_view(get)` | `vw_view()` / `vw_read(what="view")` | rev5 ✓ |
| `vw_view(set, standard_view/projection/render_mode)` | `vw_view(standard_view, projection, render_mode)` | rev5 ✓ (names instead of numbers) |
| `vw_view(fit)` | `vw_view(fit="drawing")` | rev5 ✓ (clears selection, warned); v2 selection/objects/layer, zoom, center |
| `vw_document(info)` | `vw_document(action="info")` | rev5 ✓ |
| `vw_document(save / save-as)` | `vw_document(action="save" / "save_as", path, replace)` | rev5 ✓ |
| `vw_document(open, replace_dirty_confirmation)` | `vw_document(action="open", path, discard_changes="discard")` | rev5 ✓ (readback poll); v2 job |
| `vw_document(export)` | `vw_document(action="export")` | see vw_io |
| `vw_tool_safety` | `vw_read(what="capabilities").tools` + MCP annotations | |

## compat profile (0.7 diagnostic, 47 tools)

| 0.7 tool | 1.0 | Notes |
|---|---|---|
| `vw_capabilities`, `vw_ping`, `vw_bridge_status`, `vw_preflight_for_cad` | `vw_status()` | |
| `vw_agent_context(profile)` | `vw_status(detail=true)` + `vw_read(what="summary"/"layers"/"classes")` | |
| `vw_stop_listener` | `vw_status(disconnect=true)` | closes host sockets; the 1.0 bridge never stops on request |
| `vw_get_document_info` | `vw_read(what="document")` | |
| `vw_get_layers` | `vw_read(what="layers")` | |
| `vw_get_objects`, `vw_find_objects(criteria)`, `vw_lookup_objects(ref, detail)` | `vw_read(what="query", ...)` / `vw_read(what="object", target)` | object detail adds records/bounds via script on v2 |
| `vw_inspect_object(handle, plugin_name)` | `vw_read(what="object", target)` / `vw_read(what="parametric_schema", target)` | no temporary probe objects |
| `vw_drawing_summary` | `vw_read(what="summary")` | |
| `vw_create_object`, `vw_batch_create_objects` | `vw_create(objects=[...])` | one undo step |
| `vw_create_wall` | `vw_create(objects=[{type:"wall", start, end, height, thickness, style}])` | |
| `vw_create_text` | `{type:"text", at, text, size, width, rotation}` | |
| `vw_create_linear_dimension` | `{type:"dimension", start, end, offset}` | |
| `vw_insert_door`, `vw_insert_window` | `{type:"door"/"window", wall:"$id"/uuid, at or offset, width, height, sill}` | real hosted plug-in objects |
| `vw_create_slab`, `vw_create_roof` | `{type:"slab"/"roof", points, ...}` | |
| (space) | `{type:"space", points, name, room_id}` | |
| `vw_symbol(list)` / `vw_symbol(insert)` | `vw_read(what="symbols")` / `{type:"symbol", symbol, at, rotation}` | |
| `vw_create_schematic_room` / `_door` / `_window` | `vw_create(floor_plan={style:"schematic", rooms, doors, windows})` | same 2D geometry as 0.7 |
| `vw_create_schematic_floor_plan` | `vw_create(floor_plan={style:"schematic", ...})` | |
| `vw_plan_schematic_floor_plan` | `vw_create(floor_plan=..., dry_run=true)` | no Vectorworks call |
| `vw_create_bim_floor_plan` | `vw_create(floor_plan={rooms, walls, doors, windows, label_rooms, dimension_rooms, spaces})` | now also places hosted doors/windows on the nearest wall |
| `vw_set_object_property`, `vw_batch_set_object_properties` | `vw_edit(changes=[{op:"set", target(s), properties}])` | name, class, fill/pen colour (hex, rgb, names), fill pattern, line weight, opacity |
| `vw_selection(get)` | `vw_read(what="selection")` | |
| `vw_selection(select, criteria)` / `(clear)` | `vw_edit(changes=[{op:"select", criteria or targets}])` / `{op:"deselect"}` | select by uuid needs v2 |
| `vw_selection(delete / move / duplicate)` | `vw_edit(changes=[{op:"delete"/"move"/"duplicate", target:"selection"}])` | |
| `vw_manage_classes(list/create/delete)` | `vw_read(what="classes")` / `vw_organize(kind="class", action=...)` | class attributes (`set`) need v2 |
| `vw_worksheet(list/read/read_range/write)` | `vw_read(what="worksheets")` / `vw_read(what="worksheet", target, range)` / `vw_organize(kind="worksheet", action="write", cell, value)` | rev5 ✓ |
| `vw_export(pdf/dxf/dwg/image)` | `vw_document(action="export", format)` | no dialogs |
| `vw_import_file(dxf/dwg/images)` | `vw_document(action="import")` | images via script |
| `vw_screenshot` | `vw_capture` | |
| `vw_run_script` | `vw_script(code, language)` | one undo step, rollback on error, `result` returned |
| `vw_tool_safety` | annotations | |

## New in 1.0 (not in 0.7)
Multi-document (`documents`, `document=` on every tool, `vw_document` list/activate/new/close), instance pinning,
async jobs with progress (`vw_status(job_id)`), layers create/set/delete, stories, saved views (list/restore/create/
delete), class attributes, IFC import/export, PDF page ranges, transforms rotate/scale/mirror, move to layer,
group/ungroup, record formats (list/attach/set/detach/read), resource import from another file, composite `room`
objects, unit strings, forgiving inputs, MCP resources (`vw://guide`, `vw://status`, `vw://documents`,
`vw://capabilities`, `vw://capture/{name}`) and prompts.
