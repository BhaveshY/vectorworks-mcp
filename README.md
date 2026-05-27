# Vectorworks 2024/2025 MCP Connector

Connect Claude Code to Vectorworks on the same Windows PC.

## Architecture

```text
Claude Code <--stdio--> scripts/run-mcp-server.ps1
                         |
                         v
                      server.py <--TCP/JSON--> vw_listener.py (modal agent session inside Vectorworks)
                                             127.0.0.1:9877
```

`scripts/run-mcp-server.ps1` is the self-bootstrapping entrypoint. It creates a
repo-local `.venv`, installs bounded host dependencies from `requirements.txt`, then starts `server.py`. The
generated Vectorworks launcher sets `VW_MCP_MODE=dialog`, the only pure-Python
mode currently safe for real `vs.*` API calls. It opens a modal agent-control
dialog; close or stop it when you want to use Vectorworks manually.

The host MCP server also enforces CAD safety metadata. Tools that touch the
document automatically require a fresh or very recent safe bridge status and
return a structured `blocked: true` response instead of forwarding CAD work to
transport-only or legacy listeners.

## Bridge Status

| Bridge path | Status | Real CAD/API handlers | Manual Vectorworks UI |
|-------------|--------|-----------------------|------------------------|
| Python `dialog` listener | supported fallback | yes | modal agent session |
| Python `foreground` listener | legacy diagnostic only | no, guarded | blocks UI |
| Python `background` listener | diagnostic only | no, guarded | no reliable scheduling |
| Python `win_timer` listener | diagnostic only | no, guarded | transport ping only |
| Native SDK bridge | planned scaffold | intended yes | intended non-modal |

The proper long-term fix for non-modal, always-on control is a native
Vectorworks SDK plug-in bridge. The SDK bridge is scaffolded in
`native_bridge/`, but it is not compiled or installed by default. Until that
bridge exists, use the generated dialog launcher for real CAD work.

Why this is not as simple as a Revit-style setup yet:

- Revit has a mature add-in loading model and documented mechanisms such as
  external commands/events for getting work back onto Revit's valid API context.
- Vectorworks 2024 can run Python scripts, but a long-lived Python socket loop
  either blocks the UI or loses the safe document/API context after the script
  returns.
- The safe pure-Python fallback is therefore a modal dialog agent session. It is
  stable for CAD operations, but you close it when you want manual Vectorworks
  control back.
- A Revit-like always-on Vectorworks experience needs the native SDK plug-in
  bridge: worker-thread networking plus strict marshaling back to the
  Vectorworks main/plugin event context.
- The repo cannot silently build that bridge on a fresh Windows machine until
  the official Vectorworks SDK and Visual Studio C++ build tools are installed.

Native bridge planning aids:

- `native_bridge/HANDLER_MATRIX.md` maps every current listener action to native
  phase, safety, and smoke-test expectations.
- `native_bridge/mock/mock_bridge.py` is a no-SDK protocol harness proving the
  host MCP server and preflight logic can talk to a future native bridge.
- `scripts/prepare-native-bridge-source.ps1` prepares an ignored SDK-backed
  source worktree from Vectorworks' official SDK examples.
- `scripts/copy-native-bridge-scaffold.ps1` copies the reviewed no-SDK native
  scaffold into that worktree after the unmodified SDK example builds.
- `scripts/wire-native-bridge-project.ps1` idempotently adds the copied
  scaffold files to the SDK `.vcxproj` and `.vcxproj.filters`.
- `scripts/build-native-bridge.ps1` builds that worktree after native
  prerequisites are present.
- `scripts/smoke-native-bridge.ps1` verifies a loaded native bridge with
  repeated raw-protocol ping and read-only CAD calls.

Native bridge prerequisite check:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check-native-bridge-prereqs.ps1
```

Fast doctor for the current machine/session:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\doctor-vectorworks-mcp.ps1
```

Native bridge doctor/deploy planner:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -Json
```

Agent-safe native setup runner:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\invoke-native-bridge-next.ps1 -Json
```

