# Vectorworks MCP 1.0 — implementation contract

Status: binding spec for the 1.0 rewrite (2026-10-02). Three work streams build
against it in parallel: **N** native bridge (`native_bridge/`), **H** Python host
(`vectorworks_mcp/`), **I** install / client integration / docs / skills.
If a stream must deviate, it records the deviation in the "Deviations" section at
the bottom of this file instead of silently diverging.

Goals: never drops the connection (multi-document, multi-instance, Vectorworks
restarts, busy UI); fast; usable by weak models; full capability including visual
capture so computer-use is never needed; current MCP spec (2026-07-28, dual-era via
`mcp` 2.2.x); Codex on Windows first, Claude Code second.

---

## 1. Native wire protocol v2 (capability_revision 6)

### 1.1 Transport (unchanged framing)
- 4-byte big-endian length + UTF-8 JSON, max 16 MiB, many sequential requests per
  connection.
- Request: `{"id", "action", "auth_token", "params": {...}, "timeout_ms"?: int}`.
- Response success: `{"id", "success": true, "result": <json>}`.
- Response failure: `{"id", "success": false, "error": "<message>", "error_code"?: str,
  "retryable"?: bool, "commit_state"?: "not_started"|"rolled_back"|"committed"|"unknown",
  "details"?: obj}`. `error` stays a string so old hosts keep working. Hosts must also
  accept the legacy form where `error` is a JSON-encoded object string.
- Full `\uXXXX` + surrogate-pair decoding in the JSON parser.

### 1.2 Listener, ports, discovery
- Bind with `SO_EXCLUSIVEADDRUSE` (never `SO_REUSEADDR`). Try 9877, then 9878…9886.
  `VW_MCP_PORT` env pins one port (no fallback).
- On successful bind write `%USERPROFILE%\.vectorworks-mcp\instances\<pid>.json`
  atomically (write temp + rename):
  `{"pid", "port", "protocol": 2, "capability_revision": 6, "bridge_version",
  "build_id", "vectorworks_version": "2024"|"2025", "session_id", "started_at"}`.
  Delete it on module deinit / clean shutdown. Hosts prune files whose PID is dead.
- Bind failure: exponential back-off (2 s → 60 s cap), log rate-limited.
- `Start()` must join/reap previous listener and client threads before creating new
  ones (no `std::thread` assignment to a joinable thread).
- Up to 16 concurrent clients; over the cap send an error frame
  (`error_code:"too_many_clients"`, retryable) before closing. TCP keepalive on. Idle
  client sockets closed after 120 s of no traffic.
- Handle `kPluginModuleDeinit`: stop pump, close sockets, join threads, delete
  discovery file. All window-proc/timer entry points wrapped in `try/catch(...)`.
- Pump window is a message-only window (`HWND_MESSAGE` parent).
- Plug-in is renamed from the SDK sample `ObjectExample` to `VectorworksMCPBridge`
  (`VectorworksMCPBridge.vlb` + `.vwr`); sample tool/object registrations removed. The
  build project lives in version control (not only in the ignored `worktree/`).
- `build_id` = git short hash + build UTC timestamp, baked in at compile time.

### 1.3 Socket-thread actions (answered without the main thread)
- `ping` → existing fields plus: `protocol: 2`, `capability_revision: 6`,
  `bridge_version`, `build_id`, `pid`, `port`, `session_id`, `vectorworks_version`,
  `ui_state` (see 1.4), `handler_running_ms` (0 when idle), `queue_depth`,
  `main_context_idle_ms`, `active_document` (`{fingerprint, name, path, dirty}` or
  null; cached by the main thread each tick), `open_document_count`.
- `capabilities` → existing manifest plus `actions` list with per-action
  `{name, mutates, long_running, supports_async}`.
- `stop` → **no longer stops the bridge**. It closes only the calling connection and
  returns `{"connection_closed": true, "bridge_running": true}`.
- `job_status {job_id}` → `{job_id, action, state: queued|running|done|failed|cancelled,
  started_at, elapsed_ms, result?, error?, error_code?}`. Jobs retained 15 min / 64 max.
- `cancel_job {job_id}` → cancels a queued job (running jobs cannot be interrupted;
  report `state:"running", cancellable:false`).

### 1.4 Main-thread gate (UI safety)
`ui_state` is one of `idle`, `modal` (main frame disabled / modal dialog up),
`tracking` (mouse captured / button down / menu loop / tool drag), `loading`
(between doc-changing/pre-open notifications and their completion), `no_document`.
- CAD requests are dequeued only in `idle` (and `no_document` for actions that do
  not need a document: `list_documents`, `new_document`, `open_document`,
  `capabilities`-like reads).
