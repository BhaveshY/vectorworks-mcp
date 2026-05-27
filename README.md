# Vectorworks 2024/2025 MCP Connector

Connect Claude Code to Vectorworks on the same Windows PC.

## Architecture

```text
Claude Code <--stdio--> scripts/run-mcp-server.ps1
                         |
                         v
                      server.py <--TCP/JSON--> vw_listener.py (Windows timer inside Vectorworks)
                                             127.0.0.1:9877
```

`scripts/run-mcp-server.ps1` is the self-bootstrapping entrypoint. It creates a
repo-local `.venv`, installs `requirements.txt`, then starts `server.py`. The
generated Vectorworks launcher sets `VW_MCP_MODE=win_timer` so Vectorworks pumps
the socket from its normal Windows message loop without a modal dialog.

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
- generates `vw_start_listener_2024.py` with machine-specific absolute paths and `VW_MCP_MODE=win_timer`
- registers the `vectorworks` MCP server with Claude Code
- falls back to updating `C:\Users\<you>\.claude.json` if `claude` is not on PATH
- runs no-Vectorworks host verification when `-Verify` is passed

This repo also includes a project `.mcp.json` that points Claude Code at the
self-bootstrapping runner. Claude Code may ask you to trust project MCP servers
the first time it sees that file.

## Claude Code Plugin

For the longer-term Claude Code workflow, use the bundled plugin:

```powershell
claude --plugin-dir C:\Users\Bhavesh\repos\vectorworks-mcp\plugins\vectorworks
```

If you launch Claude Code outside this repo, configure the plugin option
`vectorworks_repo` to this repo path, or set:

```powershell
$env:VECTORWORKS_MCP_REPO = "C:\Users\Bhavesh\repos\vectorworks-mcp"
claude --plugin-dir C:\Users\Bhavesh\repos\vectorworks-mcp\plugins\vectorworks
```

The plugin adds namespaced skills:

- `/vectorworks:setup` bootstraps dependencies and regenerates the Vectorworks launcher.
- `/vectorworks:ping` checks the raw listener and then `vw_ping` when MCP tools are loaded.
- `/vectorworks:diagnose` checks repo resolution, launcher Windows timer mode, Claude availability, and listener reachability.
- `/vectorworks:work` guides CAD/BIM operations with the `vw_*` MCP tools.

The plugin also declares the `vectorworks` MCP server, so `vw_ping` and the
other `vw_*` tools become available when the plugin is enabled.

## Start Vectorworks Listener

After setup, open the generated file:

```text
vw_start_listener_2024.py
```

### One-Session Script

1. Open Vectorworks 2024 or 2025.
2. Open Resource Manager with `Ctrl+R`.
3. Create `New Resource > Script`.
4. Choose Python Script.
5. Paste the generated `vw_start_listener_2024.py`.
6. Run the script resource.
7. The script should return immediately and Vectorworks should remain usable.
8. Use `vw_ping` from Claude Code as the real confirmation.

### Persistent Menu Command

1. Open `Tools > Plug-ins > Plug-in Manager`.
2. Create `New > Menu Command`.
3. Name it `VW MCP Listener`.
4. Edit the script and paste the generated `vw_start_listener_2024.py`.
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

Manual equivalent:

```powershell
.\.venv\Scripts\python.exe -m py_compile server.py vw_listener.py vw_start_listener_2024.py
.\.venv\Scripts\python.exe -m unittest discover -v
```

## Available Tools

Core:

| Tool | Description |
|------|-------------|
| `vw_ping` | Health check |
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
- listener started from the generated `vw_start_listener_2024.py`; Vectorworks should remain usable
- MCP client restarted after registration
- `/mcp` showing `vectorworks`
- first tool call: `vw_ping`

## Troubleshooting

`claude` is not recognized:

- The setup script updates `~\.claude.json` directly when the CLI is missing.
- Restart Claude Code afterward.

`vw_ping` reports a connection error:

- Start Vectorworks.
- Run the generated `vw_start_listener_2024.py`.
- Confirm the Vectorworks message bar reports listener startup on `127.0.0.1:9877`, then verify with `vw_ping`.
- Check that no previous listener is already using port `9877`.

Vectorworks hangs after running the listener script:

- Regenerate `vw_start_listener_2024.py` with `.\scripts\bootstrap-claude-code.ps1 -Verify`.
- Confirm the generated launcher contains `os.environ["VW_MCP_MODE"] = "win_timer"`.
- If Vectorworks is already stuck from an older foreground launcher, create
  `C:\Users\<you>\.vectorworks-mcp\STOP`, wait a few seconds, then restart
  Vectorworks if it does not recover.

`vw_ping` times out, but port `9877` is open:

- This means Vectorworks owns the TCP socket but the Python listener loop is not
  servicing requests. Run `.\scripts\test-vectorworks-listener.ps1` to see the
  owning process and socket state.
- Create `C:\Users\<you>\.vectorworks-mcp\STOP` and wait a few seconds.
- If the port still times out, save your work and restart Vectorworks. Then run
  the generated launcher again.
- If every old background or modal launch repeats this timeout, regenerate the
  launcher and replace the old Vectorworks menu/script with the Windows timer
  version.

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
