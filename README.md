# Vectorworks 2024/2025 MCP Connector

Connect Codex, Claude Code, or any stdio MCP client to Vectorworks on the same
Windows PC.

## Architecture

```text
Codex / Claude Code / MCP client <--stdio--> scripts/run-mcp-server.ps1
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
| Native SDK bridge | phase-2 SDK bridge when built/installed | yes, guarded to implemented actions | non-modal native plug-in |

The proper long-term fix for non-modal, always-on control is a native
Vectorworks SDK plug-in bridge. The SDK bridge in `native_bridge/` can be copied
into an SDK examples worktree, wired into the module lifecycle, built,
installed, and smoke-tested. It is not compiled or installed by default by the
host MCP setup, because fresh machines still need the official Vectorworks SDK
and Visual Studio C++ build tools. When the compiled phase-2 bridge is loaded it
reports `native-sdk-bridge-phase2`, `cad_api_safe=true`, `transport_only=false`,
and `main_context_pump_ready=true`; older phase-1 builds report
`native-sdk-bridge-phase1`.

Native phase 1 implements `get_document_info`, `get_layers`,
`get_objects`, `selection` (`get`, `clear`, `select`, `delete`), and
`create_object` for `rect`, `rectangle`, `box`, `circle`, `oval`, `line`, and
`arc`, plus atomic `batch_create_objects` for multiple phase-1 primitives in
one native undo event. Native phase 2 adds true wall objects, text blocks,
linear dimensions, and mixed atomic batches, including `vw_create_bim_floor_plan`
for wall-based rectangular room layouts. Write tools require an active
Vectorworks document; the Home/no-document screen can answer read health checks
but is not a valid drawing target. In an active document with no current
writable design layer, native creation attempts to create/select a default
`Vectorworks MCP Layer` before drawing. Host preflight blocks broader MCP tools
or unsupported variants before dispatching them to the native bridge. Use the
generated Python dialog launcher when you need broader legacy tool coverage that
has not been ported to native yet.

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

Fresh-PC agent instructions live in `AGENT_INSTALL.md`.

Native bridge planning aids:

- `native_bridge/HANDLER_MATRIX.md` maps every current listener action to native
  phase, safety, and smoke-test expectations.
- `native_bridge/mock/mock_bridge.py` is a no-SDK protocol harness proving the
  host MCP server and preflight logic can talk to the native bridge contract.
- `scripts/prepare-native-bridge-source.ps1` prepares an ignored SDK-backed
  source worktree from Vectorworks' official SDK examples.
- `scripts/copy-native-bridge-scaffold.ps1` copies the reviewed no-SDK native
  scaffold into that worktree after the unmodified SDK example builds.
- `scripts/wire-native-bridge-project.ps1` idempotently adds the copied
  scaffold files to the SDK `.vcxproj` and `.vcxproj.filters`, then patches
  `Source\ModuleMain.cpp` so the transport starts and stops with the plug-in
  lifecycle.
- `scripts/build-native-bridge.ps1` builds that worktree after native
  prerequisites are present.
- `scripts/smoke-native-bridge.ps1` verifies a loaded native bridge with
  repeated raw-protocol phase-1 read checks by default, optional
  `-AllowWriteFixture` create/select/delete cleanup, `-Phase 2
  -AllowWriteFixture` wall/text/dimension write checks, and `-Phase 0 -Stop`
  port release checks. Write fixtures must run in a disposable active document,
  not on the Vectorworks Home/no-document screen.
- `scripts/start-vectorworks-native-smoke.ps1` discovers, starts, or gracefully
  restarts Vectorworks, waits for the native bridge socket, then runs the native
  smoke script. Phase-2 runs first reuse an already healthy bridge instead of
  reopening Vectorworks; restart is used only when the bridge is missing/stale or
  an explicit force-restart path is requested. It only force-kills Vectorworks
  when explicitly allowed.

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
powershell -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -BuiltArtifact C:\path\to\ObjectExample.vlb -Install -WhatIf
powershell -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -BuiltArtifact C:\path\to\ObjectExample.vlb -Install
# Start/restart Vectorworks and run the phase-0 stop smoke automatically.
powershell -ExecutionPolicy Bypass -File .\scripts\start-vectorworks-native-smoke.ps1 -VectorworksVersion 2024 -RestartIfRunning -Json
```

