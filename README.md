# Vectorworks 2025 MCP Server

Connect Claude Code to Vectorworks 2025 on the same Windows PC.

## Architecture

```
Claude Code <--stdio--> server.py <--file bridge--> vw_listener.py (inside Vectorworks)
```

The MCP server writes JSON request files to a bridge folder.
The VW listener (running inside Vectorworks Script Editor) polls for
those files, executes commands via the `vs` module, and writes responses.

## Setup

### 1. Install dependencies

```cmd
cd vectorworks-mcp
pip install -r requirements.txt
```

### 2. Register with Claude Code

```cmd
claude mcp add vectorworks -- python C:\path\to\vectorworks-mcp\server.py
```

Or with environment variable for custom bridge path:

```cmd
claude mcp add-json vectorworks "{\"type\":\"stdio\",\"command\":\"python\",\"args\":[\"C:\\\\path\\\\to\\\\vectorworks-mcp\\\\server.py\"],\"env\":{\"VW_BRIDGE_PATH\":\"C:\\\\path\\\\to\\\\vectorworks-mcp\\\\bridge\"}}"
```

### 3. Start the listener in Vectorworks

1. Open Vectorworks 2025
2. Go to **Tools > Plug-ins > Script Editor**
3. Select **Python** as the language
4. Open or paste the contents of `vw_listener.py`
5. **Edit the `BRIDGE_PATH`** at the top to match your actual path:
   ```python
   BRIDGE_PATH = r"C:\Users\YourName\vectorworks-mcp\bridge"
   ```
6. Click **Run**
7. You should see an alert: "VW MCP Listener STARTED"

### 4. Use it

Open Claude Code. The Vectorworks tools are now available. Try:

- "What layers are in my Vectorworks document?"
- "Create a 500x300 rectangle at position 0,0"
- "Create a 3m high wall from 0,0 to 5000,0"
- "Insert a 900mm door at position 2000,0"
- "Inspect the door and show me all its parameters"
- "Find all walls in the drawing"
- "Create a floor slab for a 5x4m room"

## Available Tools (20)

### Core Tools
| Tool | Description |
|------|-------------|
| `vw_run_script` | Execute arbitrary Python inside VW (escape hatch) |
| `vw_create_object` | Create geometry: rect, circle, oval, line, arc, polygon |
| `vw_get_layers` | List all layers with visibility |
| `vw_get_objects` | List objects filtered by layer/type |
| `vw_set_object_property` | Change name, class, color, line weight, opacity |
| `vw_find_objects` | Powerful criteria-based search (T=WALL, C='Furniture', etc.) |
| `vw_manage_classes` | List, create, delete classes |
| `vw_worksheet` | Read/write worksheet cells and ranges |
| `vw_symbol` | List and insert symbols from resource library |
| `vw_export` | Export to PDF, DXF, DWG, or image |
| `vw_import_file` | Import DXF, DWG, or image files |
| `vw_get_document_info` | Document metadata (layers, object count, etc.) |
| `vw_screenshot` | Capture viewport screenshot (Claude can view it) |
| `vw_selection` | Get/set/clear/delete/move/duplicate selected objects |

### Architectural Tools
| Tool | Description |
|------|-------------|
| `vw_create_wall` | Create parametric walls with height and thickness |
| `vw_insert_door` | Insert parametric door (place near wall for auto-insertion) |
| `vw_insert_window` | Insert parametric window with sill height |
| `vw_create_slab` | Create floor slab from polygon footprint (3D extrusion) |
| `vw_create_roof` | Create roof from footprint with slope, overhang, bearing height |
| `vw_inspect_object` | **Power tool** — discover ALL parameters of any VW object |

## Stopping the Listener

Either:
- Create a file named `STOP` in the `bridge` folder
- Or stop the script in VW Script Editor

## Troubleshooting

**"TIMEOUT: Vectorworks did not respond"**
- Is the listener running in VW Script Editor?
- Does the BRIDGE_PATH in vw_listener.py match the one server.py uses?
- Check that bridge/requests/ and bridge/responses/ folders exist

**"Handle not found"**
- Handles are only valid for the current listener session
- If you restart the listener, old handles are lost
- Use vw_get_objects or vw_find_objects to get fresh handles

**Listener stops unexpectedly**
- VW Script Editor may have a timeout for long scripts
- Check VW preferences for script execution settings
- As a workaround, restart the listener when needed

## Bridge Path

Default: `./bridge` (relative to server.py location)

Override with environment variable:
```cmd
set VW_BRIDGE_PATH=C:\your\custom\path
```