- If the gate stays closed longer than the request's `busy_wait_ms` (default 3000),
  fail with `error_code:"vectorworks_busy"`, `retryable:true`,
  `commit_state:"not_started"`, `details.ui_state`.
- Requests needing a document when none is open fail with `no_active_document`.

### 1.5 Timeouts and async jobs
- Socket thread waits `timeout_ms` (request field; default 25000, max 600000) for a
  queued request. If the request has not started by then it is removed and fails
  `timeout_not_started` (`commit_state:"not_started"`, retryable). If it already
  started, the handler is converted into a job and the response is
  `success:false, error_code:"still_running", details.job_id` — never a silent drop.
- Long actions (`export_pdf`, `export_dwg`, `import_dwg`, `export_ifc`, `import_ifc`,
  `open_document`, `export_vectorworks_document`, `run_script` with `async:true`,
  `capture_view` with a render mode other than wireframe/OpenGL) accept
  `params.async: true` → immediate `{"job_id", "state": "queued"}`.

### 1.6 Documents, identity, multi-document
- `document_fingerprint` must be stable for the lifetime of an open document,
  including **untitled** documents, and must differ between two simultaneously
  open documents. (Implementation suggestion: map drawing-header handle → generated
  id, invalidated on doc-close notification; saved docs may hash their path.)
- Every request's params may carry `expect_document_fingerprint`. Before running:
  if the active document differs → if `activate_document: true` and the expected
  document is open, `SwitchToOpenFile` to it and continue; otherwise fail
  `document_changed` with `details.active_document` and `details.open_documents`.
- Subscribe to document change/close notifications; bump `document_generation`
  eagerly.
- New actions:
  - `list_documents` → `{documents:[{fingerprint, name, path, dirty, active,
    file_ref}]}` (paths converted from Mac encoding to Windows).
  - `activate_document {fingerprint | path | name}` → `{active_document}`.
  - `new_document {template_path?}` → creates an untitled document without dialogs
    if the SDK allows; otherwise `error_code:"unsupported"`.
  - `close_document {fingerprint, discard_changes?: "discard"}` → refuses a dirty
    document unless `discard_changes == "discard"`; never shows a save prompt.
  - `open_document` keeps current params, becomes a job (1.5) and reports dialogs
    as `vectorworks_busy` instead of hanging.
  - `save_document` must use `SaveActiveDocumentPath` for untitled documents (no file
    chooser ever).

### 1.7 Object references
- Raw `MCObjectHandle` values are never dereferenced from client input. `handle:`
  refs map through a table `{handle_id → uuid, document_fingerprint}`, re-resolve by
  UUID, and fail `stale_reference` on mismatch. Table cleared per document close.
- Every object record and every create/duplicate receipt includes `uuid`.
- Deletes: verify no double undo registration (`AddBeforeSwapObject` + `DeleteObject`
  with undo=false); live-verify undo/redo restores exactly once.

### 1.8 View and capture
- `get_view` → `{standard_view, projection, render_mode, zoom, center:[x,y],
  view_matrix?, active_layer, active_layer_kind: design|sheet, screen_px:[w,h],
  dpi}`.
- `set_view {standard_view?, projection?, render_mode?, zoom?, center?,
  fit?: "drawing"|"selection"|"objects"|"layer", object_uuids?, margin_pct?,
  saved_view?, layer?}` → computes fit from object bounds (`SetViewCenter` +
  `SetZoomFactorN`); **no `DoMenuName`, no select-all side effects**. Returns
  `{previous, current}`.
- `capture_view {path, format: png|jpg, fit?, object_uuids?, margin_pct?,
  standard_view?, projection?, render_mode?, layer?, saved_view?, restore: true,
  update_viewports: false, reset_plugin_objects: false, rect?}` → applies the
  temporary view, `DrawScreen`, `ExportImage` with `fExportRect` when a rect/fit is
  given, then restores view, layer and selection exactly when `restore`. Returns
  `{path, width, height, bytes, format, restored: bool, view_used}`.
- `saved_views {action: list|restore|create|delete, name}` (create/delete may use the
  in-process script engine when the SDK lacks a non-UI call).

### 1.9 Organisation: layers, classes, stories
- `layers {action: list|create|set|delete, ...}`: list returns design + sheet layers
  with `uuid, name, kind, visible (show|gray|hide), active, scale, elevation, story,
  opacity, object_count`. `create {name, kind, scale?, elevation?, title?}`,
  `set {target (uuid|name), name?, visibility?, active?, scale?, elevation?,
  opacity?}`, `delete {target, confirm:"delete"}` (refuse non-empty unless
  `force:true`).
- `manage_classes` gains `set {name, visibility?, active?, fill_color?, pen_color?,
  line_weight?, use_at_creation?}` and `list` returns visibility + active.
