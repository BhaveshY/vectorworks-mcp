# Vectorworks MCP 1.0 tool map

The default tool profile has exactly these 10 tools (`docs/V1_CONTRACT.md`
section 2.3). `scripts/check-skill-tool-names.py --live` checks this file and
SKILL.md against the running server's `tools/list` (tool names and argument
names). The tool's input schema is always authoritative.

Every result uses one envelope. Success: `{"ok": true, ...}`. Failure:
`{"ok": false, "error": {"code", "message", "retryable", "next_step",
"commit_state"?, "field"?, "example"?, "details"?}}`; `isError` is set exactly
when `ok` is false. Every document-touching tool accepts `document` (file
name, path or fingerprint). Write tools accept an optional `idempotency_key`
(derived automatically when omitted) and `dry_run`.

## vw_status (read-only, never fails)

| Argument | Meaning |
|---|---|
| (none) | instances, open documents, active document, UI state (idle / modal / tracking / loading / no_document), setup problems with their fix, versions |
| `job_id` | progress / result of a long operation |
| `idempotency_key` | whether a write with that key was applied |
| `instance` | pin one Vectorworks instance (pid or port) |
| `detail` | more diagnostics |
| `disconnect` | close the host's sockets (fresh connection on the next call) |

## vw_read (read-only)

Arguments: `what` (required), `type`, `layer`, `class`, `name`, `criteria`,
`query` / `search` (name contains), `target` (`sheet` / `viewport` aliases), `resource_type`, `fields`, `range`,
`limit`, `cursor`, `detail`, `plan`, `document`.

