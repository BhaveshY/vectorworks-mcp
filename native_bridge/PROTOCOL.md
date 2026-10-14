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
| `script_crashed` | false | unknown | a hardware exception (access violation, …) inside the Python/VectorScript engine was contained by the SEH guard (§6.4.1); the engine is now poisoned; `details {exception_code, script_engine:"poisoned", next_step}` |
| `script_engine_unavailable` | false | not_started | `run_script` / a Python-backed handler after an earlier `script_crashed` in this process; nothing ran; same `details` |
| `capture_failed` | false | not_started | `capture_view` export failed for a non-I/O reason (view already restored; `details.restored`) |
| `confirmation_required`, `already_exists`, `layer_not_empty` | false | not_started | organisation guards |
| `layer_not_found` | false | not_started | `apply_operations` create op (or plan-level) `layer` names no layer of the active document; `details {operation, layer, available_layers}`; nothing was written |
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
  `vectorworks_busy` and `timeout_not_started` details carry `modal_dialog`
  (§3.1) when a dialog blocks.
- A pump tick that re-enters while a handler runs (nested message loop)
  never dequeues; it only publishes a non-idle `ui_state`.
- One request runs at a time; per tick at most 8 requests / 8 ms.
- Before a request runs the bridge refreshes the document registry, checks
  `needs_document`, then `expect_document_fingerprint`.

### 3.1 Modal dialogs and the Home screen

Vectorworks shows a modal web-view **Home** screen at startup and whenever
the last document is closed (top-level main frame disabled; a window titled
`Vectorworks … - Home` with a descendant of class `VectorworksCDlgWebView`),
and sometimes an **update notice** (`#32770` titled `Vectorworks`, static text
"… is available", buttons `Download…` / `Not Now` / `Notification Settings…`).
Both close the UI gate (`ui_state:"modal"`).

The bridge classifies the dialog that blocks the main UI thread with Win32
only (`EnumThreadWindows` on the Vectorworks main thread, class names and
`InternalGetWindowText`; never a cross-thread `SendMessage`, so it is safe on
any thread) and reports it as `modal_dialog: {title, class, kind}` with
`kind` `home` | `update_available` | `backup_prompt` | `other` (`null` when none) in `ping` and
in `vectorworks_busy` / `timeout_not_started` details.

**Automatic dismissal** (env `VW_MCP_AUTO_DISMISS_HOME`, default on; `0`
disables): only kinds `home` (`PostMessage(WM_CLOSE)`; a retry 1.5 s later
alternates with `WM_COMMAND IDCANCEL`) and `update_available`
and `backup_prompt` (the autosave "name and save this untitled document now?" prompt:
"No" via `WM_COMMAND`, which neither saves nor discards anything)
("Not Now" via `PostMessage(WM_COMMAND, BN_CLICKED)` with that button's id —
`Download…` is never pressed) are ever touched, one dialog per attempt (update
notice first), at most once per window per 1.5 s, and only if **no other
dialog** is open (any other enabled, non-tool-window `#32770` / `*Dlg*` /
`*Dialog*` top-level window of the main thread, or a known dialog disabled by
something on top of it, blocks dismissal). Every
other dialog is reported, never touched. Triggers:
- a pump tick sees `modal`/`loading` while requests are queued (the request
  then runs as soon as the gate opens, within its `busy_wait_ms`);
- a pump tick re-entered from a nested modal loop while a handler runs
  (`close_document` of the last document, `new_document`, `open_document`
  open the Home screen synchronously inside the SDK call);
- the socket thread waiting for a request (every 200 ms) while the gate is
  `modal`/`loading` or a handler runs.

Buttons are pressed with `PostMessage(BM_CLICK)` to the button window
(1.0.x; live 2026-10-14 `WM_COMMAND(id)` did not close the IFC Export
Report, `BM_CLICK` did); every second attempt for the same window (≥ 1.5 s
later) falls back to `WM_COMMAND(id, BN_CLICKED)` to the dialog.

