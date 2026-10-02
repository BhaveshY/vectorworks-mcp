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

- **H, §2 (framework, "FastMCP 4.0.10 app"):** the default host does not import FastMCP/mcp/pydantic. Importing them
  costs ~1.8 s on a warm Windows machine, and Codex omits servers not ready ~1 s after thread start
  (`mcp_optional_startup_grace_ms`). `vectorworks_mcp/mcp_core.py` is a stdlib JSON-RPC stdio core serving static tool
  data: handshake era (initialize / notifications/initialized / ping, 2024-11-05…2025-11-25) and 2026-07-28
  (`server/discover`, per-request `_meta` protocol version, `resultType`/`ttlMs`/`cacheScope`, -32022), progress via
  `_meta.progressToken`, `notifications/cancelled`, resources, prompts, logging/setLevel. Input "schema validation" is
  the forgiving coercion layer. Measured first `tools/list` ≈ 130 ms cold. `fastmcp==4.0.10`/`mcp==2.2.0` stay pinned
  for `VW_MCP_TOOL_PROFILE=legacy` and the MCP-client acceptance scripts. The process exits only on stdin EOF
  (SIGINT/SIGBREAK ignored).
- **H, §2.3 vw_create `$id` hosting:** for doors/windows the host sends `wall_uuid: "$<local_ref>"` inside the single
  `apply_operations` on revision ≥ 6 — **N must resolve local refs in `wall_uuid`** (as it does for `target`). On
  revision ≤ 5 the host runs two transactions (walls, then openings with real UUIDs) and warns "two undo steps".
- **H, §2.3 vw_edit:** `mirror`, `layer`, `group`, `ungroup`, `record` run through the vetted `run_script` snippet
  `scripts_lib/edit_engine.py`; when mixed with native ops they form a second undo step (warned). Extra ops `select` /
  `deselect` (0.7 `vw_selection` parity). Target `"selection"` expands to the selected objects' UUIDs.
- **H, §2.3 surface additions (parity with the 0.7 47-tool profile, see docs/PARITY.md):** `vw_status(detail,
  disconnect)`; `vw_read` filters `type/layer/class/name/criteria/query/resource_type/range`; `vw_create(floor_plan,
  dry_run)` + composite `room` type (0.7 schematic/BIM floor-plan tools); `vw_document(action="info")`;
  `vw_organize(kind="resource", action="import")`. vw_status never fails: job/transaction lookup errors are reported
  inside `job` / `transaction` with `ok:true`.
- **H, §2.1 object by uuid:** the contract has no get-by-uuid read, so `vw_read(what="object")` scans `query_objects`
  pages (≤ 100 × 1000). Suggestion for N: a `uuid` filter on `query_objects`.
- **H, §2.1 lost write replies:** resolved through `transaction_status` only for `apply_operations`-based tools
  (vw_create/vw_edit/vw_apply, which always carry an idempotency key); other lost writes return `unknown_commit_state`.