- `stories {action: list|create|set}` when the SDK supports it; else `unsupported`.

### 1.10 In-process scripting (universal escape hatch)
- `run_script {language: "python"|"vectorscript", code, undo_label?,
  transaction: true, async?: false}` runs on the main thread through
  `IPythonScriptEngine` / `IVectorScriptEngine` — never a modal listener. With
  `transaction:true` the whole script is one undo event; on exception it is rolled
  back. Python scripts get `vs` pre-imported and may assign `result` (JSON-serialised
  into the response). Capture stdout/stderr (≤256 KiB each). Suppress VS error
  dialogs. Returns `{ok, result, stdout, stderr, error?, rolled_back, duration_ms}`.
- Disabled when env `VW_MCP_ALLOW_SCRIPT=0` in the Vectorworks process
  (`error_code:"script_disabled"`).

### 1.11 Other new/changed actions
- `export_ifc {path, async?}` / `import_ifc {path, async?}` via the non-UI SDK/VS
  calls; `export_pdf` gains `range: current_view|all_pages|sheet` + `sheet` + `dpi`.
- `group {action: group|ungroup, targets}` and `records` (list formats, attach, get,
  set fields) — native if cheap, else implemented by the host through `run_script`.
- Paged native reads for `resources`, `symbol list`, `worksheet list` (offset/limit,
  `next_cursor`).
- Capture/export defaults: `update_viewports=false`, `reset_plugin_objects=false`.
- `IsActiveFileChangedAfterLastSave` guarded for SDK 2025; both 2024 and 2025 must
  compile (2025 build optional if SDK2025 libs are missing — document it).

---

## 2. Host (Python) — package `vectorworks_mcp/`

