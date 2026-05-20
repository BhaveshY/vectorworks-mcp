# Vectorworks 2024/2025 MCP Server

Connect Claude Code to Vectorworks 2024 or 2025 via a TCP socket on the same machine.

## Architecture

```
Claude Code <--stdio--> server.py <--TCP/JSON--> vw_listener.py (inside Vectorworks)
                                   127.0.0.1:9877
```

The listener runs inside Vectorworks's Python interpreter on the main
thread (the only thread where the `vs` module is safe). It uses
non-blocking I/O via `selectors` — no background threads — so every
`vs.*` call is serialised on the thread VW expects.

Wire format: 4-byte big-endian length prefix + UTF-8 JSON body.

## Setup

### 1. Install dependencies (host side)

```cmd
cd vectorworks-mcp
py -3 -m pip install -r requirements.txt
```

### 2. Register the MCP server with Claude Code

Recommended on Windows 11:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register-claude-code.ps1
```

Manual registration:

```cmd
claude mcp add vectorworks -- python C:\path\to\vectorworks-mcp\server.py
```

With a custom port:

```cmd
claude mcp add-json vectorworks "{\"type\":\"stdio\",\"command\":\"python\",\"args\":[\"C:\\\\path\\\\to\\\\vectorworks-mcp\\\\server.py\"],\"env\":{\"VW_MCP_PORT\":\"9877\"}}"
```

### 3. Start the listener inside Vectorworks

Two options:

**A) Quick (one session):**
1. Open Vectorworks 2024 or 2025
2. `Tools > Plug-ins > Script Editor`
3. Language: **Python**
4. Paste the contents of `vw_listener.py`
5. Click **Run**
6. Alert box confirms: `VW MCP Listener STARTED (socket)` and shows `127.0.0.1:9877`

**B) Persistent menu command (recommended):**
1. `Tools > Plug-ins > Plug-in Manager` → **New** → **Menu Command**
2. Name it `VW MCP Listener`, pick a category (e.g. `MCP`)
3. Paste `vw_listener.py` as the script, save
4. `Tools > Workspaces > Edit Current Workspace > Menus`, drag the new
   command into a menu (e.g. under `Tools`)
5. Click the menu command once per VW session to start the listener

### 4. Use it

Open Claude Code. The Vectorworks tools are available. Try:

- "Ping Vectorworks to check the connection"
- "What layers are in my Vectorworks document?"
- "Create a 500x300 rectangle at position 0,0"
- "Create a 3m high wall from 0,0 to 5000,0"
- "Insert a 900mm door at position 2000,0"
- "Inspect the door and show me all its parameters"
- "Find all walls in the drawing"
- "Create a floor slab for a 5x4m room"

## Available Tools (22)

### Core
| Tool | Description |
|------|-------------|
| `vw_ping` | Health check — returns listener version and handler count |
| `vw_run_script` | Execute arbitrary Python inside VW (escape hatch) |
| `vw_create_object` | Create geometry: rect, circle, oval, line, arc, polygon |
| `vw_get_layers` | List all layers with visibility |
| `vw_get_objects` | List objects filtered by layer/type |
| `vw_set_object_property` | Change name, class, color, line weight, opacity |
| `vw_find_objects` | Criteria-based search (`T=WALL`, `C='Furniture'`, etc.) |
| `vw_manage_classes` | List, create, delete classes |
| `vw_worksheet` | Read/write worksheet cells and ranges |
| `vw_symbol` | List and insert symbols from resource library |
| `vw_export` | Export to PDF, DXF, DWG, or image |
| `vw_import_file` | Import DXF, DWG, or image files |
| `vw_get_document_info` | Document metadata (layers, object count, etc.) |
| `vw_screenshot` | Capture viewport screenshot |
| `vw_stop_listener` | Ask the Vectorworks listener to stop gracefully |
| `vw_selection` | Get/set/clear/delete/move/duplicate selected objects |

### Architectural
| Tool | Description |
|------|-------------|
| `vw_create_wall` | Parametric walls with height and thickness |
| `vw_insert_door` | Parametric door (place near wall for auto-insertion) |
| `vw_insert_window` | Parametric window with sill height |
| `vw_create_slab` | Floor slab from polygon footprint (3D extrusion) |
| `vw_create_roof` | Roof from footprint with slope, overhang, bearing height |
| `vw_inspect_object` | Discover ALL parameters of any VW object |

## Configuration

All env vars are optional.

| Var | Default | Side | Purpose |
|---|---|---|---|
| `VW_MCP_HOST` | `127.0.0.1` | both | Bind/connect address |
| `VW_MCP_PORT` | `9877` | both | Port |
| `VW_MCP_TIMEOUT` | `60` | server | Per-request timeout (seconds) |
| `VW_MCP_MAX_FRAME_BYTES` | `16777216` | both | Maximum TCP JSON frame size |
| `VW_MCP_STOP_DIR` | `~/.vectorworks-mcp` | listener | Where the STOP sentinel lives |

The listener and server must agree on host+port.

## Stopping the Listener

Any of:
- In Claude Code, call `vw_stop_listener`
- Create an empty file named `STOP` in the stop-file folder shown at startup
  (default `~/.vectorworks-mcp/STOP`)
- Quit Vectorworks or close the document

On Windows PowerShell, the default STOP file can be created with:

```powershell
New-Item -ItemType File -Force "$env:USERPROFILE\.vectorworks-mcp\STOP"
```

## Development and Tests

Tests do not require Vectorworks. They mock the `vs` module and use local
loopback sockets to verify the length-prefixed JSON protocol.

```cmd
py -3 -m pip install -r requirements-dev.txt
py -3 -m py_compile server.py vw_listener.py
py -3 -m unittest discover -v
# or, if pytest is installed:
py -3 -m pytest
```

## Troubleshooting

**`Connection error: ... Is vw_listener.py running?`**
- Confirm the listener is running inside VW (you should have seen the
  "STARTED" alert). Click the menu command again or re-run the script.
- Check the port matches on both sides (`VW_MCP_PORT`).
- On Windows, confirm the Windows Firewall isn't blocking loopback —
  localhost-only connections usually bypass it, but AV can interfere.

**`Vectorworks MCP startup error: The 'fastmcp' package is not installed`**
- Install host dependencies from this repo:
  `py -3 -m pip install -r requirements.txt`

**`VW MCP failed to bind 127.0.0.1:9877`**
- A previous listener is still running. Close it via the STOP file or
  restart Vectorworks. Alternatively set `VW_MCP_PORT` to a free port
  on both sides.

**"Handle not found"**
- Handles are valid only for the current listener session. Restarting
  the listener invalidates them — use `vw_get_objects` or
  `vw_find_objects` to get fresh handles.

**Listener stops unexpectedly**
- VW may interrupt long-running scripts in some configurations. The
  menu-command install (option B above) is more robust than pasting
  into the Script Editor each session.

## Changelog

**0.3.0** — Windows/Claude Code hardening
- Added bounded protocol frames, malformed-response diagnostics, safe response
  serialization, and clearer startup/configuration errors
- Fixed listener socket interest handling so idle clients do not create a busy
  writable loop
- Added graceful `vw_stop_listener`/`stop` action
- Added Windows Claude Code registration script
- Added unit test coverage with fake listener/socket and fake Vectorworks `vs`
  module; no Vectorworks instance is required

**0.2.0** — Socket transport
- Replaced file-bridge polling with persistent TCP + length-prefixed JSON
- `selectors`-based non-blocking I/O on VW's main thread (no threads, no
  polling on disk)
- Automatic reconnect on the host side
- `vw_ping` health check
- Menu-command install documented

**0.1.0** — Initial file-bridge release.