**Handler-owned acknowledgement.** While a bridge handler runs, a dialog that
appeared during that handler (not open when it started), is dialog-like
(`#32770` / `*Dlg*` / `*Dialog*` class), has no inputs (visible edit — except
read-only edits —, combo box, list box, check box, radio button, slider) and
whose visible enabled buttons are all in {OK, Close, Done, Save Report…, Help,
Details…} (also German: Schließen, Fertig, Bericht speichern…, Hilfe) is
acknowledged: OK, else Close/Schließen, else Done/Fertig is pressed — never
Save Report, Help or Details. Any other button (Cancel, Yes/No, …) leaves the
dialog untouched (reported as `modal_dialog`). The record is
`{kind:"handler_message"|"handler_report", title, at, trigger, message,
buttons:[labels], pressed:"OK", grid?:true, grid_rows_captured?:bool,
rows?:[text], grid_read?:method|reason}` —
`handler_report` when the dialog has a report grid, more than one button, or
"report"/"bericht" in its title. `message` is the visible static text (≤ 2000
chars). **Report grid rows** (BCGPGridCtrl in the IFC Export Report; 2026-10-14)
are read before OK is pressed, on the Vectorworks **main thread** only (the
pump timer runs inside the dialog's modal loop, so `AccessibleObjectFromWindow`
/ `IAccessible` calls are direct, in-apartment calls): MSAA
(`AccessibleObjectFromWindow(grid, OBJID_CLIENT)`, `AccessibleChildren`; each
row = the child's name/value plus its cells joined with ` | `), else
list-view items (`LVM_GETITEMTEXTW`), else child window texts. ≤ 50 rows of
≤ 300 chars, 1.5 s budget, access violations contained (SEH).
`grid_rows_captured:true` with `rows` and `grid_read:"msaa"|"list_view"|
"child_windows"`, otherwise `false` with `grid_read` = the reason
(`msaa_unavailable_0x…`, `msaa_no_rows`, `msaa_fault_0x…`, `no_rows`,
`not_read_on_main_thread`). Socket-thread watchdogs wait up to 3 s before
pressing OK on a grid dialog so the main-thread read happens first.

Results of requests during or before which a dialog was auto-dismissed carry
`"dismissed_dialogs":[{kind,title,at,trigger,…}]` and, for the Home screen,
`"home_dismissed":true` (in `details` for failures). With auto-dismissal
disabled, closing the last document leaves the request running until the user
closes the Home screen (the caller gets `still_running` + job id; nothing is
lost).

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
| `modal_dialog` | object \| null | `{title, class, kind:"home"\|"update_available"\|"other"}` (§3.1) |
| `auto_dismiss_dialogs` | bool | `VW_MCP_AUTO_DISMISS_HOME` is not `0` |
| `dismissed_dialogs` | array | last ≤ 5 auto/explicit dismissals `{kind,title,at,trigger}` |
| `script_enabled` | bool | `run_script` allowed |
| `script_engine` | "ok" \| "poisoned" | "poisoned" after a contained script crash (§6.4.1); then also `script_engine_fault` (hex code of the first fault) and `script_engine_faults` (count) |
| `max_operations` | 2000 | largest `operation_count` of one `apply_operations` call (was 250) |

### `dismiss_home {wait_ms?=3000 (0–15000)}`

Closes the Home screen and/or the update notice (§3.1 rules; works even when
`VW_MCP_AUTO_DISMISS_HOME=0`), waiting up to `wait_ms` for them to go away.
No-op success when neither is shown; never touches any other dialog. No
document needed. →
`{"dismissed":bool,"home_dismissed":bool,"dismissed_dialogs":[{kind,title,at,trigger}],"still_shown":bool,"blocked_by"?:"other_dialog_open","blocking_dialog"?:{title,class},"modal_dialog":obj|null,"auto_dismiss_enabled":bool,"ui_state":"…"}`.

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
  max_clients:16, client_idle_seconds:120, max_operations:2000}`
- `object_read_features` additionally lists `op_layer`, `order_op`,
  `auto_send_to_back`, `capture_isolation`, `compact_import_receipts`,
  `capture_scale_boost`, `capture_world_bounds`, `wall_insert_breaks`,
  `ifc_report_rows` (1.0.x feature flags; `capability_revision` stays 6).

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
Closing the **last** open document makes Vectorworks open the modal Home
screen inside `CloseDocument`; the bridge closes it automatically (§3.1,
result then has `home_dismissed:true`), leaving Vectorworks with zero
documents but responsive (`ui_state:"no_document"`; `new_document` /
`open_document` still work). Callers that want a document to remain should
open one before closing the last.

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
object_uuids?, margin_pct?, fill_pct?, fit_layer?, saved_view?, layer?, clear_selection?,
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
  so bounds + `margin_pct` (default 5, 0–200 %) fill `fill_pct` (default
  **90**, 20–98; was a fixed 85) % of the drawing area along the limiting
  axis. In plan views the projected rect is measured with
  `WorldRect2ViewRect` and the zoom refined (≤ 4 passes) until it fills
  `fill_pct` ± 3 %. No `DoMenuName`, no select-all. In 3D views the 3D bounds
  are projected through the view matrix (best effort, one pass). When the
  standard view or projection changes together with a fit, the screen is
  redrawn once before the fit is measured.
- fit record: `{"applied":bool,"method":"bounds_plan"|"bounds_projected_3d_best_effort"|"no_objects"|"screen_size_unavailable"|"visible_extent_unavailable"|"zoom_out_of_range",
  "bounds":{left,bottom,right,top,object_count}|null,"fill"?:0.9,"iterations"?:n,"view_rect"?:[l,t,r,b]}`
  (`fill`: achieved long-side fraction of the drawing area, plan views).
- `clear_selection:true` deselects everything (persistent in `set_view`).
- Readback mismatch of standard view / projection / render mode →
  `readback_mismatch` (`commit_state:"committed"`).

**`capture_view {path | file_path, format?, replace?, overwrite?,
replace_confirmation?, fit?, object_uuids?, margin_pct?, fill_pct?, fit_layer?,
standard_view?, projection?, render_mode?, layer?, saved_view?, zoom?,
center?, clear_selection?, rect?, crop_to_fit?, restore?, update_viewports?,
reset_plugin_objects?, export_georeferencing?, fit_to_objects?,
capture_target?, plan_default?, isolate?, ungray_others?, max_px?,
scale_boost?}`** (R):

**Capture target** (1.0.x). `capture_target` = `current` | `drawing` |
`selection` | `objects` | `layer` | `sheet` | `saved_view`; when absent it is
inferred: `saved_view` given → `saved_view`; `fit:"layer"` → `layer`, or
`sheet` when that layer (`fit_layer`/`layer`, else the active layer) is a
sheet layer; `fit:"objects"` (or `object_uuids`) → `objects`;
`fit:"selection"` → `selection`; `fit:"drawing"` → `drawing`, or `sheet` when
the active (or `layer`) layer is a sheet layer (then fitted to that sheet
layer); no fit → `current`. An explicit `capture_target` supplies the matching
`fit` when `fit` is absent (`layer`/`sheet` → `fit:"layer"`); `sheet` with a
design layer → `invalid_request`. So the current host mapping
(`view:"layer"` → `{layer, fit:"layer"}`, `view:"objects"` → `{fit:"objects",
object_uuids}`, `view:"drawing"` → `{fit:"drawing"}`, `view:"current"` → no
fit) needs no change; sending `capture_target` explicitly is recommended.

- **Plan default** (`plan_default`, default true): for `drawing`, `layer`
  and `objects` without `standard_view`, `projection` and `saved_view` the
  capture uses Top/Plan (`standard_view` 7 + `projection` plan) instead of the
  window's current (possibly iso/3D) view. `current`, `selection`,
  `saved_view` and sheet captures keep the current view. Result
  `plan_default_applied`.
- **Isolation** (`isolate`, default true for `layer`, `sheet`, `objects`):
  one layer (the requested design/sheet layer, or the single layer that holds
  all `object_uuids`) → that layer is activated and Layer Options
  (`varLayerOptions`) set to Active Only (1); objects on several layers →
  Layer Options Show/Snap/Modify Others (5), those layers shown (visibility
  0) and every other design layer hidden (−1, `ovLayerVisibility`). Class
  Options Active Only / Gray Others / Gray-Snap Others (1/2/6) → Show/Snap/
  Modify Others (5) (class visibilities are respected). Every changed value
  is reverted after the export (also with `restore:false`) and verified.
  Result `isolation:{applied, mode:"active_only"|"hidden_layers", layers:[names],
  layer_options_before, layer_options_used, class_options_before,
  class_options_used, hidden_layers}` (`{applied:false}` otherwise).
  Layer switches into/out of a sheet layer use
  `SetCurrentLayerN(layer, bAllowUnifiedViewMaintenance=true)`.
- **Gray others** (`ungray_others`, default true; `drawing` and `selection`
  targets without isolation): Layer Options Gray Others / Gray-Snap Others
  (2/6 — 6 is the default of the 2024 Blank template) and Class Options 2/6
  are switched to Show/Snap/Modify Others (5) for the export and reverted
  (`isolation.mode:"ungray_others"`). With gray layers `ExportImage` draws
  every non-active layer as light outlines **without fills** (live
  2026-10-14). This also explains the old acceptance observation that an
  `isolate:false` control capture equalled the isolated one: isolation works;
  the control showed the other layer grayed, i.e. without its black fill.
- **Standard views from Top/Plan** (2026-10-14): `standard_view` other than
  `top` / `user_defined` without `projection` while the layer is in the plan
  projection now also sets the orthogonal projection (as the UI does).
  Before, `SetCurrentView` alone kept Top/Plan: `set_view {standard_view:
  "right_iso"}` failed with `readback_mismatch` and `capture_view` silently
  exported a plan image.
- **Resolution / scale boost** (`max_px`, default 1600, clamped 400–4000;
  `max_px:0` or `scale_boost:false` disables). `ISDK::ExportImage` ignores the
  screen zoom and the size of `fExportRect`: it renders the exported region
  at **72 dpi of page size**, i.e. at the layer scale (live 2026-10-14: a
  2000×1000 mm rect on a 1:50 layer → 126×63 px at zoom 1, 4, 16 and with
  fit; the same capture at 1:2.5 → 2497×1249 px; plan and 3D). Measured rule
  (±1 px): `image px = region px × 72 / (screen dpi × zoom)`. Before the
  export the bridge estimates the long edge of the export region this way and,
  when it is below `max_px`, divides the scale of the shown design layers
  (the active layer for Active Only / an `active_only` isolation, else every
  visible design layer) by `k = clamp(max_px / estimate, 1, 50)`
  (`SetLayerScaleN`), divides the zoom by `k` (same world region on screen),
  re-derives the crop and exports. If the image is still below 60 % of
  `max_px` (fast render modes only) one corrective export with the measured
  factor replaces the file. The exact original scales are restored right after
  the export — before the view restore, also on errors (RAII) — and read back.
  Text, dimensions and line weights are page based and keep their pixel size
  (they look relatively smaller in boosted images). Sheet layers are never
  changed (`skipped:"sheet_layer"`). Result `scale_boost:{applied, factor,
  target_px, estimated_px, passes, skipped?, layers:[{name, scale,
  boosted_scale}], scales_restored?, restore_errors?, dirty_before?,
  dirty_after?, marked_dirty?}`; a scale that does not read back adds
  `restore_mismatch` field `layer_scale` (`restored:false`). **Side effects**:
  `SetLayerScaleN` marks a clean (saved, unmodified) document as changed
  (live: a run_script `SetLScale` + restore turned `dirty` false → true; the
  view changes of a capture do not) — the ISDK has no call to clear the flag,
  so `marked_dirty:true` is reported (VW 2024; `null` on 2025, where the dirty
  state is not readable). **Undo**: the scale changes were recorded as an
  undo entry (live: `capture_leaves_no_undo_entry` failed — one Undo after a
  capture did not remove the sentinel). The boost now runs in its own undo
  event, opened only when `IsCurrentlyBuildingAnUndoEvent()` is false (never
  nested in a caller's event): `SupportUndoAndRemove` +
  `SetUndoMethod(kUndoSwapObjects)` + `AddBeforeSwapObject(layer)` per boosted
  layer, then the scales; after the export `UndoAndRemove()` restores the
  scales and drops the event. The scales are read back (layer handles that
  are no longer listed are re-resolved by UUID, `layer_handles_changed:true`);
  any mismatch falls back to the explicit restore. Optional
  `scale_boost_undo:"undo_and_remove"` (default) | `"end_and_remove"`
  (explicit restore inside the event, then `EndAndRemoveUndoEvent`, nothing
  replayed) | `"off"` (no event, pre-fix behaviour). `scale_boost` adds
  `undo_mode, undo_event, undo_removed` (UndoAndRemove restored every scale),
  `undo_event_removed, layer_handles_changed?, undo_note?`; `dirty_*` /
  `marked_dirty` are still reported. `NonUndoableActionOK` was not used (it
  asks the user with a dialog).
- **Image → world mapping** (plan projection only). Result
  `world_bounds:{left, bottom, right, top, units:"mm"}`, `px_per_mm` (mean of
  `px_per_mm_xy:[x, y]`). `ExportImage` renders exactly the export region
  (`fExportRect`, or the whole drawing pane when it is empty; live: pane
  1426×924 at zoom 4 → 268×174 px, crops keep their objects centred), so
  `world_bounds` = the region's corners through `ViewPt2WorldPt` (after the
  scale boost) and `px_per_mm` = image size / world size. Image pixel
  `(u, v)` (origin top-left) ↦ `x = left + u / px_per_mm`,
  `y = top − v / px_per_mm` (live check with a `rect` capture: ≤ 1 px).
  3D views: `world_bounds:null, px_per_mm:null`.

Steps:

1. Remembers the view (active layer, standard view, view matrix, projection,
   render mode, zoom, centre), Layer/Class Options, every layer's visibility
   and the selection (`ForEachObjectN(allSelected)`). With `restore`, the
   view and class/layer visibilities are also snapshotted natively with ISDK
   `ViewCreateCurrent` (an unnamed in-memory view; restored with
   `ViewRestore`, freed with `ViewDelete`; no saved-view object, no Python) —
   for every capture since 1.0.x (was: only with `saved_view`).
2. Applies isolation, then the temporary view changes exactly like
   `set_view`, then `DrawScreen`.
3. `ExportImage` with `fExportRect` = `rect` (world coordinates, converted
   with `WorldRect2ViewRect`, clamped to the drawing area) or — when a plan
   view `fit` was applied and `crop_to_fit` (default true) — the fitted
   bounds + margin; otherwise the whole drawing area. The image size follows
   the page size at 72 dpi, not the screen (see **Resolution / scale boost**
   — the scale boost runs between the view change and the export).
4. With `restore` (default **true**): reverts isolation, `ViewRestore`s the
   snapshot, then explicitly restores layer, standard view (before the
   projection: Top/Plan = view 7 + plan projection), projection, view matrix
   (user-defined views, or when the rotation still differs), render mode,
   zoom, centre and selection, `DrawScreen`, and **re-reads** everything. If
   any field differs a second pass runs (with an extra redraw).
   `restored:true` only when the re-read state equals the state before the
   capture; otherwise `restored:false` and
   `restore_mismatch:[{field, before, after}]` with `field` in
   `active_layer`, `standard_view`, `projection`, `render_mode`,
   `view_matrix` (rotation part, tolerance 1e-6), `zoom` (relative 1e-4),
   `center`, `layer_options`, `class_options`, `layer_visibility` (+`layer`),
   `selection_count`, `exception`. Hosts must surface `restored:false` (the
   user's view was not fully restored).

`format`: `png` (default) or `jpg`/`jpeg`; an extension is appended to
`path` when it has none (otherwise the extension decides). Existing files fail
`output_exists` unless `replace:true` (alias `overwrite:true`);
`replace_confirmation` then defaults to `REPLACE_EXISTING_FILE`.
`update_viewports` and `reset_plugin_objects` default **false** (1.0).
`rect`: `[[x1,y1],[x2,y2]]`, `[x1,y1,x2,y2]`, `{left,bottom,right,top}` or
`{x1,y1,x2,y2}`.

Result: v1 fields `{"operation":"capture_view","path","size_bytes",
"replaced_existing"}` plus `"width","height"` (pixels from the PNG/JPEG/BMP
header; 0 if unknown), `"bytes"`, `"format"`, `"capture_target"`,
`"plan_default_applied"`, `"fit_to_objects_applied"` (the fit was applied;
also in `view_used`), `"isolation"`, `"restored"` (verified, see step 4),
`"restore_verified"` (same value as `restored`; legacy name),
`"restore_passes"` (1–2, with `restore`), `"restore_mismatch"?`,
`"view_used":view`, `"selection_count"`, `"fit"?`, `"export_rect"?:[l,t,r,b]`,
`"drawing_area_px":[w,h]` (of the captured view), `"scale_boost"`,
`"world_bounds"`, `"px_per_mm"`, `"px_per_mm_xy"?`, `"encoded_format"`
(`png`/`jpeg`/… actually in the file), `"transcoded_from"?` / `"transcode_error"?`
(see below),
`"slow_render_mode":bool` (render mode other than wireframe/OpenGL — prefer
`async`). Failures: I/O errors keep their `NativeIO` code (e.g.
`output_exists`); other failures → `capture_failed` (`details.restored`,
`details.restore_mismatch?`). The view is restored before any error is
returned.

**Image encoding (1.0.x).** `ISDK::ExportImage` writes JPEG data whatever the
extension (live 2026-10-14: every `.png` capture was JPEG). `capture_view` and
`export_image` now check the magic bytes and, when they do not match the
extension, re-encode the staged file with WIC (Windows Imaging Component;
PNG = lossless 24-bit) before it is moved into place: result
`encoded_format:"png"`, `transcoded_from:"jpeg"`. If WIC fails the original
data is kept and the result says `encoded_format:"jpeg"`,
`transcode_error:"…"` (hosts must then use the real format, not the
extension). Re-encoding does not remove JPEG artefacts already present.

**`export_image {path | file_path, replace?, replace_confirmation?, rect?,
update_viewports?, reset_plugin_objects?, export_georeferencing?, max_px?,
scale_boost?}`** (R) → the current view (or `rect`, clamped to the drawing
pane) → `{"operation":"export_image","path","size_bytes",
"replaced_existing","width","height","bytes","scale_boost","world_bounds",
"px_per_mm","px_per_mm_xy"?,"export_rect"?}`. The scale boost, its restore
(scales, then the original zoom and centre) and the world mapping work as in
`capture_view`; no corrective second export. Extensions `.png .jpg .jpeg
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