| `what` | Returns / main arguments |
|---|---|
| `document` | active document info |
| `documents` | open documents (active, modified, fingerprint) |
| `layers` | design + sheet layers with visibility, scale, elevation, story, object count |
| `classes` | classes with visibility (`query` filters names) |
| `summary` | whole-drawing counts by type / layer / class (`type`, `layer`) - use for "how many" |
| `query` | objects; filter `type`, `layer`, `class`, `criteria` (VW criteria); `search` / `query` / `name` = name, text or room label contains (case-insensitive); spaces carry `room_name` (displayed label) next to the object `name`; `fields`; paged by `limit` + `cursor` |
| `object` | one object, compact (<= 1.5 KB): layer, class, attributes, bounds + per-type geometry (wall start/end/length/thickness/height/style/inserts; line endpoints; rect/oval origin/size; polygon points (first 50); text content/font/size; door/window width/height/sill/wall/at; space room_name/room_id/area/height; extrude height/base; viewport scale/view); `detail=true` adds every record (parametric parameters) |
| `selection` | selected objects (`fields`) |
| `view` | current view, zoom, render mode, active layer |
| `saved_views` | saved views |
| `sheets` / `viewports` / `annotations` | sheet layers / viewports of a sheet (`target` or `sheet` = sheet name or uuid: scale, view, render_mode, layers, hidden_classes, centre, size, crop) / annotations of a viewport (`target` = viewport name or uuid) |
| `symbols` / `resources` | symbol definitions / resources (`type` or `resource_type`: hatch, line_type, text_style, gradient, image_fill, tile, wall_style, slab_style, roof_style, material, symbols, ...; each item has `source` document or library); `search` filters names (without `type`: across all kinds, items carry `type`) |
| `worksheets` / `worksheet` | worksheet list / cells of one worksheet (`target`, `range` like `"A1:D20"`) |
| `records` | record formats and fields |
| `parametric_schema` | compact: ~30 most useful parameters `{id, name, type, value}` + `friendly_names` (`target` = "Door", "Window", "Space", "Stair", ... or a placed object's uuid); `search` filters ids/names (friendly names too: "sill" finds Elevation); `detail=true` = every parameter |
| `capabilities` | what the connected bridge supports |
| `plan_quality` | plan checks (overlaps, gaps, unhosted openings); optional `plan` |

## vw_capture (read-only; the user's view is restored)

`view` = `drawing` (default, fit all) / `current` / `selection` / `objects`
(+ `objects` = uuids) / `layer` (+ `layer`) / `sheet` (+ `sheet`) /
`saved_view` (+ `saved_view`); optional `standard_view` (top_plan, top, front,
back, left, right, bottom, iso, right_iso, left_iso, right_rear_iso,
left_rear_iso), `render_mode` (wireframe, opengl, shaded, hidden_line,
dashed_hidden_line, final_shaded, sketch, fast_renderworks, final_renderworks),
`format` (png/jpg), `margin_pct`, `document`. Returns an inline image (long
edge <= 1568 px) plus a link to the full-resolution file. `drawing` / `layer` /
`objects` default to Top/Plan; `restored:false` + `restore_mismatch` = the
user's view could not be fully restored (tell the user).

## vw_create (write, one undo step)

Arguments: `objects` and/or `floor_plan`, `layer` (design layer for every object), `create_layer`
(make a missing layer instead of failing), `units`, `join_walls`, `preview`, `dry_run`, `document`,
`idempotency_key`. Each object: `type` + optional `id` (referenced later in
the same call as `"$id"`), `name`, `class`, `layer`.

Receipt: `count`, `ids` + `created` (<= 20 objects) or `uuids` (in list order) + `named` (your id -> index) +
`types`; `layer` / `layers`; `undo_steps` (plans above the bridge's per-transaction cap run as consecutive
transactions; a failure deletes what the earlier ones created). Spaces (and slabs) are sent behind walls.
`preview=true`, and automatically for >= 10 objects or 3D objects, adds an image of the new objects: look at it.
Unknown layer -> `not_found` with close names (nothing created); unknown symbol / hatch / line type / text style
-> `not_found` with up to 5 "did you mean" names.

| `type` | Fields |
|---|---|
| `line` | `start`, `end` |
| `rect` / `oval` | `origin` (or `center`) + `width` + `height`, or `start` + `end` |
| `circle` / `arc` | `center`, `radius`; arc `start_angle`, `sweep_angle` |
| `polygon` / `polyline` | `points` (polygon closed) |
| `wall` | `start`, `end`, `thickness` (200), `height` (3000), `style` |
| `slab` | `points`, `thickness` (200), `elevation`, `style` |
| `roof` | `points`, `slope` (30), `thickness`, `overhang` (500), `elevation` |
| `space` | `points`, `name` (the displayed room label), `room_id`, `height` |
| `text` | `at`, `text`, `size`, `rotation` |
| `dimension` | `start`, `end`, `offset` (300) |
| `symbol` | `symbol` (definition name), `at`, `rotation` |
| `door` / `window` | `wall` (`"$id"` or uuid), `offset` (same-call wall) or `at`, `width`, `height`; window `sill`; `parameters` |
| `parametric` | `plugin`, `at`, `rotation`, `parameters` `{"ParamId": value}` |
| `room` | `origin`, `width`, `depth`, `name` (+ `space`, `dimensions`): 4 real walls + label |
| `extrude` | `points` (profile) or `origin` + `width` + `depth`; `height` (or `top`), `elevation` (base, 0) |
| `column` | `at`, `width` + `depth` (400) or `diameter`, `height` (3000), `rotation`, `parameters` (Column plug-in fields) |

Style keys on any object (applied by a script after the native create, so the result says two undo steps):
`fill`, `line_style`, `pen_color`, `fill_color`, `line_weight`, `opacity`, text `font` / `text_size` /
`text_style` / `align` / `valign`, `arrows` / `start_marker` / `end_marker` (values as in vw_edit `set`).
Extrudes and columns alone are one undo step.

`floor_plan` = `{"rooms": [{name, origin, width, depth}], "walls": [...],
"doors": [{at, width}], "windows": [{at, width}], "wall_thickness",
"wall_height", "style": "bim" | "schematic"}`.

## vw_edit (write, destructive, one undo step)

Arguments: `changes` (required), `units`, `preview` (image of the changed objects), `dry_run`, `document`,
`idempotency_key`. Each change: `op` + `target` or `targets` (uuid,
`"name:..."`, `"$id"`, `"selection"`).

| `op` | Fields |
|---|---|
| `set` | values flat (`{"op":"set","target":..,"width":1000}`) or in `properties`. Type-specific keys are routed by the target: door/window `width`, `height`, `sill` (window), `open_angle`, `swing`, `hinge`, `configuration` (-> plug-in parameters); wall `thickness`, `height`, `style`; text `text`; Space `name` = displayed room label (+ object name), `room_id`. Anything else: name, class, fill_color, pen_color, fill_pattern, line_weight, opacity; `fill` none / solid / hatch, gradient, image or tile name (or `{"hatch": name}`); `line_style` line type name / "solid" / index; `font`; `text_size` (pt); `text_style` bold, italic, underline or a text style name; `align` left / center / right / justify; `valign`; `arrows` start / end / both / none; `start_marker` / `end_marker` arrow, open_arrow, dot, circle, slash, cross, none or `{style, size}` (page mm). Library resources are imported on first use; every value is read back. |
| `move` | `by` `[dx, dy]` |
| `rotate` | `angle` (degrees), `center` |
| `scale` | `factor` |
| `mirror` | `axis` "x" / "y", `center` |
| `reshape` | walls / lines / native linear dimensions: `start`, `end`; polygon/polyline: complete `points` |
| `section_heights` | `bands`, `expected_geometry`, `labels`, `stairs`, `rigid`, `fixed_points`, `preserve_text`; drawn 2D geometry, native chain sources and rigid 2D symbols; see `docs/SECTION_EDITING.md` |
| `duplicate` | `by`, `count` (1-100) |
| `delete` | target only |
| `parametric` | `parameters` (`plugin` optional: taken from the target); friendly names (`width`, `sill_height`, `open_angle`, `swing`, ...) map to the real ids; unknown ids -> "did you mean" |
| `order` | `position` front / back / forward / backward (stacking within the layer; also `op: "send_to_back"` / `"bring_to_front"`) |
| `class` / `layer` | `class` / `layer` |
| `group` / `ungroup` | - |
| `record` | `record`, `fields` |
| `select` / `deselect` | `targets` or `criteria` |

## vw_apply (write, destructive) - 0.7 grammar

`operations` = `[{"type", "operation_id"?, "params"}]` with type `create`
(`params.object_type` + 0.7 fields, e.g. wall `start_x, start_y, end_x, end_y`),
`set_properties` (`{"edits": [{"ref", "properties"}]}`), `transform` (`target,
dx, dy, rotation_deg, scale_x, scale_y, pivot_x, pivot_y`), `reshape`,
`update_parametric`, `duplicate`, `delete`, `order` (`target, position`); sheet / viewport / annotation
lifecycle operations when the bridge supports them. `coordinate_units`,
`idempotency_key`, `document`. Full grammar: MCP resource `vw://guide`.

## vw_view (changes the view only)

`standard_view`, `projection` (orthogonal / perspective / plan),
`render_mode`, `fit` (drawing / selection / objects / layer) + `objects`,
`zoom`, `center` `[x, y]`, `margin_pct`, `saved_view` (restore),
`save_view_as` (create), `layer` (make active), `document`. No arguments =
report the current view.

## vw_organize (write)

`kind` (layer, class, story, saved_view, worksheet, resource, viewport) + `action`
(create, set, delete, write, import, update). Fields: `name`, `new_name`,
`visibility` (show / gray / hide), `active`, `scale`, `elevation`, `opacity`,
`fill_color`, `pen_color`, `line_weight`, `use_at_creation`, `layer_kind`
(design / sheet), `title`, `cell` + `value` (worksheet write), `source` +
`resource_type` (resource import), `force` (delete non-empty layer), `units`,
`document`. Saved views: create, set (= restore), delete. Stories: create, set.

`kind="viewport"` (one undo step, rolled back on error, readback in `result.viewport`):
`action` create (`sheet` required; sheet layer created if missing) / set (`target` = viewport name or uuid)
/ update (regenerate `target`, or every viewport on `sheet`) / delete (`target`, `confirm=true`).
Fields: `name`, `new_name`, `layers` (design layers shown, others hidden), `scale` (100 or "1:100"),
`view` (top_plan, top, front, ..., iso), `render_mode`, `at` [x, y] (viewport centre on the sheet, paper mm),
`crop` [[x1, y1], [x2, y2]] or a polygon in model coordinates (`"none"` removes it), `classes`
`{name: show|hide|gray}`, `title` (drawing title), `annotations` `[{type text|line|rect|polygon|dimension,
... model coordinates, + style keys}]`, `units` (model lengths).

## vw_document (write, destructive)

`action` = list, info, activate, new (`template`), open (`path`), save,
save_as (`path`), close (`document`, `discard_changes="discard"`), export
(`path`, `format` pdf / dwg / dxf / ifc / png / jpg / tif / bmp / vwx, `range`
current_view / all_pages / sheet + `sheet`, `dpi`, `replace`), import (`path`,
`format`, `at`). Long exports / imports become jobs (`still_running` +
`job_id`).

## vw_script (write, destructive)

`code` (required), `language` (python / vectorscript), `undo_label`,
`background` (run as a job), `document`. Python gets `vs` pre-imported and may
set `result`. Returns `{ok, result, stdout, stderr, error?, rolled_back,
duration_ms}`. Disabled when Vectorworks runs with `VW_MCP_ALLOW_SCRIPT=0`.

## Old (0.7) tools -> 1.0

Nothing was removed: every 0.7 capability maps to a 1.0 tool. The complete
table is `docs/PARITY.md` in the repository.

<!-- tool-check: legacy-begin -->
| 0.7 call | 1.0 call |
|---|---|
| `vw_status(action="health" / "context")` | `vw_status()` |
| `vw_status(action="transaction", idempotency_key=...)` | `vw_status(idempotency_key=...)` |
| `vw_read(action="document" / "layers" / "summary" / "query" / "selection")` | `vw_read(what=...)` (same names; `object_type` -> `type`) |
| `vw_read(action="sheet_layers" / "viewports" / "viewport_annotations")` | `vw_read(what="sheets" / "viewports" / "annotations")` |
| `vw_read(action="plan_quality")` | `vw_read(what="plan_quality")` |
| `vw_catalog(action="capabilities" / "classes" / "symbols" / "worksheets" / "resources")` | `vw_read(what=...)` |
| `vw_catalog(action="parametric_schemas", query=X)` | `vw_read(what="parametric_schema", target=X)` |
| `vw_apply(...)` | `vw_create` / `vw_edit`, or `vw_apply` with the same grammar |
| `vw_execute_operations(...)` | `vw_apply` (same grammar) |
| `vw_io(action="capture")`, `vw_view(action="capture")` | `vw_capture` |
| `vw_io(action="export" / "import")` | `vw_document(action="export" / "import")` |
| `vw_view(action="get")` | `vw_read(what="view")` or `vw_view()` |
| `vw_view(action="set" / "fit")` | `vw_view(...)` |
| `vw_document(action="info")` | `vw_document(action="info")` or `vw_read(what="document")` |
| `vw_document(action="save" / "export" / "open")` | `vw_document` (same actions) |
| `vw_tool_safety` | removed: tool annotations + `error.retryable` / `error.commit_state` + the recovery table in SKILL.md |
| schematic floor-plan tools (`vw_create_schematic_*`) | `vw_create(floor_plan={..., "style": "schematic"})` |
| compatibility tools (`vw_ping`, `vw_bridge_status`, `vw_preflight_for_cad`, per-object create tools) | `vw_status` + `vw_create` / `vw_edit`; the 0.7 server stays available with `VW_MCP_TOOL_PROFILE=legacy` |
<!-- tool-check: legacy-end -->
