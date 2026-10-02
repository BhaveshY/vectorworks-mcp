# Native Bridge Handler Matrix

This matrix is the implementation map for the native Vectorworks SDK bridge.
Every action in `vw_listener.py` must appear here before native work is called
complete.

Legend:

- Safety: `read`, `write`, or `destructive`.
- Context: all real CAD handlers must run on the Vectorworks SDK main/plugin
  event context, never directly from a socket worker thread.
- Native phase: `0` is transport, `1` is minimum smoke-test parity, `2+` are
  broader handler groups.

| Action | Python handler | Safety | Context | Native phase | Smoke test |
|--------|----------------|--------|---------|--------------|------------|
| `ping` | transport status | read | transport-only is allowed | 0 | Phase-0 scaffold returns `native_bridge: true`, `cad_api_safe: false`, `transport_only: true`, version, and handler count; production phase-1 returns `cad_api_safe: true` |
| `stop` | `handle_stop` | read | transport thread | 0 / v2 | v2: closes only the calling connection (`{connection_closed:true, bridge_running:true}`); the bridge keeps running |
| `get_document_info` | `handle_get_document_info` | read | main/plugin event context | 1/4 | Returns filename, saved path, layer/object counts, stable document fingerprint, active-document generation, bridge session ID, dirty state, and active layer identity. |
| `get_layers` | `handle_get_layers` | read | main/plugin event context | 1/4 | Lists layer name, UUID, kind, and visibility repeatedly without freezing Vectorworks. |
| `get_sheet_layers` | no Python fallback | read | main/plugin event context | 4, revision 5 | Bound/paged native sheet-layer metadata and exact viewport counts. |
| `get_viewports` | no Python fallback | read | main/plugin event context | 4, revision 5 | Bound/paged viewports on one exact sheet UUID, including scale, crop, view/render settings, placement, and complete source layer/class visibility. |
| `get_viewport_annotations` | no Python fallback | read | main/plugin event context | 4, revision 5 | Bound/paged native annotation-group children for one exact sheet/viewport UUID pair. |
| `get_objects` | `handle_get_objects` | read | main/plugin event context | 1 | Lists objects with layer/type filters; native text objects include live UTF-8 `text` content, including empty strings and line breaks. `object_read_features` advertises `text_content`. |
| `query_objects` | no Python fallback | read | main/plugin event context | 4 | Native offset pages for object, criteria, and selection reads; projects requested fields before serialization, returns document binding and explicit continuation, and validates an optional expected binding. Advertised as `paged_object_reads`. |
| `transaction_status` | no Python fallback | read | main/plugin event context | 4 | Looks up a general apply receipt in the current document/session cache. A retained receipt means historical commit; missing means unknown, never permission to retry. |
| `selection` | `handle_selection` | mixed/destructive | main/plugin event context | 1 | `get` and `clear` work; `delete` requires explicit destructive test |
| `create_object` | `handle_create_object` | write | main/plugin event context | 1/4 | Creates phase-1 primitives plus phase-4 open/closed polygons; returns UUID, bounds, and polygon vertex/closure metadata |
| `batch_create_objects` | `handle_batch_create_objects` | write | main/plugin event context | 1/4 | Atomically creates mixed supported objects, including phase-4 polygons, in one undo event |
| `create_wall` | `handle_create_wall` | write | main/plugin event context | 2 | Creates and deletes a temporary true wall object; can set width and fixed corner height |
| `create_text` | `handle_create_text` | write | main/plugin event context | 2 | Creates and deletes a temporary text label |
| `create_linear_dimension` | `handle_create_linear_dimension` | write | main/plugin event context | 2 | Creates and deletes a temporary linear dimension |
| `set_property` | `handle_set_property` | write | main/plugin event context | 2 | Changes name/class/fill color/pen color/fill pattern/line weight/opacity on a test object with host readback verification |
| `manage_classes` | `handle_manage_classes` | mixed/destructive | main/plugin event context | 2 | Lists, creates, and deletes a temporary class; delete has separate destructive confirmation |
| `find_objects` | `handle_find_objects` | read | main/plugin event context | 3 | Criteria search returns known test object |
| `drawing_summary` | `handle_drawing_summary` | read | main/plugin event context | 3 | Returns compact counts by type/layer/class plus bounded examples without dumping every object |
| `apply_operations` | no Python fallback | idempotent write | main/plugin event context | 4 | Uses one `NativeTransaction` for create, property, transform, wall/line endpoint reshape, schema-verified parametric update, duplicate, and delete operations. Supports local, handle, UUID, and exact-name refs. Verifies semantic receipts, exact cleanup, undo commit, and idempotency-key reuse across first save/Save As by stable layer identity. |
| `apply_documentation_operations` | no Python fallback | idempotent/destructive write | main/plugin event context | 4, revision 5 | One bound native transaction for typed create/update/delete of sheet layers, viewports, and viewport annotations. Requires the exact saved path, document fingerprint, active-document generation, bridge session ID, and dirty state; validates parent-child ownership and semantic readback; restores the starting active layer. |
| `get_view` | native view state | read | main/plugin event context | 4 / v2 | v2: zoom, centre, view matrix, active layer + kind, screen/drawing-area pixels, dpi, names for the enums. |
| `set_view` | native view state | view write | main/plugin event context | 4 / v2 | v2: fit to drawing/selection/objects/layer from object bounds (`SetViewCenter` + `SetZoomFactorN`, no `DoMenuName`, no select-all), zoom/centre/layer/saved view, returns `{previous, current}`. |
| `worksheet` | `handle_worksheet` | mixed/write | main/plugin event context | 3 | Lists worksheets and reads/writes a temporary cell |
| `symbol` | `handle_symbol` | mixed/write | main/plugin event context | 3 | Lists symbols and inserts a known symbol in a test document |
| `export` | `handle_export` | write-file | compatibility only | deferred | Disabled. Production uses the native non-modal `export_image`, `export_pdf`, `export_vectorworks_document`, or `export_dwg` action. |
| `import_file` | `handle_import_file` | document write | compatibility only | deferred | Disabled for production. The native `import_dwg` action uses silent `IImportExportDWG` and returns a document mutation receipt. |
| `screenshot` | `handle_screenshot` | write-file | compatibility only | deferred | Disabled. Production uses native `capture_view` (v2: temporary view/fit/render mode, crop to fit, exact view + selection restore, image size readback). |
| `run_script` | in-process `IPythonScriptEngine` / `IVectorScriptEngine` | destructive/open-ended | main/plugin event context | v2 | One undo step, rollback on failure (`rolled_back`, `rollback_verified`), stdout/stderr capture, `result` JSON, VS error dialogs suppressed; disabled by `VW_MCP_ALLOW_SCRIPT=0`. |
| `insert_door` | `handle_insert_door` | write | compatibility only | deferred | Disabled. Production uses `create_object` kind `door`, a runtime schema fingerprint, an exact wall UUID, and semantic host readback. |
| `insert_window` | `handle_insert_window` | write | compatibility only | deferred | Disabled. Production uses `create_object` kind `window`, a runtime schema fingerprint, an exact wall UUID, and semantic host readback. |
| `create_slab` | `handle_create_slab` | write | compatibility only | deferred | Disabled. Production uses `create_object` kind `slab`, which calls `ISDK::CreateSlab` and verifies `kSlabNode`. |
| `create_roof` | `handle_create_roof` | write | compatibility only | deferred | Disabled. Production uses `create_object` kind `roof`, which calls `ISDK::CreateRoof` and verifies `kRoofContainerNode`. No extrusion substitute exists. |
| `inspect_object` | `handle_inspect_object` | read | compatibility only | deferred | Existing-object inspection is read-only. Temporary plug-in probing is disabled. Production schema discovery uses `describe_parametric_schema`. |