`list`/`restore` are native. ISDK 2024/2025 has no dialog-free call to
create or delete a *named* saved view (only `CreateSavedViewWithUI`), so
`create`/`delete` remain the only internal-Python users here; they fail
`script_engine_unavailable` once the engine is poisoned.

Rule (1.0): built-in handlers use native SDK calls; the internal Python engine
is used only where no native API exists — today layer opacity and named
saved-view create/delete.

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
(document units) are read natively (`ObjectVariables` `ovLayerHeightInCurrUnits`
/ `ovLayerThicknessInCurrUnits`). `opacity` (100 − `vs.HGetLayerTransp`) has
no ISDK/ObjectVariables accessor in SDK 2024/2025 and is the one value still
read through one internal Python call per page; it is `null` when scripting
is disabled or the engine is poisoned. Both are design-layer only (`null` for
sheet layers, on failure, or with `include_extras:false`). `story` is the
story name or `null`. `object_count` counts top-level objects.

**`layers {action:"create", name, kind?="design"|"sheet", scale?, elevation?,
title?, activate?=false}`** (W) → one undo step → `{"created":true,"layer":layer}`;
the previously active layer is restored unless `activate:true`.

**`layers {action:"set", target (name | "uuid:…" | "name:…"; `name` accepted
as alias), new_name?, visibility? (show|gray|hide|0|2|-1), active?, scale?,
elevation?, opacity? (0–100, design layers), title? (sheet layers)}`** (W) →
one undo step → `{"updated":true,"layer":layer}`. `elevation` is set natively
(`ovLayerHeightInCurrUnits`, read back); `opacity` uses `vs.HSetLayerTransp`
(no native setter).

