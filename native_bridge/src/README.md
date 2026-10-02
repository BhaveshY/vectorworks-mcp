# Native Bridge Sources

C++17 sources of the Vectorworks native bridge (wire protocol v2). They are
compiled in place by `native_bridge/project/VectorworksMCPBridge.vcxproj`
(`scripts/build-native-bridge.ps1`); nothing is copied into an SDK sample.

| File | Role |
|---|---|
| `BridgeProtocol.*` | framing, request/response envelope, `BridgeError`, `\u` decoding |
| `JsonParams.*` | params parser/getters, JSON helpers, small JSON DOM |
| `NativeTransport.*` | loopback TCP server: port range, exclusive bind, client cap, keepalive, idle close |
| `RequestScheduler.*` | queue, per-request timeouts, async jobs, UI-gate dequeue |
| `UiGate.*` | Win32 classification of modal/tracking/loading states |
| `BridgeRuntime.cpp` | module init/deinit, message-only pump, transport back-off, ping/capabilities/jobs, main-thread dispatch |
| `Discovery.*` | `instances/<pid>.json` discovery file and startup log |
| `DocumentRegistry.*` | document fingerprints, list/activate/new/open/save/close, handle table |
| `ViewCapture.*` | get/set view, fit, capture with restore, export_image, saved views |
| `OrganizationHandlers.*` | layers, stories, class set / detailed list |
| `ScriptEngine.*` | `run_script` (Python / VectorScript) and internal Python helper |
| `VectorworksMCPBridge.cpp` | 0.x handlers (objects, apply_operations, resources, worksheets, ...) |
| `CapabilityRegistry.*`, `NativeDomain.hpp` | action registry and capability manifest |
| `NativeIOHandlers.*`, `DocumentationHandlers.*`, `NativeTransaction.*`, `BimObjectHandlers.*`, `SpaceObjectHandlers.*`, `ParametricObjectAdapter.*`, `NativeObjectFactory.*`, `ResourceWorksheetHandlers.*`, `ViewDocumentHandlers.*` | 0.x subsystems (I/O, documentation, transactions, BIM objects) |

Rules: socket threads never call the SDK; every SDK call happens on the
Vectorworks main thread from the pump, behind the UI gate; window-proc entry
points never let exceptions escape.