## Protocol v2 additions (capability revision 6)

| Action | Safety | Context | Notes |
|--------|--------|---------|-------|
| `job_status` | read | transport thread | Job record for async/converted requests (15 min / 64 jobs). |
| `cancel_job` | write (queue only) | transport thread | Cancels queued jobs; running handlers are not interruptible. |
| `list_documents` | read | main (no document needed) | Stable per-document fingerprints, active flag, dirty (2024), untitled. |
| `activate_document` | view write | main | `SwitchToOpenFile` by fingerprint/path/name. |
| `new_document` | document write | main (no document needed) | Opens a blank template (no SDK "new" call exists); reports `untitled`. |
| `close_document` | destructive | main | Never prompts: refuses dirty docs unless `discard_changes:"discard"`. |
| `open_document` | document write | main (no document needed) | Now synchronous + job conversion (was deferred fire-and-forget). |
| `capture_view` | read (restores state) | main | See PROTOCOL.md 6.2. |
| `saved_views` | mixed | main | list/restore native; create/delete via in-process `vs.SaveSheet` / `vs.VDelete`. |
| `layers` | mixed/destructive | main | list/create/set/delete with visibility, scale, elevation, opacity, story, counts. |
| `stories` | mixed | main | list/create/set via ISDK story calls. |
| `manage_classes set` / `list detailed` | write / read | main | visibility, active, colours, line weight, use-at-creation. |
| `export_ifc` / `import_ifc` | write-file / document write | main | Silent `IIFCSupport` (`bShowUI=false`). |
| `export_pdf` `range`/`sheet`/`dpi` | write-file | main | current_view, all_pages, or one sheet layer. |
| `resources` / `symbol list` / `worksheet list` paging | read | main | offset/limit/cursor + `next_cursor`. |
| `group`, `records` | — | host | Not native in 1.0; hosts use `run_script` snippets. |

