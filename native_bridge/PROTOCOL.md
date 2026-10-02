# Native Bridge Wire Protocol v2

Status: implemented by `VectorworksMCPBridge.vlb` 1.0.0 (`protocol: 2`,
`capability_revision: 6`). This document describes exactly what the C++ code
in `native_bridge/src` does; the binding design is `docs/V1_CONTRACT.md` §1
(deviations are recorded there). Hosts written for v1 (capability revision
≤ 5, the 0.7 server) keep working: every v1 action, param and result field is
still present.

Source map: transport `NativeTransport.cpp`, envelope `BridgeProtocol.cpp`,
params/JSON `JsonParams.cpp`, scheduler/jobs `RequestScheduler.cpp`, UI gate
`UiGate.cpp`, runtime/ping/dispatch `BridgeRuntime.cpp`, documents/handles
`DocumentRegistry.cpp`, view/capture `ViewCapture.cpp`, scripting
`ScriptEngine.cpp`, layers/stories/classes `OrganizationHandlers.cpp`,
discovery `Discovery.cpp`, 0.x handlers `VectorworksMCPBridge.cpp`, I/O
`NativeIOHandlers.cpp`.

---

## 1. Transport

- TCP on `127.0.0.1` only (`VW_MCP_HOST` may name another 127.x address).
- Port: first free port of **9877 … 9886**, bound with `SO_EXCLUSIVEADDRUSE`.
  `VW_MCP_PORT=<n>` pins exactly one port (no fallback). If no port can be
  bound the bridge retries with exponential back-off 2 s → 4 → … → 60 s cap
  (logged to `%USERPROFILE%\.vectorworks-mcp\native-bridge-startup.log`).
- Frame: 4-byte big-endian unsigned length + UTF-8 JSON, 1 byte … 16 MiB.
  Many sequential requests per connection. A bad header (0 or > 16 MiB) gets
  one `invalid_frame` error frame and the connection is closed. A frame that
  is not a valid request envelope gets an `invalid_request` error with
  `id:"native-transport-error"` and the connection stays open.
- Up to **16** concurrent clients. A 17th connection receives one error frame
  `{"id":"native-transport-rejected","success":false,"error":"…",
  "error_code":"too_many_clients","retryable":true,"commit_state":"not_started"}`
  and is closed.
- TCP keepalive (30 s idle / 5 s interval) and `TCP_NODELAY` on every client.
  A client socket with **no inbound traffic for 120 s** is closed by the
  bridge (reconnect transparently). While a request is executing the bridge
  is not reading, so long requests never trip the idle timer.
- A response larger than 16 MiB is replaced by
  `error_code:"response_too_large"` (`details {bytes}`); the connection stays
  usable. Request smaller pages (`limit`/`offset`, `max_bytes`).

### 1.1 Discovery file

After every successful bind the bridge writes (temp file + atomic rename)
`%USERPROFILE%\.vectorworks-mcp\instances\<pid>.json`:

```json
{"pid":12345,"port":9877,"protocol":2,"capability_revision":6,
 "bridge_version":"1.0.0","build_id":"862d87c-20261002T101500Z",
 "vectorworks_version":"2024","session_id":"vw-session-…",
 "started_at":"2026-10-02T10:15:03.120Z"}
```

It is rewritten if the listener is restarted (possibly on another port),
deleted on an application-shutdown deinit, and deleted by a static
destructor at process exit. Hosts must still prune files whose `pid` is dead
(hard kill / crash).

## 2. Envelope

### 2.1 Request

```json
{"id":"client-unique-id","action":"get_view","auth_token":"…",
 "params":{},"timeout_ms":25000}
```

- `id` (required, non-empty string) is echoed. An id that is still in flight
  on the bridge fails `duplicate_request_id`.
- `auth_token` must equal `VW_MCP_AUTH_TOKEN` (Vectorworks process env) or the
  first line of `%USERPROFILE%\.vectorworks-mcp\auth-token` (or
  `VW_MCP_AUTH_TOKEN_FILE`). The file is cached and re-read when its
  mtime/size changes; comparison is constant-time.
  `VW_MCP_INSECURE_NO_AUTH=1` disables auth (diagnostics only).
- `params` must be an object (missing → `{}`). Unknown keys are ignored.
- `timeout_ms` (top level, optional): how long the socket thread waits for a
  main-thread request. Default **25000**, clamped to 50 … 600000. If absent,
  `params.timeout_ms` is honoured as a fallback.
- JSON strings: full `\uXXXX` decoding including surrogate pairs (UTF-8 out)
  in the envelope and in params. Unpaired surrogates are rejected.

Generic params understood for every **main-thread** action:

| param | type | default | meaning |
|---|---|---|---|
| `async` | bool | false | queue as a job and answer `{"job_id","state":"queued","action"}` immediately |
| `busy_wait_ms` | int | 3000 | how long a queued request may wait for a closed UI gate before `vectorworks_busy` (0 … 600000) |
| `expect_document_fingerprint` | string | – | fail `document_changed` unless this document is active when the request starts |
| `activate_document` | bool | false | with the above: if that document is open, `SwitchToOpenFile` to it and continue |

### 2.2 Success response

```json
{"id":"client-unique-id","success":true,"result":{…}}
```

Every main-thread result that is a JSON object gets an extra member
`"timing":{"queue_wait_ms":n,"handler_ms":n,"total_native_ms":n}`.

### 2.3 Failure response

