# Native Bridge Handler Matrix

This matrix is the implementation map for the native Vectorworks SDK bridge.
Every action in `vw_listener.py` must appear here before native work is called
complete.

Legend:

- Safety: `read`, `write`, or `destructive`.
- Context: all real CAD handlers must run on the Vectorworks SDK main/plugin
  event context, never directly from a socket worker thread.
- Native phase: `0` is transport, `1` is minimum smoke-test parity, `2+` are
  broader handler groups.

| Action | Python handler | Safety | Context | Native phase | Smoke test |
|--------|----------------|--------|---------|--------------|------------|
| `ping` | transport status | read | transport-only is allowed | 0 | Returns `native_bridge: true`, `cad_api_safe: true`, version, and handler count |
| `stop` | `handle_stop` | write | transport plus plugin unload/stop path | 0 | Releases port `9877` |
| `get_document_info` | `handle_get_document_info` | read | main/plugin event context | 1 | Returns filename, path, layer count, object count |
| `get_layers` | `handle_get_layers` | read | main/plugin event context | 1 | Lists layers repeatedly without freezing Vectorworks |
| `get_objects` | `handle_get_objects` | read | main/plugin event context | 1 | Lists objects with layer/type filters |
| `selection` | `handle_selection` | mixed/destructive | main/plugin event context | 1 | `get` and `clear` work; `delete` requires explicit destructive test |
| `create_object` | `handle_create_object` | write | main/plugin event context | 1 | Creates and then deletes a test rectangle |
| `set_property` | `handle_set_property` | write | main/plugin event context | 2 | Changes name/class/color/line weight on a test object |
| `find_objects` | `handle_find_objects` | read | main/plugin event context | 2 | Criteria search returns known test object |
| `manage_classes` | `handle_manage_classes` | mixed/destructive | main/plugin event context | 2 | Lists and creates a temporary class; delete has separate destructive check |
| `worksheet` | `handle_worksheet` | mixed/write | main/plugin event context | 3 | Lists worksheets and reads/writes a temporary cell |
| `symbol` | `handle_symbol` | mixed/write | main/plugin event context | 3 | Lists symbols and inserts a known symbol in a test document |
| `export` | `handle_export` | write | main/plugin event context | 3 | Exports test document to a temporary file or opens expected export dialog |
| `import_file` | `handle_import_file` | write | main/plugin event context | 3 | Imports a temporary DXF/image fixture |
| `screenshot` | `handle_screenshot` | read/write-file | main/plugin event context | 3 | Writes a screenshot/image to the requested path |
| `run_script` | `handle_run_script` | destructive/open-ended | main/plugin event context | 4 | Disabled by default or explicitly gated as trusted code execution |
| `create_wall` | `handle_create_wall` | write | main/plugin event context | 4 | Creates and deletes a temporary wall |
| `insert_door` | `handle_insert_door` | write | main/plugin event context | 4 | Inserts a door into a temporary wall/document |
| `insert_window` | `handle_insert_window` | write | main/plugin event context | 4 | Inserts a window into a temporary wall/document |
| `create_slab` | `handle_create_slab` | write | main/plugin event context | 4 | Creates and deletes a slab from a test footprint |
| `create_roof` | `handle_create_roof` | write | main/plugin event context | 4 | Creates and deletes a roof from a test footprint |
| `inspect_object` | `handle_inspect_object` | read | main/plugin event context | 4 | Reports plugin/object parameters for a selected test object |

## Native Acceptance Rule

For each row, native implementation is only accepted after:

- The handler is implemented in the native bridge or explicitly marked deferred.
- Socket work and JSON framing remain off the Vectorworks API path.
- The CAD/API portion is marshaled to the Vectorworks main/plugin event context.
- A smoke test is recorded in `ACCEPTANCE.md` or a future automated Vectorworks
  smoke-test script.