The official SDK example scaffold currently emits `ObjectExample.vlb`; the
doctor accepts the built artifact path explicitly and installs that candidate
when requested. After the phase-0 stop smoke passes, load the native bridge
plug-in again and run the default phase-1 read smoke. In a disposable test
document, add `-AllowWriteFixture` to prove create/select/delete cleanup; the
delete runs only after the fixture identity and exact selection are verified.
Run `-Phase 2 -AllowWriteFixture` in a disposable document to prove native wall,
text, linear dimension, and mixed-batch creation.

The Python listener also applies conservative resource guards for long agent
sessions: `VW_MCP_MAX_CLIENTS`, `VW_MCP_CLIENT_IDLE_SECONDS`,
`VW_MCP_MAX_PENDING_READ_BYTES`, and `VW_MCP_MAX_PENDING_WRITE_BYTES`.
The host MCP server uses `VW_MCP_HEALTH_TIMEOUT` for short-lived ping and
preflight probes so diagnostics do not wait behind a slower CAD request on the
persistent command socket.

If an agent or user has already extracted the Vectorworks SDK somewhere else,
pass the same `-SdkDir C:\path\to\sdk` to the native doctor, bootstrap,
prepare, and build; the scripts preserve that custom SDK path end-to-end.
If the official SDK ZIP is already downloaded, the prerequisite checker reports
it in `sdkArchiveCandidates`, and the guarded doctor/runner will prefer
`-SdkArchivePath C:\path\to\SDK.zip` instead of downloading the archive again.

## Agent-Ready Universal Setup

One-command install from any PowerShell on Windows 11:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/BhaveshY/vectorworks-mcp/main/install.ps1 | iex"
```

The installer clones or updates `BhaveshY/vectorworks-mcp` under
`$env:USERPROFILE\repos\vectorworks-mcp`, installs Python dependencies,
generates the durable `vw_start_listener_2024.py` launcher and
`vw_load_listener_2024.py` Vectorworks loader, runs host verification, and
leaves the repo `.mcp.json` ready for Codex, Claude Code project MCP, or any
stdio MCP client. The default is `-Client HostOnly`, so it does not modify
Claude Code user config.

From an existing checkout:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

For a non-technical Windows PC where the agent should also attempt the native
SDK bridge setup in one run, use:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/BhaveshY/vectorworks-mcp/main/install.ps1 -OutFile $env:TEMP\vectorworks-mcp-install.ps1; powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $env:TEMP\vectorworks-mcp-install.ps1 -FullNative"
```

`-FullNative` first checks/installs base tools (`Git` and `Python 3.12`) when
winget is available, then runs the guarded native bridge loop with the required
network/software/download/plugin-write allow flags. After the native artifact is
installed, the installer automatically opens or restarts Vectorworks, waits for
the native plug-in socket, runs phase-0 transport smoke, and then attempts the
phase-2 production smoke. If Vectorworks opens on the Home/no-document screen,
the native bridge opens a default blank document before write fixtures. If
Vectorworks blocks automation with a license, recovery, plug-in approval, or
startup prompt, JSON reports
`native_summary.vectorworks_automation_attempted`, `native_summary.next_command`,
and `native_summary.acceptance_next_command` so the agent can resume exactly.
Native production readiness is not claimed until both smoke stages pass.