```json
{"id":"client-unique-id","success":false,"error":"human readable",
 "error_code":"vectorworks_busy","retryable":true,
 "commit_state":"not_started","details":{"ui_state":"modal","busy_wait_ms":3000}}
```

- `error` is always a non-empty string (v1 hosts keep working).
- Legacy form kept for document-lifecycle validation errors
  (`ViewDocument::Error` in `open_document` / `save_document`): `error` is a
  JSON-encoded object string `{"code","message","requested_path",
  "active_path","commit_state","retryable"}`; the v2 fields are set as well
  (`error_code` = that `code`).
- `commit_state`: `not_started` (nothing ran / validation failed),
  `rolled_back` (ran and was undone), `committed` (applied; e.g. a readback
  mismatch after the change), `unknown` (could not be determined — never
  blindly resend a write; use `transaction_status` / `job_status`).
- `retryable: true` means resending the identical request is safe.

### 2.4 Error codes

| error_code | retryable | commit_state | when |
|---|---|---|---|
| `auth_failed` | false | not_started | token missing/mismatch (`error`: "native bridge authentication failed") |
| `unknown_action` | false | not_started | action not registered / not routed |
| `invalid_request` | false | not_started (socket/reads), rolled_back (writes) | bad/missing params |
| `invalid_frame` | false | not_started | frame length outside 1 B … 16 MiB (connection closed) |
| `duplicate_request_id` | false | not_started | id already in flight |
| `queue_full` | true | not_started | 128 requests already queued |
| `too_many_jobs` | true | not_started | 64 unfinished async jobs |
| `too_many_clients` | true | not_started | 17th connection (only frame sent) |
| `bridge_not_ready` | true | not_started | main-thread pump not running |
| `bridge_stopping` | true | not_started | module shutting down / queue cancelled |
| `vectorworks_busy` | true | not_started | UI gate closed longer than `busy_wait_ms`; `details {ui_state, busy_wait_ms}` |
| `no_active_document` | false | not_started | action needs a document and none is open |
| `timeout_not_started` | true | not_started | did not start within `timeout_ms`; removed from the queue; `details {timeout_ms, ui_state}` |
| `still_running` | false | unknown | started but not finished within `timeout_ms`; converted into a job; `details {job_id, action, elapsed_ms, timeout_ms}` |
| `job_not_found` | false | not_started | unknown or expired job id |
| `cancelled` | false | not_started | job cancelled before it started (reported by `job_status`) |
| `document_changed` | false | not_started | `expect_document_fingerprint` mismatch; `details {expected_fingerprint, active_document, open_documents}` |
| `document_not_found`, `ambiguous_document` | false | not_started | activate/close target |
| `document_dirty` | false | not_started | close without `discard_changes:"discard"`; `details {fingerprint, dirty}` |
| `stale_reference` | false | not_started | handle id unknown / other document / object deleted; `object_uuids` not found (`details.missing`) |
| `unsupported` | false | not_started | e.g. `new_document` without a usable template (`details.tried`) |
| `script_disabled` | false | not_started | `VW_MCP_ALLOW_SCRIPT=0` |
| `script_failed` | false | unknown | an internal helper script failed (saved views create/delete, layer elevation/opacity, class set) |
| `capture_failed` | false | not_started | `capture_view` export failed for a non-I/O reason (view already restored; `details.restored`) |
| `confirmation_required`, `already_exists`, `layer_not_empty` | false | not_started | organisation guards |
| `readback_mismatch` | false | committed | `set_view` readback differs; `details {previous, current}` |
| `sdk_operation_failed`, `interface_unavailable` | false | varies | SDK call failed / interface missing |
| `invalid_path`, `input_not_found`, `unsupported_extension`, `output_exists`, `replace_confirmation_required`, `output_verification_failed`, `native_io_error` | false | not_started (exports) / rolled_back (imports) | `NativeIO::Error` (export/import/capture) |
| `confirmation_required`, `document_dirty`, `invalid_path`, `input_not_found`, `readback_mismatch`, `unknown_commit_state`, … | false | from the error | `ViewDocument::Error` (legacy JSON-string `error`) |
| `handler_failed` | false | not_started (reads), unknown (writes) | any other exception in a handler |
| `internal_error` | true | not_started | dispatcher / serialisation failure |

## 3. Threading, UI gate and jobs

Socket threads parse frames and answer the transport actions (§4) directly.
Every other action is queued and executed by a WM_TIMER pump (50 ms, plus a
posted wake-up message on enqueue) on a **message-only window**
(`HWND_MESSAGE`) owned by the Vectorworks main thread. All window-proc and
timer entry points are wrapped in `try/catch(...)`.

Before every dequeue the pump classifies the UI with Win32 calls only (no
SDK calls). `ui_state`:

| ui_state | condition |
|---|---|
| `modal` | Vectorworks main frame disabled (a modal dialog is up) |
| `tracking` | menu loop / popup menu / window move-size (`GetGUIThreadInfo` flags), mouse capture, or a mouse button held while a Vectorworks window is in the foreground |
| `loading` | a bridge document operation (open/new/close) is running, a 250 ms settle window after a document switch/close/open was observed, or the main frame does not exist / is hidden (startup) |
| `no_document` | otherwise idle, but no document is open |
| `idle` | otherwise |

- Requests are dequeued only in `idle`, and in `no_document` only for
  actions with `needs_document:false` (`list_documents`, `new_document`,
  `open_document`). Actions that need a document fail `no_active_document`
  as soon as the pump sees `no_document`.
- If the gate stays closed (`modal`/`tracking`/`loading`) longer than
  `busy_wait_ms`, the request fails `vectorworks_busy` (nothing ran).