Use the runner before hand-executing doctor output. It reads
`nextCommandSpec`, blocks unless required safety flags are explicitly allowed
(`requiresNetwork`, `mayInstallSoftware`, `mayDownloadLargeFiles`,
`mayModifyVectorworksUserPlugins`, `requiresVectorworksRestartBeforeRun`,
`mayRequireReboot`), validates the command spec before execution, runs the
executable with arguments as an array, and reruns the doctor up to `-MaxSteps`
whenever `rerunDoctorAfter` is true. Agents should read the structured
`status`, `missingAllowFlags`, `safetyBlocks`, and `validationErrors` fields;
`blocked_by_safety_flag` means add the explicit allow switches only after user
review, and `invalid_spec` means stop because the doctor/runner contract is
stale or unsafe. The lower-level doctor JSON remains useful for inspecting
`nextCommandReason`; the older manual sequence below is reference material.

Optional SDK bootstrap helper:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-native-bridge.ps1
```

Opt-in Windows 11 native prerequisite setup:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-native-bridge.ps1 -InstallVisualStudioBuildTools -DownloadSdk -CloneSdkExamples -PrepareSource
```

After the SDK examples and Visual Studio tools are installed, prepare and build
the native worktree:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\prepare-native-bridge-source.ps1 -CloneSdkExamples
powershell -ExecutionPolicy Bypass -File .\scripts\build-native-bridge.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\copy-native-bridge-scaffold.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\wire-native-bridge-project.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\build-native-bridge.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -BuiltArtifact C:\path\to\VectorworksMCPBridge.vwlibrary -Install -WhatIf
powershell -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -BuiltArtifact C:\path\to\VectorworksMCPBridge.vwlibrary -Install
# Restart Vectorworks, enable/load the native VectorworksMCPBridge plug-in, then run the phase-0 stop smoke first.
powershell -ExecutionPolicy Bypass -File .\scripts\smoke-native-bridge.ps1 -Phase 0 -Stop -Json
```

After the phase-0 stop smoke passes, load the native bridge plug-in again before
running the default phase-1 read gate. In a disposable test document, add
`-AllowWriteFixture` to prove create/select/delete cleanup; the delete runs only
after the fixture identity and exact selection are verified.

The Python listener also applies conservative resource guards for long agent
sessions: `VW_MCP_MAX_CLIENTS`, `VW_MCP_CLIENT_IDLE_SECONDS`,
`VW_MCP_MAX_PENDING_READ_BYTES`, and `VW_MCP_MAX_PENDING_WRITE_BYTES`.
The host MCP server uses `VW_MCP_HEALTH_TIMEOUT` for short-lived ping and
preflight probes so diagnostics do not wait behind a slower CAD request on the
persistent command socket.

If an agent or user has already extracted the Vectorworks SDK somewhere else,
pass the same `-SdkDir C:\path\to\sdk` to the native doctor, bootstrap,
prepare, and build; the scripts preserve that custom SDK path end-to-end.

## Agent-Ready Setup

Fresh Windows 11 checkout:

```powershell
cd C:\path\to\vectorworks-mcp
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-agent.ps1 -Verify
```

Claude Code-specific setup:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-claude-code.ps1 -Verify
```

The setup is idempotent and safe to rerun. It:

- creates or refreshes `.venv`
- installs pinned host dependencies
- generates `vw_start_listener_2024.py` with machine-specific absolute paths and `VW_MCP_MODE=dialog`
- generates `vw_load_listener_2024.py`, a tiny stable Vectorworks script/menu loader that runs the current launcher file
- best-effort copies the exact loader script text to your clipboard
- registers the `vectorworks` MCP server with Claude Code
- falls back to updating `C:\Users\<you>\.claude.json` if `claude` is not on PATH
- runs no-Vectorworks host verification when `-Verify` is passed

This repo also includes a project `.mcp.json` that points Claude Code at the
self-bootstrapping runner. Claude Code may ask you to trust project MCP servers
the first time it sees that file.

## Claude Code Plugin

For the longer-term Claude Code workflow, use the standalone marketplace plugin:

```text
/plugin marketplace add BhaveshY/vectorworks-claude-plugin
/plugin install vectorworks@vectorworks-claude-plugin
/reload-plugins
```

The marketplace suffix is the Claude plugin marketplace name. The connector
repo remains `BhaveshY/vectorworks-mcp` and is resolved or cloned separately by
the plugin setup wrapper.

For local development of this connector repo, a bundled plugin mirror is also
available. It is checked against the standalone marketplace plugin in CI, and
its setup/runtime wrappers are contract-gated so agents do not silently bind to
stale connector checkouts:

```powershell
claude --plugin-dir C:\Users\Bhavesh\repos\vectorworks-mcp\plugins\vectorworks
```