- **H, §2.1 pre-1.0 capture:** on revision ≤ 5 `vw_capture` exports the current view as-is (the old fit used
  select-all / menu fit and changed the user's selection); fit/standard view/render mode need revision 6.
- **H, files:** `plan_quality.py`/`plan_geometry.py` moved to `vectorworks_mcp/analysis/` (lazy import);
  `vw_listener.py` and `production_apartment.py` moved to `legacy/`; `legacy/server_v07.py` is the 0.7 server with only
  its `plan_quality` import path changed. The generated per-user `vw_load_listener_2024.py` /
  `vw_start_listener_2024.py` (git-ignored) were left in place.
- **H, errors:** extra codes beyond the contract list: `path_error`, `confirmation_required`, `script_error`,
  `job_not_found`, `upgrade_required`, `multiple_instances`, `document_not_found`, `native_rejected`, `native_error`,
  `internal_error`. `next_step` for `upgrade_required` uses `VW_MCP_INSTALL_COMMAND` when set.
- **I, §3 client command:** both clients start the launcher as `C:\Windows\System32\cmd.exe /d /c
  <root>\bin\vectorworks-mcp.cmd` instead of the bare `.cmd` path. Node-based clients cannot spawn a `.cmd` without a
  shell, and `/d` skips cmd AutoRun hooks that could print to stdout and corrupt the MCP stream. Codex additionally gets
  `env_vars` (USERPROFILE, APPDATA, LOCALAPPDATA, TEMP, TMP, SystemRoot, PATH, PATHEXT, ComSpec), `enabled = true`
  and the top-level `mcp_optional_startup_grace_ms = 5000` (only raised, never lowered). No client config sets
  `VW_MCP_PORT` (it would disable instance discovery).
- **I, §3 launcher:** runs `python.exe -I -X utf8 -u -m vectorworks_mcp` (`-I` added: the client's cwd, e.g. a repo
  checkout containing `vectorworks_mcp/`, must not shadow the installed package; PYTHON* env and user site are
  ignored). The code lives in `runtime\<id>\app\` and is put on `sys.path` by a `.pth` in the venv. The launcher
  derives the root from its own location, reads `current.txt`, sets `VW_MCP_STATE_DIR` (if unset) and
  `VW_MCP_INSTALL_COMMAND` (baked: the exact install/repair command for this machine). Repair mode uses the base
  interpreter recorded at install time, then `py -3`, then `python`; `bin\repair_server.py` is a copy of
  `vectorworks_mcp/repair_server.py`.
- **I, §3 runtime id:** `current.txt` holds a runtime id, normally the version; when `runtime\<version>` is in use by
  a running client during a reinstall, the new runtime is installed side by side as `<version>-<yyyyMMdd-HHmmss>` and
  older ids are removed by a later install once unused.
- **I, §3 Python:** the installer requires Python >= 3.11 (python.org; 3.12 preferred via `py -3.12`) because the
  registration helper uses `tomllib`; Codex's bundled runtime and WindowsApps/LocalCache interpreters are refused.
- **I, §3 plugins:** both plugins are skills-only (no `mcpServers`). Codex: the `vectorworks-mcp` marketplace is
  re-pointed to the local copy `%USERPROFILE%\.vectorworks-mcp\marketplace` (also a Claude marketplace) and refreshed
  with `codex plugin add vectorworks@vectorworks-mcp`; while the cached plugin still defines a `vectorworks` server it
  is disabled so exactly one server is active. Claude Code: the installer also runs `claude plugin marketplace add
  <root>\marketplace` + `claude plugin install vectorworks@vectorworks-mcp`. The org-synced Claude desktop (Cowork)
  plugin cannot be changed locally; doctor reports it and `package-release.ps1` builds the replacement zip.
- **I, §3 native install default:** from a release package the prebuilt plug-in is installed by default (Vectorworks
  closed); from a git checkout only with `-FullNative` (a checkout may hold a stale dev build). Artifacts are looked up
  in `-NativeArtifactDir`, `native\<ver>\` (release zip) and `native_bridge\project\out\<ver>\Release\`. The old
  `ObjectExample.vlb/.vwr` is removed only when it references `.vectorworks-mcp` / `VW_MCP_AUTH_TOKEN` (i.e. it is the
  0.x bridge, not the SDK sample).
- **I, §3 skills:** one skill `vectorworks` (`plugins/vectorworks/skills/vectorworks/` with `SKILL.md`,
  `references/tool-map.md`, `agents/openai.yaml`); the 0.x setup/ping/diagnose/work skills were removed.
  `scripts/check-skill-tool-names.py --live` checks tool names *and* example argument names against `tools/list`.
- **N, §1.4 notifications / `loading`:** a module that registers no extensions has no SDK notification sink, so
  `kNotifyDocChanging`/`kNotifyDocPreOpen` are not subscribed. Instead the main thread polls `GetOpenFilesList` + the
  drawing header (≤ 200 ms while idle and before every request) and bumps `document_generation` eagerly. `loading` =
  a bridge-initiated open/new/close in progress, a 250 ms settle window after a switch/close/open was observed, or the
  main frame missing/hidden (startup). User-initiated loads show as `modal` (progress/convert dialogs) or block the
  pump (`main_context_idle_ms` grows). `tracking` uses `GetGUIThreadInfo` (menu/popup/move-size), `GetCapture` and
  mouse-button state while a Vectorworks window is foreground (no tool-mode notification).
- **N, §1.2 deinit:** Vectorworks may deinit a module with no registered interfaces right after init, so
  `kPluginModuleDeinit` performs the full shutdown (stop pump, cancel queue, close sockets, join threads, delete
  discovery file) only when the main frame was seen and is gone; otherwise it is logged and ignored (the DLL stays
  pinned, as in 0.x). A static destructor deletes the discovery file at process exit. Init returns `kDoNotCache`.
- **N, §1.2 build:** project `native_bridge/project/VectorworksMCPBridge.vcxproj` (SDK via `VWSDKRoot`, default
  `third_party/VectorworksSDKExamples/VectorworksSDK/SDK<ver>`); output
  `native_bridge/project/out/<ver>/Release/VectorworksMCPBridge.{vlb,vwr}`. Both 2024 and 2025 compile (2025 libs
  present). `build_id` = `<git short hash>[-dirty]-<yyyyMMddTHHmmssZ>`.
- **N, §1.3 ping:** `main_context_pump` keeps the 0.x value `"win32_ui_timer"` (the 0.7 host and smoke.py require it);
  the message-only window is reported as `main_context_pump_window:"message_only"`. Extra fields: `ui_state_reason`,
  `running_action`, `job_count`, `client_count`, `git_hash`, `started_at`, `script_enabled`. `main_context_idle_ms` is
  -1 before the first tick.
- **N, §1.5:** `async:true` is accepted by every main-thread action (`supports_async` true for all of them;
  `long_running` marks the slow ones). `capture_view` is not flagged long-running by render mode in the registry; its
  result carries `slow_render_mode`. `timeout_ms` is also read from `params.timeout_ms` when the top-level field is
  absent. Jobs report `blocked_by_ui` + `hint_error_code:"vectorworks_busy"` while a dialog blocks a running job (a
  running handler cannot be failed with `vectorworks_busy`).
- **N, §1.6 new_document:** the SDK has no dialog-free "new" call; the bridge opens a blank template (`template_path`,
  `VW_MCP_NEW_DOCUMENT_TEMPLATE`, or `<VW>\Libraries\Defaults\Templates\Blank (Metric|UK Metric|Imperial).sta`, then any
  blank/leer/… template) with `OpenDocumentPath(..., false)` and reports `untitled`; `unsupported` when none exists.
- **N, §1.6 dirty / close:** in Vectorworks 2025 `dirty` is `null` (API removed), so `close_document` there always needs
  `discard_changes:"discard"`. With discard, a dirty document is first saved to a private temp file and then closed,
  so `CloseDocument` can never prompt (original file untouched). `open_document`'s `replace_dirty_confirmation` is no
  longer required (documents open side by side); the open runs synchronously and becomes a job on timeout.
- **N, §1.6 fingerprints:** keyed by `GetOpenFilesList` `fileRef` (+ active drawing header). If Vectorworks ever
  changed a document's fileRef (not observed in the SDK docs) its fingerprint would change too.
- **N, §1.7 handles:** handle ids are `h<N>` (opaque); objects without a UUID get `h0-<N>` ids that never resolve
  (`stale_reference`). 0.x `0x…` ids are rejected as `stale_reference`.
- **N, §1.8 fit:** bounds fill ≤ 85 % of the drawing area; the drawing area is derived from the view centre
  (`WorldPt2ViewPt`) because `GetScreenSize` may include palettes. Zoom is read from program variable
  `varWindowZoomFactor` (500, "internal"); fit is exact in plan views and "projected best effort" in 3D views (view
  matrix). Capture crops (`fExportRect`) only in plan views or with an explicit `rect`.
- **N, §1.8 saved views:** create/delete use in-process `vs.SaveSheet` / `vs.VDelete`; `capture_view` with
  `saved_view` snapshots class/layer visibility through a temporary saved view (created and deleted around the capture).
- **N, §1.9:** layer `elevation`/`thickness`/`opacity` read/write go through in-process `vs.GetLayerElevation`,
  `vs.SetLayerElevation`, `vs.HGetLayerTransp`, `vs.HSetLayerTransp` (one batched script per list page). `stories list`
  discovers stories through their layers (stories without layers only counted in `sdk_story_count`). `manage_classes
  list` keeps returning the 0.x array of names; the detailed form needs `detailed:true`. Class `set` uses `vs.*` class
  calls in one undo step.
- **N, §1.10:** VectorScript runs return `result:null` and no stdout. `rollback_verified` compares layer count + top-level
  object counts (cheap structural check, not a UUID diff). Rollback depends on Vectorworks registering `vs.*` creations
  in the bridge's open undo event — must be live-verified.
- **H+N, §1.10 crash class:** wrong-typed `vs.*` arguments can crash Vectorworks (live: `SetClFillFore` with a palette
  index). `vw_script` (Python) lints every `vs.*` call against `vectorworks_mcp/data/vs_signatures.json` before sending
  (`script_lint_failed`, nothing sent; `lint:false` bypasses); the native bridge contains hardware faults in the
  script engine with an SEH guard (`script_crashed`, commit_state unknown), then fails scripts fast with
  `script_engine_unavailable`; `ping.script_engine` / `vw_status` problems report `poisoned` (PROTOCOL.md §6.4.1,
  `native_bridge/VS_CALL_AUDIT.md`).
- **N, §1.11:** `group`/`ungroup` and `records` are not native in 1.0 (host `run_script` snippets). `query_objects` gained
  a `uuids`/`uuid` exact-lookup filter (H suggestion) and a `max_bytes` page budget. `apply_operations` resolves
  `wall_uuid:"$<local_ref>"` (H request) for doors/windows/hosted parametric objects in one transaction.
- **N, mock:** `native_bridge/mock/` only held a stale `.pyc` (source removed in 73cf2f9 "live e2e only"); it was not
  resurrected because H ships `vectorworks_mcp/testing/mock_bridge.py`. Core transport/scheduler/protocol behaviour
  (timeouts, jobs, cancel, busy gate, no_document, too_many_clients, stop, idle close, oversize response, \u decoding,
  exclusive port fallback) was verified with a throwaway SDK-free harness, not committed.