- A pump tick that re-enters while a handler runs (nested message loop)
  never dequeues; it only publishes a non-idle `ui_state`.
- One request runs at a time; per tick at most 8 requests / 8 ms.
- Before a request runs the bridge refreshes the document registry, checks
  `needs_document`, then `expect_document_fingerprint`.

Timeouts and jobs:

- The socket thread waits `timeout_ms`. Not started → removed →
  `timeout_not_started`. Started → converted into a job → `still_running`
  with `details.job_id`; poll `job_status`. Results are never dropped.
- `params.async:true` → immediate `{"job_id","state":"queued","action"}`.
  Every main-thread action accepts `async`; `capabilities.actions[].long_running`
  marks the ones that should use it (`export_pdf`, `export_dwg`, `import_dwg`,
  `export_ifc`, `import_ifc`, `export_vectorworks_document`, `open_document`,
  `new_document`; also `capture_view` with a slow render mode, see
  `slow_render_mode`).
- Jobs are retained 15 minutes after finishing, at most 64 (oldest finished
  pruned first). Ids look like `job-17-3fa2c9d1`.

## 4. Socket-thread actions

These never wait for the main thread (auth still applies).

### `ping` (params ignored)

v1 fields (values unchanged): `pong:true`,
`version:"native-sdk-bridge-phase4"`, `bridge_kind:"native_sdk_bridge_phase4"`,
`dispatch_mode:"native_sdk"`, `handlers` (int), `cad_api_safe`,
`transport_only`, `native_bridge:true`, `native_phase:4`,
`capability_revision:6`, `capability_fingerprint`, `bridge_session_id`,
`implemented_actions[]`, `create_object_types[]`, `object_read_features[]`,
`cad_handlers_implemented:true`, `main_context_pump:"win32_ui_timer"`,
`main_context_pump_ready`, `dispatch_wakeup:"win32_message"`,
`pump_watchdog_ms:50`, `pump_budget_ms:8`, `main_context_idle_ms`.

v2 fields:

| field | type | meaning |
|---|---|---|
| `protocol` | 2 | wire protocol |
| `bridge_version` | "1.0.0" | |
| `build_id` | string | `<git short hash>[-dirty]-<yyyyMMddTHHmmssZ>` |
| `git_hash` | string | |
| `pid`, `port` | int | |
| `session_id` | string | same value as `bridge_session_id` |
| `vectorworks_version` | "2024" \| "2025" | SDK the module was built for |
| `started_at` | ISO-8601 UTC | module init time |
| `ui_state` | string | §3, published by the main thread every tick |
| `ui_state_reason` | string | e.g. `main_window_disabled_modal_dialog`, `menu_loop`, `mouse_capture`, `mouse_button_down`, `document_transition_settling`, `no_document`, `idle` |
| `handler_running_ms` | int | 0 when no handler runs |
| `running_action` | string \| null | |
| `queue_depth` | int | queued (not yet running) requests |
| `job_count` | int | retained jobs |
| `main_context_idle_ms` | int | ms since the last pump tick (-1 before the first); large ⇒ main thread blocked |
| `active_document` | object \| null | document record (§5), cached by the main thread (≤ 200 ms old while idle) |
| `open_document_count` | int | |
| `client_count` | int | open connections |
| `main_context_pump_window` | "message_only" | |
| `script_enabled` | bool | `run_script` allowed |

### `capabilities`

v1 manifest (`capability_revision`, `capability_fingerprint`, `native_phase`,
`implemented_actions`, `descriptors[]` `{action, native_phase, availability,
execution_context, may_write_document, destructive}`, `create_object_types`,
`object_kind_descriptors`, `object_read_features`) plus:

- `protocol: 2`, `bridge_version`, `build_id`, `vectorworks_version`
- `actions[]`: `{name, mutates, destructive, long_running, supports_async,
  needs_document, execution_context}` (`supports_async` is true for every
  main-thread action, false for socket-thread actions)
- `limits`: `{max_frame_bytes, default_timeout_ms:25000, max_timeout_ms:600000,
  default_busy_wait_ms:3000, max_jobs:64, job_retention_ms:900000,
  max_clients:16, client_idle_seconds:120}`

`run_script` is omitted from `implemented_actions` / `actions` when
`VW_MCP_ALLOW_SCRIPT=0`.

### `stop`

Closes **only the calling connection** after replying
`{"connection_closed":true,"bridge_running":true}`. The bridge keeps running.

### `job_status {job_id}`

```json
{"job_id":"job-3-1a2b3c4d","action":"export_pdf","request_id":"…",
 "state":"queued|running|done|failed|cancelled",
 "enqueued_at":"…Z","started_at":"…Z|null","finished_at":"…Z|null",
 "elapsed_ms":1234,"cancellable":false,"ui_state":"idle",
 "result":{…}}
```