All main-thread actions run behind the UI gate (`idle` only, `no_document`
for document-less actions), accept `async`, `busy_wait_ms`,
`expect_document_fingerprint` + `activate_document`, and honour `timeout_ms`
(`timeout_not_started` / `still_running` + job).

## Mixed Action Safety

Native bridge behavior must match host `vw_tool_safety` variant metadata for
mixed handlers. Read-only variants are retry-safe after transport loss; write or
destructive variants must not be retried automatically and should report an
unknown commit state if the response is lost after the request was sent.

| Variant | Safety | Retry policy |
|---------|--------|--------------|
| `selection.get` | read | retry-safe |
| `selection.select` | selection-write | no automatic retry |
| `selection.clear` | selection-write | no automatic retry |
| `selection.delete` | destructive document write | no automatic retry; unknown commit state on response loss; current-selection delete requires `DELETE_SELECTED`, exact-name criteria delete requires `DELETE_EXACT_NAME` |
| `selection.move` | document write | no automatic retry; unknown commit state on response loss |
| `selection.duplicate` | document write | no automatic retry; unknown commit state on response loss |
| `manage_classes.list` | read | retry-safe |
| `manage_classes.create` | document write | no automatic retry; unknown commit state on response loss |
| `manage_classes.delete` | destructive document write | no automatic retry; unknown commit state on response loss |
| `worksheet.list` | read | retry-safe |
| `worksheet.read` | read | retry-safe |
| `worksheet.read_range` | read | retry-safe |
| `worksheet.write` | document write | no automatic retry; unknown commit state on response loss |
| `symbol.list` | read | retry-safe |
| `symbol.insert` | document write | no automatic retry; unknown commit state on response loss |

## Native Acceptance Rule

For each row, native implementation is only accepted after:

- The handler is implemented in the native bridge or explicitly marked deferred.
- Socket work and JSON framing remain off the Vectorworks API path.
- The CAD/API portion is marshaled to the Vectorworks main/plugin event context.
- A smoke test is recorded in `ACCEPTANCE.md` or a future automated Vectorworks
  smoke-test script.