If you launch Claude Code outside this repo, configure the plugin option
`vectorworks_repo` to this repo path, or set:

```powershell
$env:VW_MCP_REPO = "C:\Users\Bhavesh\repos\vectorworks-mcp"
claude --plugin-dir C:\Users\Bhavesh\repos\vectorworks-mcp\plugins\vectorworks
```

`VECTORWORKS_MCP_REPO` remains supported as a backward-compatible alias, but
`VW_MCP_REPO` is the canonical override used by the plugin docs and skills.

The plugin adds namespaced skills:

- `/vectorworks:setup` bootstraps dependencies and regenerates the Vectorworks launcher plus stable loader.
- `/vectorworks:ping` checks the raw listener and then `vw_ping` when MCP tools are loaded.
- `/vectorworks:diagnose` checks repo resolution, launcher agent-session mode, Claude availability, and listener reachability.
- `/vectorworks:work` guides CAD/BIM operations with the `vw_*` MCP tools.

The plugin also declares the `vectorworks` MCP server, so `vw_ping` and the
other `vw_*` tools become available when the plugin is enabled.

## Start Vectorworks Listener

After setup, open the generated loader file:

```text
vw_load_listener_2024.py
```

Setup tries to copy this loader script to your clipboard automatically. If you
need to copy it again, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\copy-vectorworks-loader.ps1
```

Paste/install the loader in Vectorworks, not the full launcher. The loader is a
stable two-line script that runs the regenerated `vw_start_listener_2024.py`
from disk, so future setup repairs update the launcher without requiring you to
replace the Vectorworks Resource Manager or menu-command script again. If you
previously pasted an old full foreground/background launcher, replace it with
this loader once.

Do not paste `vw_listener.py`, `vw_start_listener_2024.py`, or any old
foreground/background/timer launcher into Vectorworks. Paste only the generated
`vw_load_listener_2024.py` stable loader. For CAD-safe work, a healthy Python
listener reports `dispatch_mode=dialog`,
`bridge_kind=python_dialog_agent_session`, `cad_api_safe=true`, and
`transport_only=false`. Raw socket reachability is not enough: a listener that
can answer `ping` but reports `transport_only=true` is not safe for CAD
handlers.

### One-Session Script

1. Open Vectorworks 2024 or 2025.
2. Open Resource Manager with `Ctrl+R`.
3. Create `New Resource > Script`.
4. Choose Python Script.
5. Paste the generated `vw_load_listener_2024.py`.
6. Run the script resource.
7. A small `VW MCP Listener` dialog should stay open during the agent-control session.
8. Use `vw_ping` from Claude Code as the real confirmation, then close/stop the dialog when you want manual Vectorworks control back.

### Persistent Menu Command

1. Open `Tools > Plug-ins > Plug-in Manager`.
2. Create `New > Menu Command`.
3. Name it `VW MCP Listener`.
4. Edit the script and paste the generated `vw_load_listener_2024.py`.
5. Save it.
6. Open `Tools > Workspaces > Edit Current Workspace > Menus`.
7. Drag `VW MCP Listener` into a menu.
8. Click that menu command once per Vectorworks session.

## Verify End To End

Restart Claude Code after setup, then run:

```text
/mcp
```

Confirm `vectorworks` is listed. With Vectorworks open and the listener running,
try:

```text
Use vw_ping.
Use vw_get_document_info.
Create a 500x300 rectangle at position 0,0.
```

If `vw_ping` fails, Claude Code can start the MCP server, but the Vectorworks
listener is not reachable on `127.0.0.1:9877`.

## No-Vectorworks Verification

These checks prove the host side works without opening Vectorworks:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify-no-vectorworks.ps1
```

The same no-Vectorworks verification runs in GitHub Actions on Windows.

Manual equivalent:

```powershell
.\.venv\Scripts\python.exe -m py_compile server.py vw_listener.py vw_start_listener_2024.py vw_load_listener_2024.py
.\.venv\Scripts\python.exe -m unittest discover -v
```

## Available Tools

Core:

| Tool | Description |
|------|-------------|
| `vw_ping` | Health check, including bridge mode and CAD safety status |
| `vw_bridge_status` | Same status payload as `vw_ping`, named for agent preflight checks |
| `vw_preflight_for_cad` | Structured JSON go/no-go check before real CAD/API handlers |
| `vw_tool_safety` | Structured safety metadata for all tools |
| `vw_run_script` | Execute trusted Python inside Vectorworks |
| `vw_create_object` | Create rect, circle, oval, line, arc, polygon |
| `vw_get_layers` | List layers |
| `vw_get_objects` | List objects filtered by layer/type |
| `vw_set_object_property` | Change name, class, color, line weight, opacity |
| `vw_find_objects` | Criteria-based search such as `T=WALL` |
| `vw_manage_classes` | List, create, delete classes |
| `vw_worksheet` | Read/write worksheet cells and ranges |
| `vw_symbol` | List and insert symbols |
| `vw_export` | Export PDF, DXF, DWG, or image where VW supports automation |
| `vw_import_file` | Import DXF, DWG, or image files |
| `vw_get_document_info` | Document metadata |
| `vw_screenshot` | Capture viewport screenshot where supported |
| `vw_stop_listener` | Ask the listener to stop gracefully |
| `vw_selection` | Get, select, clear, delete, move, or duplicate selected objects |

Architectural:

| Tool | Description |
|------|-------------|
| `vw_create_wall` | Create parametric walls |
| `vw_insert_door` | Insert a parametric door |
| `vw_insert_window` | Insert a parametric window |
| `vw_create_slab` | Create a slab from a polygon footprint |
| `vw_create_roof` | Create a roof from a footprint |
| `vw_inspect_object` | Discover object/plugin parameters |

## Agent Handoff

Project instructions are in `AGENTS.md`; Claude Code imports them through
`CLAUDE.md`.

Known-good host checks:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-agent.ps1 -Verify
```

End-to-end requires:

- Vectorworks 2024/2025 open
- listener started through the generated `vw_load_listener_2024.py`; leave the `VW MCP Listener` dialog open while the agent works
- MCP client restarted after registration
- `/mcp` showing `vectorworks`
- first tool call: `vw_ping`

## Troubleshooting

`claude` is not recognized:

- The setup script updates `~\.claude.json` directly when the CLI is missing.
- Restart Claude Code afterward.

`vw_ping` reports a connection error:

- Start Vectorworks.
- Run the generated `vw_load_listener_2024.py` inside Vectorworks.
- Confirm the `VW MCP Listener` dialog is open on `127.0.0.1:9877`, then verify with `vw_ping`.
- Check that no previous listener is already using port `9877`.

Vectorworks hangs after running the listener script:

- Regenerate `vw_start_listener_2024.py` with `.\scripts\bootstrap-claude-code.ps1 -Verify`.
- Run `.\scripts\copy-vectorworks-loader.ps1`, then replace any old pasted Vectorworks script with the clipboard contents from `vw_load_listener_2024.py`; it loads the current launcher from disk and prevents stale pasted listener code from lingering in a menu command.
- Confirm the generated launcher contains `os.environ["VW_MCP_MODE"] = "dialog"`.
- Confirm `vw_ping` reports `dispatch_mode=dialog`,
  `bridge_kind=python_dialog_agent_session`, `cad_api_safe=true`, and
  `transport_only=false` before CAD work.
- If Vectorworks is already stuck from an older foreground launcher, create
  `C:\Users\<you>\.vectorworks-mcp\STOP`, wait a few seconds, then restart
  Vectorworks if it does not recover.

`vw_ping` times out, but port `9877` is open:

- This means Vectorworks owns the TCP socket but the Python listener loop is not
  servicing requests. Run `.\scripts\test-vectorworks-listener.ps1` to see the
  owning process and socket state.
- Create `C:\Users\<you>\.vectorworks-mcp\STOP` and wait a few seconds.
- If the port still times out, save your work and restart Vectorworks. Then run
  the generated loader again.
- Background and Windows timer modes are transport-only diagnostics; they can
  answer `vw_ping`, but real CAD handlers can deadlock outside a normal
  Vectorworks script or plug-in event context.

`VW MCP failed to bind 127.0.0.1:9877`:

- A previous listener is still running. Call `vw_stop_listener`, create
  `C:\Users\<you>\.vectorworks-mcp\STOP`, or restart Vectorworks.

`Handle not found`:

- Handles only live for the current listener session. Use `vw_get_objects` or
  `vw_find_objects` again after restarting the listener.

## Security

This is a local-trust connector. `vw_run_script` can execute arbitrary Python
inside Vectorworks. Only enable this MCP server in Claude Code profiles you
trust.