- `state:"done"` → `result` (the action's result incl. `timing`).
- `state:"failed"|"cancelled"` → `error`, `error_code`, `commit_state`,
  `retryable`, `details` (when present) instead of `result`.
- While `running` and `ui_state` is `modal`/`tracking` the record adds
  `"blocked_by_ui":true,"hint_error_code":"vectorworks_busy"` (a dialog is
  up, e.g. an open that asks to convert a file).
- Unknown/expired id → `job_not_found`.

### `cancel_job {job_id}`

Queued job → removed:
`{"job_id","state":"cancelled","cancelled":true,"cancellable":true}`.
Running or finished → `{"job_id","state":…,"cancelled":false,
"cancellable":false}` (a running handler cannot be interrupted).

## 5. Documents and references

### Fingerprints

Every open document gets `fingerprint` = `vw-doc-<16 hex>`, generated when
the bridge first sees it (keyed by the SDK `fileRef` from `GetOpenFilesList`
and the active drawing-header handle). It is stable for the lifetime of the
open document — including untitled documents and after Save As — and
different for simultaneously open documents; a closed document's fingerprint
is never reused. The same value is now used in the v1
`binding.document_fingerprint`; `document_generation` increments whenever the
active document changes or a document closes.

Document record:

```json
{"fingerprint":"vw-doc-…","name":"Plan.vwx","path":"C:\\…\\Plan.vwx",
 "dirty":true,"active":true,"untitled":false,"file_ref":3}
```

- `dirty`: `true`/`false` for the active document in Vectorworks 2024 (the
  last known value for inactive ones); always `null` in Vectorworks 2025
  (`IsActiveFileChangedAfterLastSave` is gone from the 2025 SDK).
- `path` is `""` for untitled documents. Colon-separated paths reported by
  `GetOpenFilesList` ("Mac encoding") are converted to `C:\…`.

### Handles

`handle` fields are opaque ids `h<N>` mapped to `{uuid, document fingerprint}`
(stable per object while its document is open). `handle:<id>` targets and
`set_property.handle` re-resolve by UUID in the active document and fail
`stale_reference` when the id is unknown, belongs to another document, or the
object was deleted. Objects without a UUID get ids `h0-<N>` that never
resolve. A document's entries are dropped when it closes. Raw pointers are
never parsed from client input (0.x `0x…` ids are rejected as
`stale_reference`).

## 6. Main-thread actions

Legend: **R** read, **W** mutates the document, **D** destructive, **L**
long-running (use `async`). `needs_document` is true unless stated.

### 6.1 Documents

**`list_documents {}`** (R, no document needed) →
`{"documents":[record…],"active_fingerprint":"…"|null,"count":n,"document_generation":n}`

**`activate_document {fingerprint | path | name}`** (R) → `SwitchToOpenFile`.
Matching: `fingerprint` exact; else `path` (case-insensitive, `/`→`\`); else
`name` exact, then case-insensitive. →
`{"active_document":record,"previous_fingerprint":"…"|null,"switched":bool}`.
Errors: `document_not_found`, `ambiguous_document` (both with
`details.open_documents`), `sdk_operation_failed`.

**`new_document {template_path?}`** (W, L, no document needed). The SDK has
no dialog-free "new document" call, so the bridge opens a template with
`OpenDocumentPath(…, bShowErrorMessages=false)`. Template resolution:
`template_path` (absolute, must exist, else `input_not_found`) → env
`VW_MCP_NEW_DOCUMENT_TEMPLATE` →
`<Vectorworks>\Libraries\Defaults\Templates\Blank (Metric).sta`,
`Blank (UK Metric).sta`, `Blank (Imperial).sta` → first top-level `.sta` there
whose name starts with blank/leer/vide/vuoto/blanco. None → `unsupported`
(`details.tried`). →
`{"document":record,"template_path":"…","untitled":bool,"method":"open_template","warnings":[…]}`.
**Check `untitled`**: if Vectorworks opened the template file itself
(`untitled:false`) a warning says so — never write into / save over it.

**`close_document {fingerprint?, discard_changes?}`** (W, D) closes the given
(default: active) document with `CloseDocument` and never shows a save
prompt: a dirty — or, in 2025, unknown-dirty — document is refused with
`document_dirty` unless `discard_changes:"discard"` (or `true`). With discard,
a dirty document is first saved to a private temp file
(`%TEMP%\vectorworks-mcp\discard\…vwx`, deleted afterwards) so the close
cannot prompt; the document's original file on disk is untouched. The
previously active document is re-activated if still open. →
`{"closed":true,"fingerprint":"…","discarded_changes":bool,"active_document":record|null,"open_document_count":n}`.

**`open_document {file_path | path, replace_dirty_confirmation?}`** (W, D, L,
no document needed) opens synchronously on the main thread (or activates the
document if it is already open); long opens return `still_running` + job id.
The legacy `replace_dirty_confirmation` is accepted but no longer required
(Vectorworks opens documents side by side). →
v1 fields `{"operation":"open_document","path","saved":false,"requested_path",
"active_path","commit_state":"committed"}` plus `"already_open":bool,
"document":record`. A dialog during the open shows as `ui_state:"modal"` and
`job_status.blocked_by_ui`.

**`save_document {file_path | path?, replace_confirmation?}`** (W):
`SaveActiveFile` for an already saved document without a path;
`SaveActiveDocumentPath` for an explicit path (required for untitled
documents — never a file chooser). Overwriting a different existing file needs
`replace_confirmation:"REPLACE_EXISTING_FILE"`. → v1 fields `{operation, path,
saved, requested_path, active_path, commit_state}` plus `document:record`
(the fingerprint is unchanged after Save As).

**`get_document_info {}`** (R, v1) → `{filename, filepath, layers[],
layer_count, total_objects, dirty, document_fingerprint, document_generation,
bridge_session_id, active_layer_uuid, active_layer_name, binding{…}}`.

### 6.2 View and capture

Enum params accept numbers or names (case-insensitive; spaces, `-`, `/` →
`_`):

- `standard_view`: `user_defined`=0, `front`=3, `back`=4, `left`=5,
  `right`=6, `top`=7 (aliases `top_plan`, `plan`), `bottom`=8,
  `right_iso`=9 (`iso`), `left_iso`=10, `right_rear_iso`=11,
  `left_rear_iso`=12, `bottom_right_iso`=13, `bottom_left_iso`=14,
  `bottom_right_rear_iso`=15, `bottom_left_rear_iso`=16
- `projection`: `orthogonal`=0, `perspective`=1, `cavalier_45`=2,
  `cavalier_30`=3, `cabinet_45`=4, `cabinet_30`=5, `plan`=6 (`top_plan`)
- `render_mode`: `wireframe`=0, `quick`=1, `solid`=2, `shaded_solid`=3,
  `shaded_no_lines`=4, `final_shaded`=5, `hidden_line`=6,
  `dashed_hidden_line`=7, `opengl`=11 (`shaded`), `fast_renderworks`=12,
  `fast_shadow_renderworks`=13, `final_renderworks`=14 (`realistic`,
  `renderworks`), `custom_renderworks`=15, `artistic_renderworks`=17,
  `sketch`=18

**View record** (`get_view`; `set_view.previous/current`;
`capture_view.view_used`):

```json
{"standard_view":7,"standard_view_name":"top","projection":6,"projection_name":"plan",
 "render_mode":0,"render_mode_name":"wireframe","zoom":0.5,"center":[x,y],
 "view_matrix":[[a,b,c],[d,e,f],[g,h,i],[tx,ty,tz]],
 "active_layer":"Design Layer-1","active_layer_uuid":"…","active_layer_kind":"design",
 "screen_px":[w,h],"drawing_area_px":[w,h],"dpi":96,"plan_view":true,
 "fit_to_objects_applied":false,"selection_cleared":false}
```

`zoom` is the window zoom factor (`varWindowZoomFactor`; `null` when
unreadable). `center` is in document world units. `drawing_area_px` is
derived from the view centre (the SDK screen rect may include docked
palettes). `plan_view` is true for sheet layers, plan projection, or top view
in orthogonal projection.

**`get_view {}`** (R) → view record.

**`set_view {standard_view?, projection?, render_mode?, zoom?, center?, fit?,
object_uuids?, margin_pct?, fit_layer?, saved_view?, layer?, clear_selection?,
fit_to_objects?}`** (R — changes view state only) →
`{standard_view, projection, render_mode, fit_to_objects_applied,
selection_cleared, previous:view, current:view, fit?:fit}` (the first five
are the v1 fields). Application order: `layer` (activate by name/`uuid:`),
`saved_view` (restore), `standard_view`, `projection`, `render_mode`,
`clear_selection`, `fit`, `zoom`, `center`.

- `fit`: `drawing` (all top-level objects of visible design layers, or of the
  active sheet layer), `selection`, `objects` (needs `object_uuids`: array of
  uuids / `uuid:` refs, or a comma-separated string; any missing →
  `stale_reference` with `details.missing`), `layer` (`fit_layer` or `layer`,
  else the active layer). Legacy `fit_to_objects:true` ⇒ `fit:"drawing"`;
  `object_uuids` without `fit` ⇒ `fit:"objects"`.
- Fit uses the union of `GetObjectBounds`: `SetViewCenter` + `SetZoomFactorN`
  so bounds + `margin_pct` (default 5, 0–200 %) fill ≤ 85 % of the drawing
  area. No `DoMenuName`, no select-all. Exact in plan views; in 3D views the
  3D bounds are projected through the view matrix (best effort).
- fit record: `{"applied":bool,"method":"bounds_plan"|"bounds_projected_3d_best_effort"|"no_objects"|"screen_size_unavailable"|"visible_extent_unavailable"|"zoom_out_of_range",
  "bounds":{left,bottom,right,top,object_count}|null,"view_rect"?:[l,t,r,b]}`.
- `clear_selection:true` deselects everything (persistent in `set_view`).
- Readback mismatch of standard view / projection / render mode →
  `readback_mismatch` (`commit_state:"committed"`).

**`capture_view {path | file_path, format?, replace?, overwrite?,
replace_confirmation?, fit?, object_uuids?, margin_pct?, fit_layer?,
standard_view?, projection?, render_mode?, layer?, saved_view?, zoom?,
center?, clear_selection?, rect?, crop_to_fit?, restore?, update_viewports?,
reset_plugin_objects?, export_georeferencing?, fit_to_objects?}`** (R):

1. Remembers the view (active layer, standard view, view matrix, projection,
   render mode, zoom, centre) and the selection (`ForEachObjectN(allSelected)`).
   With `saved_view` and `restore`, class/layer visibilities are also
   snapshotted by creating a temporary saved view
   (`vs.SaveSheet("__vw_mcp_restore_<ms>", True, True, True)`, deleted after).
2. Applies the temporary changes exactly like `set_view`, then `DrawScreen`.
3. `ExportImage` with `fExportRect` = `rect` (world coordinates, converted
   with `WorldRect2ViewRect`, clamped to the drawing area) or — when a plan
   view `fit` was applied and `crop_to_fit` (default true) — the fitted
   bounds + margin; otherwise the whole drawing area.
4. With `restore` (default **true**): restores layer, standard view, view
   matrix, projection, render mode, zoom, centre and selection, `DrawScreen`,
   and verifies (`restore_verified`).

`format`: `png` (default) or `jpg`/`jpeg`; an extension is appended to
`path` when it has none (otherwise the extension decides). Existing files fail
`output_exists` unless `replace:true` (alias `overwrite:true`);
`replace_confirmation` then defaults to `REPLACE_EXISTING_FILE`.
`update_viewports` and `reset_plugin_objects` default **false** (1.0).
`rect`: `[[x1,y1],[x2,y2]]`, `[x1,y1,x2,y2]`, `{left,bottom,right,top}` or
`{x1,y1,x2,y2}`.

Result: v1 fields `{"operation":"capture_view","path","size_bytes",
"replaced_existing"}` plus `"width","height"` (pixels from the PNG/JPEG/BMP
header; 0 if unknown), `"bytes"`, `"format"`, `"restored"`,
`"restore_verified"` (view equal and same selection count after restore),
`"view_used":view`, `"selection_count"`, `"fit"?`, `"export_rect"?:[l,t,r,b]`,
`"slow_render_mode":bool` (render mode other than wireframe/OpenGL — prefer
`async`). Failures: I/O errors keep their `NativeIO` code (e.g.
`output_exists`); other failures → `capture_failed` (`details.restored`). The
view is restored before any error is returned.

**`export_image {path | file_path, replace?, replace_confirmation?, rect?,
update_viewports?, reset_plugin_objects?, export_georeferencing?}`** (R) →
the current view as-is → `{"operation":"export_image","path","size_bytes",
"replaced_existing","width","height","bytes"}`. Extensions `.png .jpg .jpeg
.tif .tiff .bmp`; `.png` appended when missing.

**`saved_views {action, name?, save_view?, save_classes?, save_layers?}`**
(W for create/delete):

- `list` → `{"saved_views":[{"name","index"}],"count":n}`
- `restore {name}` → `RestoreSavedView` →
  `{"restored":true,"name","previous":view,"current":view}`
- `create {name, save_view=true, save_classes=true, save_layers=true}` →
  in-process `vs.SaveSheet` → `{"created":true,"name","method":"script:SaveSheet"}`
  (`already_exists` if taken)
- `delete {name}` → `vs.VDelete` → `{"deleted":true,"name","method":"script:VDelete"}`

### 6.3 Organisation

**`layers {action:"list", kind?, offset?, limit?, include_extras?}`** (R) →
`{"layers":[layer…],"total","offset","has_more","next_cursor":"<offset>"|null,"active_layer"}`,
`limit` default 500 (1–5000, clamped), `kind` `design`|`sheet` filters.
Layer record:

```json
{"uuid":"…","name":"Ground Floor","kind":"design","visible":"show",
 "visibility":0,"active":true,"scale":50.0,"elevation":0.0,"thickness":0.0,
 "opacity":100.0,"story":"Level 1","object_count":12}
```

`visible` is `show|gray|hide` (`visibility` 0/2/-1). `elevation`/`thickness`
(document units, `vs.GetLayerElevation`) and `opacity` (100 −
`vs.HGetLayerTransp`) come from one in-process Python call per page (design
layers only; `null` for sheet layers, on failure, or with
`include_extras:false`). `story` is the story name or `null`.
`object_count` counts top-level objects.

**`layers {action:"create", name, kind?="design"|"sheet", scale?, elevation?,
title?, activate?=false}`** (W) → one undo step → `{"created":true,"layer":layer}`;
the previously active layer is restored unless `activate:true`.

**`layers {action:"set", target (name | "uuid:…" | "name:…"; `name` accepted
as alias), new_name?, visibility? (show|gray|hide|0|2|-1), active?, scale?,
elevation?, opacity? (0–100, design layers), title? (sheet layers)}`** (W) →
one undo step → `{"updated":true,"layer":layer}`.

**`layers {action:"delete", target, confirm:"delete", force?}`** (W, D):
refuses non-empty layers (`layer_not_empty`, `details.object_count`) unless
`force:true`, and the last design layer → `{"deleted":true,"name","uuid","object_count"}`.

**`stories {action:"list"}`** (R) →
`{"stories":[{"name","uuid","elevation","suffix","layers":[…]}],"count","sdk_story_count","note"}`.
Stories are discovered through their layers; stories without layers are only
counted in `sdk_story_count`.
**`stories {action:"create", name, suffix?, elevation?}`** (W) →
`{"created":true,"name","story":story|null}`.
**`stories {action:"set", target|name, elevation?, suffix?, new_name?}`** (W) →
`{"updated":true,"story":story}`.

**`manage_classes`**:

- `{action:"list"}` → v1: **JSON array of class names** (unchanged).
- `{action:"list", detailed:true, offset?, limit?=1000}` (v2) →
  `{"classes":[{"name","visible":"show|gray|hide","active":bool,"use_at_creation":bool}],
  "total","offset","has_more","next_cursor","active_class"}`.
- `{action:"create", class_name}` → `{"action":"create","class_name","created","existed"}` (v1).
- `{action:"delete", class_name, confirm:"DELETE_CLASS"}` → `{"action":"delete","class_name","deleted"}` (v1).
- `{action:"set", name|class_name, visibility?, active?, fill_color?,
  pen_color?, line_weight? (mils, 0–2000), use_at_creation?}` (v2, one undo
  step; implemented in-process with `vs.ShowClass/HideClass/GrayClass/
  NameClass/SetClFillFore/SetClPenFore/SetClLW/SetClUseGraphic`) →
  `{"action":"set","class_name","changed":[…],"class":{"name","visible","active","use_at_creation"}}`.
  Colours: `[r,g,b]` (0–255 scaled ×257, or 0–65535), `"#rrggbb"`, or
  `"r,g,b"` (0–65535).

### 6.4 Scripting

**`run_script {language?="python"|"vectorscript", code, undo_label?="Vectorworks MCP script", transaction?=true, async?}`** (W, D)

- Runs on the Vectorworks main thread through
  `IPythonScriptEngine::ExecuteScript` / `IVectorScriptEngine` — no
  listener, no dialog. VectorScript error dialogs are suppressed (program
  variable 9876) during the run. Aliases: `py`; `vs`, `pascal`.
- **Python**: the code runs as
  `exec(compile(code, "<vw_mcp_script>", "exec"), ns)` with a fresh single
  dict `ns = {"vs": vs, "__name__": "__vw_mcp__", "__builtins__": …,
  "result": None}` — `vs` is pre-imported and top-level functions see each
  other as globals. Assign `result`; it is JSON-serialised with
  `json.dumps(…, default=str)` (non-serialisable → `repr`). `sys.stdout` /
  `sys.stderr` are captured (≤ 256 KiB each, `*_truncated` flags). Any
  `BaseException` (incl. `SystemExit`) is caught; the formatted traceback
  (last 64 KiB) is returned in `error` (its last line is
  `ExceptionType: message`).
- **VectorScript**: compiled first (failure → `ok:false`,
  `error:"VectorScript compile error at line N: …"`), then executed; `result`
  is always `null` and there is no stdout.
- `transaction:true`: the whole run is one undo event named `undo_label`
  (`SupportUndoAndRemove` + `NameUndoEvent` … `EndUndoEvent`); when the
  script fails the event is rolled back with `UndoAndRemove`
  (`rolled_back:true`) and `rollback_verified` compares the layer count and
  the top-level object count of every layer before/after.
  `transaction:false`: no undo wrapper, nothing is rolled back.
- `VW_MCP_ALLOW_SCRIPT=0` in the Vectorworks process → `script_disabled`.
- Limits: `code` ≤ 4 MiB; serialised `result` ≤ 8 MiB.

Result (a failing script is still `success:true`, with `ok:false`):

```json
{"ok":true,"language":"python","result":{…},"stdout":"","stderr":"",
 "stdout_truncated":false,"stderr_truncated":false,
 "transaction":true,"rolled_back":false,"undo_label":"…","duration_ms":12.5}
```

On failure additionally `"error":"Traceback …"` and, when rolled back,
`"rollback_verified":bool`.

### 6.5 Import / export

All paths absolute; outputs are written to a staging file beside the target
and renamed into place (an existing target needs `replace:true` +
`replace_confirmation:"REPLACE_EXISTING_FILE"`). `path` is an alias of
`file_path`. Result `{"operation","path","size_bytes","replaced_existing"}`;
imports add `"document_mutation":{verified, before, after,
created_object_uuids, deleted_object_uuids, created_layers, deleted_layers,
changed_layers, active_layer_changed}`.

| action | params | notes |
|---|---|---|
| `export_pdf` (L) | `file_path`, `range?` (`current_view` \| `all_pages` \| `sheet`), `sheet?` (sheet layer name; required for `range:"sheet"`), `dpi?` (alias `resolution_dpi`; 72–2400, default 300), `current_view_only?` (v1), `update_viewports?`=false, `reset_plugin_objects?`=false, `recalculate_worksheets?`=true | `sheet` activates that sheet layer, exports its current view and restores the active layer |
| `export_dwg` (L) | `file_path`, `update_viewports?`=false, `reset_plugin_objects?`=false, `recalculate_worksheets?`=true | silent `IImportExportDWG` |
| `import_dwg` (W, L) | `file_path` (.dwg) | silent import + mutation receipt |
| `export_ifc` (L) | `file_path` (.ifc/.ifczip/.ifcxml) | `IIFCSupport::ExportFile(…, bShowUI=false)`, current document IFC settings |
| `import_ifc` (W, L) | `file_path` (.ifc/.ifczip/.ifcxml) | `IIFCSupport::ImportFile(…, bShowUI=false)` + mutation receipt |
| `export_vectorworks_document` (L) | `file_path` (.vwx), `target_file_version?`=29 | `ExportDocument` |

The v1 `expected_document_*` binding params, when present, are validated
before and after these exports and around `capture_view` / `export_image`.

### 6.6 Objects and transactions (v1 shapes; v2 changes noted)

Object record (`get_objects`, `selection get`, `find_objects`, receipts):
`{"handle":"h17","uuid","type","type_id","plugin_name"?,"native_type"?,"name",
"text"?,"room_id"?,"height"?,"net_area"?,"gross_area"?,"layer","class",
"class_name","fillColor":"r,g,b","penColor":"r,g,b","lineWeight",
"fillPattern","opacity","bounds":{"top_left":[x,y],"bottom_right":[x,y]}}`
(`handle` is now an opaque table id, §5).

- `get_objects {layer?, object_type?, limit?=100 (≤ 1000)}` → array.
- `query_objects {mode?="objects"|"criteria"|"selection", criteria?, layer?,
  object_type?, offset?, limit?=100 (≤ 1000), field_count?, field_N?,
  max_bytes?, expected_document_*?}` →
  `{"items":[…],"has_more","offset","returned","next_offset","binding"}`.
  v2: a page also stops at `max_bytes` (default 8 MiB, 64 KiB–14 MiB) with
  `has_more:true`; continue at `offset = next_offset`. v2: `uuids` (array or
  comma string of uuids / `uuid:` refs) or `uuid` switches to exact lookup with
  `GetObjectByUuid` (no document scan; `layer`/`object_type` filters still
  apply) and adds `"missing_uuids":[…]`.
- `find_objects {criteria="ALL", layer?, object_type?, limit?}` → array.
- `drawing_summary {scan_limit?, limit?, include_examples?, example_limit?, layer?, object_type?}` → v1 summary.
- `selection {action: get|clear|select|delete, criteria?, limit?, confirm?}` →
  v1 shapes; v2: delete registers undo once (`AddBeforeSwapObject` +
  `DeleteObject(undo=false)`).
- `create_object`, `create_wall`, `create_text`, `create_linear_dimension`
  (flat params) → `{type, handle, uuid, warnings?, vertex_count?, closed?,
  object, transaction_artifact, undo_committed}`.
- `batch_create_objects {object_count, object_N_json}` →
  `{atomic:true, rollback_on_error:true, created_count, created:[…]}`.
- `set_property {handle, property_name, value}` (allowlist `name`, `class`,
  `fillColor`, `penColor`, `fillPattern`, `lineWeight`, `opacity`) →
  `{changed:true, handle, property_name, value, before, after}`; unknown /
  stale handle → `stale_reference`.
- `apply_operations {idempotency_key?, operation_count, operation_N_json,
  expected_document_*?}` — ops `create`, `set_property`, `object.transform`,
  `object.reshape`, `object.update_parametric`, `object.duplicate`,
  `object.delete`; targets `uuid:`, `name:`, `handle:`, `$local_ref`. v2:
  - door / window / hosted `parametric` creates accept
    **`wall_uuid:"$<local_ref>"`** of a wall created earlier in the same
    transaction (walls + openings in ONE undo step); `wall_uuid:"uuid:<u>"` is
    accepted too. A `$ref` that is not declared by an earlier operation fails
    at parse time (`invalid_request`, nothing ran).
  - multiple `name:` targets are resolved with one document traversal; a
    single one uses `GetNamedObject`.
  - `object.delete` registers undo once.
  - idempotency identity is `<bridge session>:<document fingerprint>`.
  Result `{ok:true, atomic:true, verified:true, replayed, idempotency_key?, transaction:{…}}`.
- `transaction_status {idempotency_key, expected_document_*?}` →
  `{state:"committed", recorded_result, current_objects_verified:false}` or
  `{state:"unknown", retry_safe:false, reason}`.
- `describe_parametric_schema {plugin_name}`, `get_layers {}`,
  `get_sheet_layers`, `get_viewports`, `get_viewport_annotations`,
  `symbol {action:"insert", definition_name, x?, y?, rotation_deg?}`,
  `worksheet {action:"read"|"write", worksheet_name, row, column, formula?}`,
  `apply_documentation_operations` (registered but disabled: documentation
  writes await undo/redo acceptance) — v1 params/results.

Paged resource reads (v2; the default limit 5000 keeps v1 behaviour for
normal documents):

- `resources {query?, offset?, limit?=5000 (≤ 20000), cursor?}` →
  `{"symbols":[{name,native_node_type}],"worksheets":[{name,rows,columns,native_node_type}],
  "symbols_total","worksheets_total","total","offset","has_more","next_cursor"}`
  (one offset/limit pages both lists; `total` = the larger list).
- `symbol {action:"list", query?, offset?, limit?, cursor?}` →
  `{"symbols":[…],"total","offset","has_more","next_cursor"}`.
- `worksheet {action:"list", query?, offset?, limit?, cursor?}` →
  `{"worksheets":[{name,rows,columns}],"total","offset","has_more","next_cursor"}`.
- `next_cursor` is the next offset as a string; pass it back as `cursor`.

### 6.7 Not native in 1.0 (host via `run_script`)

`group` / `ungroup` and `records` (formats, attach, get/set fields) are not
native actions; hosts implement them through `run_script`
(`vectorworks_mcp/scripts_lib/edit_engine.py`, `records.py`). They are not
listed in `capabilities`.

## 7. Lifecycle

- `kPluginModuleInit`: pins the DLL for the process lifetime, caches the main
  frame (`GS_GetMainHWND`), creates the message-only pump window, starts the
  transport (port range, back-off), writes the discovery file and returns
  `kDoNotCache` so Vectorworks initialises the module on every launch.
  Idempotent. The plug-in registers no tools or objects.
- `kPluginModuleDeinit`: if the main frame was seen and is gone (application
  shutdown) the bridge stops the pump, cancels queued work, closes sockets,
  joins all threads and deletes the discovery file. A deinit while
  Vectorworks is running (module released after init) is ignored and logged
  (`deinit_ignored`) — the pinned bridge keeps serving.
- `Start()` joins an exited listener and reaps finished client threads
  before creating new threads (no assignment to a joinable `std::thread`);
  active connections of a previous listener keep serving. An accept loop
  that keeps failing (> 50 consecutive errors, with 50 ms…1 s sleeps) exits
  and the pump restarts the transport with back-off.

## 8. Environment

| variable (Vectorworks process) | effect |
|---|---|
| `VW_MCP_AUTH_TOKEN` | token (overrides the file) |
| `VW_MCP_AUTH_TOKEN_FILE` | token file path |
| `VW_MCP_INSECURE_NO_AUTH=1` | disable auth |
| `VW_MCP_HOST` | bind address (127.0.0.0/8 only) |
| `VW_MCP_PORT` | pin one port (no range fallback) |
| `VW_MCP_ALLOW_SCRIPT=0` | disable `run_script` |
| `VW_MCP_NEW_DOCUMENT_TEMPLATE` | template for `new_document` |