If you specifically want Claude Code user registration from the checkout:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Client ClaudeCode
```

Then point the client at the project `.mcp.json` from this repo, or configure
the same server manually:

```json
{
  "mcpServers": {
    "vectorworks": {
      "type": "stdio",
      "command": "powershell.exe",
      "args": [
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "scripts/run-mcp-server.ps1"
      ],
      "env": {
        "VW_MCP_HOST": "127.0.0.1",
        "VW_MCP_PORT": "9877",
        "VW_MCP_TIMEOUT": "60",
        "VW_MCP_PREFLIGHT_CACHE_MS": "750"
      },
      "timeout": 120000
    }
  }
}
```

The root `.mcp.json` intentionally uses the repo-relative
`scripts/run-mcp-server.ps1` path instead of Claude-only variables so project
MCP loading can work in Codex, Claude Code, and other clients that launch from
the checkout root. If your client launches MCP servers from another working
directory, use the absolute path to `scripts\run-mcp-server.ps1`.

Package-style local development is also supported:

```powershell
py -3 -m pip install -e .
# Use vectorworks-mcp as the stdio command in an MCP client configuration.
```

The self-contained console script starts the same host MCP server as `server.py`; the
self-bootstrapping scripts remain the recommended setup path because they also
prepare the repo-local virtual environment, generated Vectorworks loader, and
optional agent/client registration helpers.

Bundled plugin helper:

```powershell
py -3 .\plugins\vectorworks\bin\vectorworksctl agent-install --repo-path $PWD --json
py -3 .\plugins\vectorworks\bin\vectorworksctl doctor --repo-path $PWD --json
```

`agent-install` prepares the Python dialog fallback and returns the guarded
native SDK bridge plan in one JSON payload. `setup_complete: true` means the
MCP install is usable now; `native_requires_action: true` means only the
optional non-modal native bridge still has follow-up work. JSON also includes
top-level `repo_root`, `mcp_config_path`, `runner_path`, `launcher_path`,
`loader_path`, and `next_user_step` so agents do not need to scrape PowerShell
output. See
`AGENT_INSTALL.md` for the full fresh-PC flow.

Claude Code-specific setup:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-claude-code.ps1 -Verify
```

The setup is idempotent and safe to rerun. It:

- creates or refreshes `.venv`
- installs bounded host dependencies into the repo-local virtual environment
- generates `vw_start_listener_2024.py` with machine-specific absolute paths and `VW_MCP_MODE=dialog`
- generates `vw_load_listener_2024.py`, a tiny stable Vectorworks script/menu loader that runs the current launcher file
- best-effort copies the exact loader script text to your clipboard
- registers the `vectorworks` MCP server with Claude Code
- falls back to updating `C:\Users\<you>\.claude.json` if `claude` is not on PATH
- runs no-Vectorworks host verification when `-Verify` is passed

This repo also includes a project `.mcp.json` that points compatible MCP clients
at the self-bootstrapping runner. Claude Code and Codex may ask you to trust
project MCP servers the first time they see that file.

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

If an agent is pointed at this connector repo directly, it can also install the
bundled plugin through the connector marketplace metadata:

```text
/plugin marketplace add BhaveshY/vectorworks-mcp
/plugin install vectorworks@vectorworks-mcp
/reload-plugins
```

For local development of this connector repo, a bundled plugin mirror is also
available. It is checked against the standalone marketplace plugin in CI, and
its setup/runtime wrappers are contract-gated so agents do not silently bind to
stale connector checkouts:

```powershell
claude --plugin-dir $env:USERPROFILE\repos\vectorworks-mcp\plugins\vectorworks
```

If you launch Claude Code outside this repo, configure the plugin option
`vectorworks_repo` to this repo path, or set:

```powershell
$env:VW_MCP_REPO = "$env:USERPROFILE\repos\vectorworks-mcp"
claude --plugin-dir $env:USERPROFILE\repos\vectorworks-mcp\plugins\vectorworks
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

Restart or reload your MCP client after setup, then confirm the `vectorworks`
server is trusted and loaded. In Claude Code, `/mcp` should list
`vectorworks`; in Codex or another MCP client, use that client's server status
view or make the first tool call.

With Vectorworks open and the listener running, try:

```text
Use vw_ping.
Use vw_get_document_info.
Create a 500x300 rectangle at position 0,0.
```

If `vw_ping` fails, the MCP client may have started the host server correctly,
but the Vectorworks listener is not reachable on `127.0.0.1:9877`.

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
| `vw_agent_context` | One-call compact Codex planning snapshot with preflight, key capabilities, and token-efficient drawing summary |
| `vw_capabilities` | Current bridge capabilities, native phase-1/phase-2 support, and tool surface |
| `vw_tool_safety` | Structured safety metadata for all tools |
| `vw_run_script` | Execute trusted Python inside Vectorworks; requires `confirm="RUN_TRUSTED_CODE"` |
| `vw_create_object` | Create rect/rectangle/box, circle, oval, line, arc; phase-2 batches also support wall, text, and linear_dimension |
| `vw_batch_create_objects` | Create many native objects; `atomic=true` uses native all-or-none batch creation, `atomic=false` uses legacy non-atomic composition |
| `vw_plan_schematic_floor_plan` | Dry-run a multi-room schematic floor plan and return the native primitives |
| `vw_create_schematic_floor_plan` | Create a multi-room schematic floor plan from rooms, wall segments, doors, and windows |
| `vw_create_bim_floor_plan` | Create a native wall-based floor plan with optional room labels and linear dimensions |
| `vw_create_schematic_room` | Create a rectangular schematic room from native 2D wall rectangles |
| `vw_create_schematic_door` | Draw a schematic door leaf and swing arc from native 2D primitives |
| `vw_create_schematic_window` | Draw a schematic double-line window marker from native 2D primitives |
| `vw_get_layers` | List layers |
| `vw_get_objects` | List objects filtered by layer/type |
| `vw_drawing_summary` | Summarize document, layers, object counts, optional examples, and bounds; use `include_examples=false` for compact large-project context |
| `vw_set_object_property` | Change name, class, color, line weight, opacity |
| `vw_find_objects` | Criteria-based search such as `T=WALL`; native bridge can resolve simple `ALL`, `T=...`, `C=...`, and exact-name `((N='Name'))` lookups via bounded `get_objects` |
| `vw_manage_classes` | List, create, delete classes; delete requires `confirm="DELETE_CLASS"` |
| `vw_worksheet` | Read/write worksheet cells and ranges |
| `vw_symbol` | List and insert symbols |
| `vw_export` | Open the matching Vectorworks export dialog and report that manual save confirmation is required |
| `vw_import_file` | Import DXF, DWG, or image files |
| `vw_get_document_info` | Document metadata |
| `vw_screenshot` | Open the Vectorworks Export Image File dialog with the requested path |
| `vw_stop_listener` | Ask the listener to stop gracefully |
| `vw_selection` | Get, select, clear, delete, move, or duplicate selected objects; selected-object delete requires `confirm="DELETE_SELECTED"`; exact-name criteria delete requires `confirm="DELETE_EXACT_NAME"` |

Architectural:

| Tool | Description |
|------|-------------|
| `vw_create_wall` | Create native true wall objects |
| `vw_create_text` | Create native text annotations |
| `vw_create_linear_dimension` | Create native linear dimensions |
| `vw_insert_door` | Insert a parametric door through the Python/legacy path; native wall-hosted insertion is deferred pending plugin inspection |
| `vw_insert_window` | Insert a parametric window through the Python/legacy path; native wall-hosted insertion is deferred pending plugin inspection |
| `vw_create_slab` | Create an extruded floor-like solid from a polygon footprint, not a BIM slab object |
| `vw_create_roof` | Try to create a roof custom object from a footprint, with flat extrusion fallback |
| `vw_inspect_object` | Discover object/plugin parameters; plugin probing requires `confirm="PROBE_PLUGIN"` |

## Agent Handoff

Project instructions are in `AGENTS.md`; client-specific entrypoints are
`CLAUDE.md` for Claude Code and `CODEX.md` for Codex.

Known-good host checks:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-agent.ps1 -Verify
```

End-to-end requires:

- Vectorworks 2024/2025 open
- listener started through the generated `vw_load_listener_2024.py`; leave the `VW MCP Listener` dialog open while the agent works
- MCP client restarted after registration
- `vectorworks` trusted/loaded in the MCP client; Claude Code users can confirm this with `/mcp`
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

- Regenerate `vw_start_listener_2024.py` with `.\scripts\bootstrap-agent.ps1 -Verify` or, for host-only clients, `.\scripts\bootstrap-agent.ps1 -Client HostOnly -Verify`.
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

This connector is loopback-only and authenticated by default. The Python server
and Python listener reject non-loopback `VW_MCP_HOST` values, and the native
transport has the same local-bind policy. Setup writes a per-user token to
`C:\Users\<you>\.vectorworks-mcp\auth-token` and sets `VW_MCP_AUTH_TOKEN` /
`VW_MCP_AUTH_TOKEN_FILE` for the MCP server and generated Vectorworks launcher.
Frames without the token are rejected before dispatch. `VW_MCP_INSECURE_NO_AUTH=1`
exists only for local diagnostics/tests.

`vw_run_script` can execute arbitrary Python inside Vectorworks. Destructive or
trusted-code paths require explicit confirmation arguments, and the listener
enforces the same confirmations for raw TCP requests. `vw_selection` delete
without criteria deletes only the current selection with
`confirm="DELETE_SELECTED"`; criteria-based delete is restricted to exact object
name criteria such as `((N='Fixture'))` and requires
`confirm="DELETE_EXACT_NAME"`.