**`layers {action:"delete", target, confirm:"delete", force?}`** (W, D):
refuses non-empty layers (`layer_not_empty`, `details.object_count`) unless
`force:true`, and the last design layer → `{"deleted":true,"name","uuid","object_count"}`.
The layer is removed with `DeleteObject(layer, true)` (one undo record).

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
  "total","offset","has_more","next_cursor","active_class"}`. Native readback
  (`GetClVisibility`, `GetActiveClass`, `GetClUseGraphic`; no Python).
- `{action:"create", class_name}` → `{"action":"create","class_name","created","existed"}` (v1).
- `{action:"delete", class_name, confirm:"DELETE_CLASS"}` → `{"action":"delete","class_name","deleted"}` (v1).
- `{action:"set", name|class_name, visibility?, active?, fill_color?,
  pen_color?, line_weight? (mils, 0–2000), use_at_creation?}` (v2). **Native
  ISDK only** (1.0: the earlier Python implementation crashed the 2024 Python
  engine in `vs.SetClFillFore`): `SetClVisibility`, `GetClColor`/`SetClColor`
  (fill/pen fore; RGB → `RGBToColorIndexN`), `SetClLineWeight`,
  `SetClUseGraphic`, active class via `SetDefaultClass` / `GetActiveClass`.
  Class attributes are document settings, not objects, so they cannot be
  registered with `kUndoSwapObjects`: **the change is not part of Vectorworks
  Undo** (`undoable:false`). The handler snapshots the previous state and
  restores it itself if a step fails (`sdk_operation_failed`,
  `commit_state:"rolled_back"`). →
  `{"action":"set","class_name","changed":[…],"undoable":false,"readback_mismatch":[…],
  "class":{"name","visible","active","use_at_creation","line_weight","fill_color":[r,g,b],"pen_color":[r,g,b]}}`
  (colours 0–255).
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

#### 6.4.1 Script engine crash containment

Wrong-typed `vs.*` arguments are a crash class, not a Python error: e.g.
`vs.SetClFillFore(name, vs.RGBToColorIndex(r, g, b))` (an int where the stub
declares COLOR = `(r, g, b)` 0–65535) raised an access violation inside
`Vectorworks2024.exe`, called from `python39.dll`, and terminated Vectorworks.
Two layers address this:

1. **Host lint (prevention).** `vw_script` checks every `vs.<name>` call
   against `vectorworks_mcp/data/vs_signatures.json` (generated from the SDK
   `vs.py` stubs by `scripts/gen-vs-signatures.py`) before sending and returns
   `script_lint_failed` (nothing sent). The host's own snippets are checked by
   `scripts/lint-vs-snippets.py`; native-generated Python is audited in
   `native_bridge/VS_CALL_AUDIT.md`.
2. **Native SEH guard (containment).** Every `IPythonScriptEngine::ExecuteScript`,
   `IVectorScriptEngine::CompileScript` / `ExecuteScript` call (both
   `run_script` and `RunInternalPython`, i.e. layer extras, class set, saved
   view create/delete) runs inside `__try/__except` in a small function with
   only POD locals (MSVC forbids `__try` next to objects that need unwinding).
   The filter handles hardware faults only (access violation, stack overflow
   — `_resetstkoflw()` re-arms the guard page —, in-page error, illegal/
   privileged instruction, integer/float divide by zero, misalignment, array
   bounds); C++ exceptions and everything else continue the search unchanged.

After a contained fault the bridge:

- does **not** call back into Python: the interpreter's thread state, frame
  stack, recursion counter and the GIL acquired by the engine were abandoned
  mid-call by the unwind; the result file is discarded unread;
- closes the open undo event with `EndUndoEvent` (itself SEH-guarded) instead
  of `UndoAndRemove`: replaying the swap list could traverse an object the
  faulting call left half-modified. Whatever the script changed before the
  fault stays in the document as one undo step the user may undo manually;
  no rollback verification, no `DrawScreen`;
- replies `script_crashed` (`commit_state:"unknown"`) and marks the scripting
  engine **poisoned** for the rest of the process: later `run_script` calls
  and Python-backed handlers fail fast with `script_engine_unavailable`
  (`commit_state:"not_started"`, before any undo event is opened); `ping`
  reports `script_engine:"poisoned"`; the host adds a `vw_status` problem
  with next_step "save your work and restart Vectorworks". Native
  (non-script) actions keep working, including `manage_classes set`,
  layer elevation and `capture_view {saved_view, restore:true}` (native since
  1.0). While poisoned `layers list` reports `opacity:null`, `layers set
  {opacity}` and `saved_views create/delete` fail (no native API).

Trade-off / residual risk: containment converts a certain crash into a
degraded but running Vectorworks so the user can save, but the process
state after the fault is not trustworthy. Frames between the fault and the
guard were discarded without running destructors (`/EHsc`), so memory,
locks or critical sections held by Vectorworks or Python at that point may
leak or stay held — a later Vectorworks operation (or a Python plug-in /
Marionette network run by the user) can hang or crash. Faults that never
reach the frame-based handler are not contained: a vectored exception
handler or crash reporter installed by Vectorworks runs first and may
terminate the process; heap corruption may surface later outside the guard;
`__fastfail`/`abort` and Python fatal errors are not exceptions. Recommend
"Save As" to a new file and a restart, and do not trust drawing data read
after the fault until then.

### 6.5 Import / export

All paths absolute; outputs are written to a staging file beside the target
and renamed into place (an existing target needs `replace:true` +
`replace_confirmation:"REPLACE_EXISTING_FILE"`). `path` is an alias of
`file_path`. Result `{"operation","path","size_bytes","replaced_existing"}`;
imports (`import_dwg`, `import_ifc`) add `document_mutation`:

- default (compact, 1.0.x, finding #21): `{verified, detail:false,
  created_object_count, deleted_object_count,
  created_object_uuids_sample:[≤ 20 uuids], created_layers:[≤ 50 names],
  created_layer_count, deleted_layer_count, changed_layer_count,
  object_count_before, object_count_after, active_layer_changed}`;
- with param `detail:true` (the previous full receipt): `{verified,
  detail:true, before, after, created_object_uuids, deleted_object_uuids,
  created_layers:[{uuid,name,native_node_type,visible}], deleted_layers,
  changed_layers, active_layer_changed}`.

| action | params | notes |
|---|---|---|
| `export_pdf` (L) | `file_path`, `range?` (`current_view` \| `all_pages` \| `sheet`), `sheet?` (sheet layer name; required for `range:"sheet"`), `dpi?` (alias `resolution_dpi`; 72–2400, default 300), `current_view_only?` (v1), `update_viewports?`=false, `reset_plugin_objects?`=false, `recalculate_worksheets?`=true | `sheet` activates that sheet layer, exports its current view and restores the active layer |
| `export_dwg` (L) | `file_path`, `update_viewports?`=false, `reset_plugin_objects?`=false, `recalculate_worksheets?`=true | silent `IImportExportDWG` |
| `import_dwg` (W, L) | `file_path` (.dwg) | silent import + mutation receipt |
| `export_ifc` (L) | `file_path` (.ifc/.ifczip/.ifcxml), `method?` (`auto` \| `sdk` \| `script`, default `auto`), `schema?` (`auto` \| `ifc2x3` \| `ifc4`, default `auto` = IFC2x3 then IFC4) | see below |
| `import_ifc` (W, L) | `file_path` (.ifc/.ifczip/.ifcxml), `method?` (as export), `detail?` | see below + mutation receipt |
| `export_vectorworks_document` (L) | `file_path` (.vwx), `target_file_version?`=29 | `ExportDocument` |

**IFC (1.0.x, finding #17; reworked 2026-10-14).** Live on VW 2024 29.0.8
(Architect): `ExportFile(bShowUI=false)` returned VCOM 1 in ~240 ms after
showing the modal "IFC Export Report"; `vs.IFC_ExportNoUI` returned False in
~30 ms without a dialog; no file for untitled and saved documents, plain or
dotted names (also reproduced with `IFC_SetIFCScheme` 0–4 and `/` paths).
`export_ifc` now: reads the document's schema (`IFC_GetIFCScheme`) and the
IFC header (`IFC_ExportHeaderAsJson`, ≤ 1200 chars, as diagnostics); then
for each schema (`schema`, default IFC2x3 then IFC4 =
`kIFCSupportScheme_2x3` / `kIFCSupportScheme_2x4`): `SetIFCScheme` +
`IFC_SetIFCScheme` + `UpdateVWDocumentIFCData`, then
`IIFCSupport::ExportFile(path, bShowUI=false)`, then the native
`IIFCSupport::IFC_ExportNoUI(path)` (VW 2021+, the entry point behind
`vs.IFC_ExportNoUI`); stops at the first produced file. `method:"auto"`
finally tries `vs.IFC_ExportNoUI` through the internal Python engine
(skipped when scripting is disabled or the engine is poisoned); `script` runs
only that, `sdk` never. The original schema is restored. The file is written
**directly to `file_path`** when it does not exist yet (removed on failure),
otherwise to a plain sibling `<stem>.vw-mcp-ifc-<n>.<ext>` (no leading dot)
that replaces it. Success is decided by the **produced file** (non-empty,
starts with `ISO-10303-21` / `PK` / `<`), not by the VCOM result. The
"IFC Export Report" is acknowledged by the handler-owned dialog rule (§3.1)
after its grid rows were read. Result adds `"method":
"sdk:IIFCSupport::ExportFile"|"sdk:IIFCSupport::IFC_ExportNoUI"|
"script:vs.IFC_ExportNoUI"`, `"schema"?:"IFC2x3"|"IFC4"`, `"ifc_format":
"step"|"zip"|"xml"`, `"setup":{original_schema, header_status?, header?}` and
`"attempts":[{method, ok, schema?, set_scheme?, ifc_set_scheme?,
update_document_data?, vcom_result?, sdk_reported_success?, file_produced,
elapsed_ms, report?, returned?, error?, skipped?}]` (`report` = title,
message and grid rows of the report dialog(s) shown during that attempt). No
file → `sdk_operation_failed` whose message contains the report text
("IFC Export Report: … | row | row …"), the setup and the attempts JSON.
`import_ifc`: `sdk` = `IIFCSupport::ImportFile(…, bShowUI=false)` (accepted
when the SDK reports success or the document changed); `auto` falls back to
`vs.IFC_ImportNoUI(path)` when the SDK call failed without changing the
document. Result adds `method`, `attempts:[{method, ok, vcom_result?,
document_changed, elapsed_ms, error?}]` and
`warnings:["…no document change was observed"]` when nothing changed.

The v1 `expected_document_*` binding params, when present, are validated
before and after these exports and around `capture_view` / `export_image`.

### 6.6 Objects and transactions (v1 shapes; v2 changes noted)

Object record (`get_objects`, `selection get`, `find_objects`, receipts):
`{"handle":"h17","uuid","type","type_id","plugin_name"?,"native_type"?,"name",
"text"?,"room_id"?,"height"?,"net_area"?,"gross_area"?,"layer","class",
"class_name","fillColor":"r,g,b","penColor":"r,g,b","lineWeight",
"fillPattern","opacity","bounds":{"top_left":[x,y],"bottom_right":[x,y]}}`
(`handle` is now an opaque table id, §5).

**Wall inserts (1.0).** Doors, windows, symbols and other plug-in objects
inserted in a wall live in the wall's member list, which the layer walk and
`ForEachObjectN(descendIntoAll)` never visit. Every enumeration — `get_objects`,
`query_objects` (objects / `criteria:"ALL"` / selection modes), `find_objects`
with `criteria:"ALL"`, `drawing_summary`, `get_document_info.total_objects`,
name-target resolution, selection, import/rollback snapshots — walks each
wall's inserts right after the wall. Insert records add
`"parent_uuid":"<wall uuid>","host":"wall"`; door/window records add
`width`/`height` (plug-in parameters `Width`/`Height`). Other criteria
strings go through `ForEachObjectInCriteria` unchanged (e.g.
`((PON='Door'))` already finds inserts). `object_type` accepts node type names
(`wall`, `rect`, …), `door`/`window`/`space`, and any parametric plug-in name
compared case-insensitively ignoring spaces, `_` and `-` (`"Stair"`,
`"custom stair"`).

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
  v1 shapes; delete registers undo exactly once with `DeleteObject(h, true)`
  (1.0: the v2 `AddBeforeSwapObject` + `DeleteObject(h, false)` combination
  was live-verified NOT to restore the object on Undo).
- `create_object`, `create_wall`, `create_text`, `create_linear_dimension`
  (flat params) → `{type, handle, uuid, warnings?, vertex_count?, closed?,
  object, transaction_artifact, undo_committed}`.
- `batch_create_objects {object_count, object_N_json}` →
  `{atomic:true, rollback_on_error:true, created_count, created:[…]}`.
- `set_property {handle, property_name, value}` (allowlist `name`, `class`,
  `fillColor`, `penColor`, `fillPattern`, `lineWeight`, `opacity`) →
  `{changed:true, handle, property_name, value, before, after}`; unknown /
  stale handle → `stale_reference`.
- `apply_operations {idempotency_key?, operation_count (1 … 2000; was 250),
  operation_N_json, layer?, auto_order?, join_walls?, expected_document_*?}` —
  ops `create`, `set_property`, `object.transform`, `object.reshape`,
  `object.update_parametric`, `object.duplicate`, `object.delete`, `order`;
  targets `uuid:`, `name:`, `handle:`, `$local_ref`. 1.0.x:
  - **per-op layer** (finding #14): a `create` op accepts `layer` (layer
    name, `name:<n>`, `uuid:<u>` or a bare layer uuid); the plan-level
    `layer` param is the default for create ops without their own. All
    layers are resolved before anything is written: an unknown layer fails
    the call with `layer_not_found` (`commit_state:"not_started"`,
    `details {operation, layer, available_layers}`) — never a silent fallback
    to the active layer. A sheet layer is accepted only for 2D types (`rect`,
    `oval`, `circle`, `line`, `arc`, `polygon`, `text`, `linear_dimension`,
    `symbol`), else `invalid_request`. The bridge activates the op's layer
    for the create (`SetCurrentLayerN`; create ops without `layer` go to the
    layer that was active when the call started) and re-activates the
    original active layer when the call ends (success or failure). Doors,
    windows and wall-hosted plug-ins live in their host wall: the plan-level
    `layer` does not apply to them, and an op-level `layer` that differs from
    the wall's layer fails the plan (`invalid_request`, rolled back). Create receipts add `"layer":"<name>"` (the object's actual
    layer). Walls on different layers are never auto-joined.
  - **automatic stacking** (finding #1; plan-level `auto_order`, default
    true): after all ops and wall joins, created Space objects and slabs whose
    container is a layer are moved to the back of that layer
    (`InsertObjectBefore(obj, FirstMemberObj(layer))`; relative creation
    order kept, slabs behind spaces) so walls, doors and windows draw on top.
    An explicit `order` op on such an object in the same plan wins. Result:
    `transaction.sent_to_back` (count) and top-level copy `sent_to_back`;
    `transaction.send_to_back_failed` (count, only when > 0 — a readback that
    did not reach index 0; the plan still commits).
  - **`order` op** (alias `object.order`): `{"op":"order","target":<ref>,
    "position":"front"|"back"|"forward"|"backward"}` (aliases `to_front`,
    `bring_to_front`, `to_back`, `send_to_back`, `bring_forward`,
    `send_backward`). Moves the target within its container's stacking order
    (first member = back): front = after the last member, back = before the
    first, forward/backward = one step. Existing objects are registered with
    `AddBeforeMoveObject` / `AddAfterMoveObject` (one Undo restores the
    position); wall inserts (doors/windows) cannot be ordered
    (`invalid_request`). A position readback mismatch fails the plan (rolled
    back). An existing object ordered in a plan may not be deleted later in the
    same plan. Receipt `{index, op:"order", target, uuid, position, changed,
    stack_index_before, stack_index_after, stack_count, layer}` (indices
    0-based from the back; `changed:false` when it already was there).
  - **door / window / hosted `parametric` creates: wall break is best
    effort** (2026-10-14). Vectorworks keeps an insert in the wall's member
    list AND in a `kSymbolBreak` (`SymbolBreakType.theSymbol` = the insert)
    that positions it along the wall; an insert without a break may be
    dropped when the wall is regenerated (class change, move, reset). Live
    VW 2024 history: `AddObjectToContainer` alone → creates succeed, no break
    (`vs.GetObjectWallOffset` → False); commit 714c15d added
    `AddObjectToContainer` + `ISDK::AddBreak` + a wall `ResetObject` →
    AddBreak registered **no** break and the wall reset made every
    door/window create fail ("final object disappeared before commit",
    "host wall dropped the inserted opening"). Since the fix the native create
    never fails because of break handling and never resets the wall:
    1. `CreateCustomObject` (detached), then `ISDK::AddBreak(wall, offset,
       kSymbolBreak, {theSymbol, insertMode:kSymInsertOnCenter, breakMode:
       kSymFullBreakWithCaps, height:0, rightSide = flipH = (rotation is
       opposite to the wall direction)})` **before** the insert is in the
       wall (`offset` = `(x1, y1)` projected onto the wall axis, clamped);
       checked with `ForEachBreakN` (theSymbol = insert) and the parent chain.
       New symbol breaks that do not reference the insert are deleted again.
    2. When the insert is still not inside the wall: `AddObjectToContainer`
       (the pre-714c15d behaviour; failure to attach is still an error).
    3. Still no break: `ISDK::SetObjectWallOffset(insert, wall, offset)`,
       re-checked. (`SetObjectAsSpanWallBreak` was not used: it changes the
       insert to a centred spanning break.)
    4. After the insert's own parameter reset the break is read back only.
    Without a break the create succeeds with a `warnings` entry starting
    `"wallBreakRegistered=false: …"` that lists each attempt (AddBreak return
    code, parent state, SetObjectWallOffset/GetObjectWallOffset results) and
    says the opening may be dropped when the wall is regenerated and that the
    MCP host creates doors/windows with `vs.AddSymToWall` (which registers the
    break) — the host's `vw_create` uses that path; raw `apply_operations`
    keeps the native path. A failed create still removes any break of the
    insert before the insert (`DeleteInsertAndBreak`). The commit verifier no
    longer checks the break. The window `ElevationSetAt = "Sill of Window"`
    fix below is unchanged. Live acceptance: `wall_inserts_have_break_records`
    and `wall_inserts_survive_*` are informational (WARN) for this path.
  - **window sill**: native window creates set `ElevationSetAt` to
    "Sill of Window" before `Elevation` (default "Head of Window" put the head
    at the sill height, i.e. the window below the floor); when Vectorworks
    rejects the value (readback differs, e.g. a localized popup) the previous
    value is restored and a warning is added. The host's same-transaction
    fix-up (`update_parametric ElevationSetAt + Elevation`) stays compatible.
  - door / window / hosted `parametric` creates accept
    **`wall_uuid:"$<local_ref>"`** of a wall created earlier in the same
    transaction (walls + openings in ONE undo step); `wall_uuid:"uuid:<u>"` is
    accepted too. A `$ref` that is not declared by an earlier operation fails
    at parse time (`invalid_request`, nothing ran).
  - multiple `name:` targets are resolved with one document traversal; a
    single one uses `GetNamedObject`.
  - `object.delete` registers undo exactly once: `DeleteObject(h, true)`
    moves the object into the open undo event; no `AddBeforeSwapObject`
    (an extra before-record would restore it twice; before-record +
    `DeleteObject(h, false)` did not restore it at all). One Undo brings back
    exactly one object with the same uuid. Consequently a plan may not delete
    an existing object that an earlier op of the same plan changed (or that
    hosts an insert created in it) → `invalid_request`; use two calls. The op
    result adds `"cascaded_inserts":[uuid…]` (doors/windows/symbols deleted
    with a wall).
  - **automatic wall joins** (`join_walls`, plan-level, default `true`; a
    wall `create` op may set `"join_walls":false` to opt out): walls created
    in the plan are joined inside the same undo event before any later op
    (door/window insertion, edits) and before commit — L join where two
    endpoints coincide (≤ 1 mm), T join where an endpoint lies on another
    wall's centreline (collinear continuations are left alone). ISDK
    `JoinWalls(first, second, firstPt, secondPt, kLWallJoin|kTWallJoin,
    capped:false, showAlerts:false)` (identical in SDK 2024/2025; each wall's
    midpoint picks the portion to keep). Result: `transaction.wall_joins` and
    a top-level copy `wall_joins`:
    `[{"first_uuid","second_uuid","kind":"L"|"T","point":[x,y],"joined":bool}]`.
  - idempotency identity is `<bridge session>:<document fingerprint>`.
  - parametric parameter readback is tolerant: reals compare with a relative
    1e-6 tolerance; string values that both parse as numbers ("900",
    "900.0", "900mm") compare numerically; `true/1/yes` and `false/0/no` are
    equal. A remaining mismatch names the written and read-back values.
  Result `{ok:true, atomic:true, verified:true, replayed, idempotency_key?, transaction:{…}, wall_joins:[…], sent_to_back:n}`.
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
| `VW_MCP_AUTO_DISMISS_HOME=0` | do not auto-close the Home screen / update notice (§3.1); `dismiss_home` still works |