- Python ≥3.10 (target 3.12 from python.org, never Codex's bundled runtime).
  Pins: `mcp==2.2.0`, `fastmcp==4.0.10`, `pydantic` matching. `server.py` stays as a
  thin shim (`from vectorworks_mcp.server import main`).
- Startup: no network, no pip, no venv work, nothing heavy imported eagerly
  (`plan_quality` lazy). The server starts and answers `initialize`/`tools/list`
  even when Vectorworks is not running, config is invalid, or the token is missing;
  such problems surface through `vw_status`. Target cold start < 2 s.
- Logging only to stderr + rotating file under `%USERPROFILE%\.vectorworks-mcp\logs`.
  Never write to stdout except MCP frames.

### 2.1 Bridge client
- Instance discovery from `instances/*.json` (prune dead PIDs), falling back to
  port 9877 for pre-1.0 bridges. One instance → use it. Several → use the pinned one
  (`instance` arg or last used), else the one whose active document matches the
  requested document, else return `multiple_instances` with the list.
- Per-instance persistent connection, reopened transparently; idle sockets closed
  after 30 s. Auth token re-read on `auth_failed` then retried once (nothing ran).
- Per-call budgets below the client's tool timeout: reads 20 s, writes 45 s; long
  operations use async jobs polled every 0.5–2 s with `ctx.report_progress`, total
  ≤ 170 s per tool call, then return `still_running` + `job_id` (the model calls
  `vw_status(job_id=…)`).
- Lock acquisition with timeout → `vectorworks_busy` instead of queuing forever.
- Reads retry once on transport errors; writes never auto-retry; lost write
  responses are resolved through `transaction_status` by idempotency key.
- Pre-1.0 bridge (capability_revision ≤5): keep working with the subset it supports;
  missing actions → `upgrade_required` with the exact install command.

### 2.2 Document pinning
- The host remembers the last document fingerprint the model observed. Writes
  automatically send `expect_document_fingerprint` of that document; on mismatch the
  result is `document_changed` with the active + open documents and a `next_step`.
- Every document-touching tool accepts optional `document` (fingerprint, file name
  or path) → host resolves via `list_documents` and sends
  `expect_document_fingerprint` + `activate_document:true`.

### 2.3 Tool surface (default profile)
Names are final. Each tool has accurate annotations (`openWorldHint:false`
everywhere; read-only tools `readOnlyHint:true`), a typed input schema with
`additionalProperties:false`, an `outputSchema`, and a description ≤ 600 chars
with one inline example. Total `tools/list` budget ≈ 6k tokens.

| Tool | Kind | Purpose |
|---|---|---|
| `vw_status` | ro | health, instances, documents, ui_state, setup problems, job status (`job_id`), transaction status (`idempotency_key`). Never throws. |
| `vw_read` | ro | `what`: document, documents, layers, classes, summary, query, object, selection, view, saved_views, sheets, viewports, annotations, symbols, worksheets, worksheet, resources, records, parametric_schema, capabilities, plan_quality |
| `vw_capture` | ro* | image of view / drawing / selection / objects / layer / sheet / saved view, optional temporary standard view + render mode; returns MCP `image` (≤1568 px long edge, JPEG/PNG) + `resource_link` to full-res file. View restored. (*restores state; annotate readOnly, idempotent) |
| `vw_create` | write | `objects:[{type, ...}]` typed, flat, unit-aware; one atomic undo step; `id` + `$id` refs (doors/windows into `wall:"$w1"`). |
| `vw_edit` | write, destructive | `changes:[{op: set|move|rotate|scale|mirror|reshape|duplicate|delete|parametric|class|layer|group|ungroup|record, target(s), ...}]` atomic. |
| `vw_apply` | write, destructive | raw mixed create+edit transaction (legacy grammar of 0.7, kept for power users/back-compat). |
| `vw_view` | write (non-destructive) | standard view, projection, render mode, zoom/center/fit, saved view restore/create, active layer. |
| `vw_organize` | write | layers, classes, stories, saved views, worksheets (cell write), symbols/resources import. |
| `vw_document` | write, destructive | list/activate/new/open/save/save_as/close/export(pdf/dwg/ifc/image/vwx)/import(dwg/ifc). |
| `vw_script` | write, destructive | run Python (`vs.*`) or VectorScript in-process, one undo step, rollback on error. Escape hatch for anything not covered. |

`vw_execute_operations` and `vw_tool_safety` are removed from the default profile.
The 0.7 server is preserved verbatim as `legacy/server_v07.py`, reachable only with
`VW_MCP_TOOL_PROFILE=legacy` (no other compat profile).

### 2.4 Forgiving input (weak models)
- Case-insensitive enums and common synonyms (`rectangle`→`rect`, `op`↔`type`).
- Numbers as strings; lengths as `"3m"`, `"3000 mm"`, `"12'6\""`; points as `[x,y]`,
  `{"x":..,"y":..}` or `"x,y"`; stringified JSON arrays/objects parsed.
- Out-of-range `limit` clamped (reported in `warnings`), not rejected.
- `idempotency_key` optional: derived from plan hash + document fingerprint + 60 s
  bucket when omitted.
- Every error result: `isError:true`, `structuredContent: {ok:false, error:{code,
  message, retryable, next_step, commit_state?}}`. Validation errors use the same
  envelope with the offending field and an example of the correct shape.
- `isError` is exactly `ok is False`.

### 2.5 Resources and prompts
- Resources: `vw://guide` (≤2k-token usage guide for models), `vw://status`,
  `vw://documents`, `vw://capabilities`, `vw://capture/{name}` (captured images).
- Prompts: `draw-floor-plan`, `inspect-drawing`, `export-sheets`, `fix-connection`.

---

## 3. Install & client integration

- Runtime root (real filesystem, not virtualised AppData):
  `%USERPROFILE%\.vectorworks-mcp\` →
  `runtime\<version>\` (code + `venv\`), `current.txt` (active version),
  `bin\vectorworks-mcp.cmd` (launcher), `instances\`, `logs\`, `captures\`,
  `auth-token`, `install-receipt.json` (version, file hashes, .vlb hash, time).
- Launcher `.cmd` runs `"<runtime>\venv\Scripts\python.exe" -X utf8 -u -m
  vectorworks_mcp` directly — no PowerShell, pip, git or resolver at launch. If the
  runtime is missing it starts a tiny stdlib-only "repair mode" MCP server
  (`vectorworks_mcp/repair_server.py`, no third-party imports) exposing only
  `vw_status` that reports `setup_required` + the repair command — so the client
  never shows "connector unavailable".
- Installer refuses to write into `%LOCALAPPDATA%\Packages\*\LocalCache` (detect MSIX
  redirection and abort with a message).
- Registration (backups first): Codex `~/.codex/config.toml`
  `[mcp_servers.vectorworks]` → `command = '<bin>\vectorworks-mcp.cmd'`,
  `startup_timeout_sec = 30`, `tool_timeout_sec = 180`; remove duplicate plugin
  server definitions. Claude Code: `claude mcp add -s user vectorworks -- <launcher>`.
  Plugins (Codex + Claude) become skills-only or point at the same launcher.
- Native install: copy `VectorworksMCPBridge.vlb/.vwr` to
  `%APPDATA%\Nemetschek\Vectorworks\<ver>\Plug-ins`, remove the old
  `ObjectExample.vlb/.vwr` (backup), refuse while Vectorworks is running unless asked
  to wait.
- `vectorworksctl doctor` checks all of the above plus measured host startup.
- One skill (`vectorworks`) for Codex and Claude: first call, tool cheat-sheet,
  recipes (draw plan, place doors, capture & verify, export sheets, multi-doc,
  recovery), generated tool map checked against `tools/list`.

---

## Deviations
(Record here: stream, section, what changed, why.)
