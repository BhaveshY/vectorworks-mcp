# Native Source Scaffold

This folder contains the reviewed, SDK-agnostic native bridge scaffold. It is
not a standalone build system and it deliberately avoids Vectorworks SDK
includes so this repo still verifies on machines that do not have the SDK.

Files:

- `BridgeProtocol.hpp` / `BridgeProtocol.cpp`: length-prefixed frame constants,
  strict request envelope parsing, and strict response envelope serialization
  from `..\PROTOCOL.md`.
- `BridgeDispatcher.hpp`: phase-0 and phase-1 action map from
  `..\HANDLER_MATRIX.md`, including the worker-thread vs main/plugin-context
  split.
- `CadRequestQueue.hpp`: worker-to-main-context queue abstraction. Socket
  worker code must enqueue CAD/API work and wait for completion; it must not
  call Vectorworks document APIs directly. The queue rejects duplicate request
  ids and applies bounded backpressure before work reaches the Vectorworks
  main/plugin event context.
- `VectorworksMCPBridge.cpp`: SDK hook placeholders for plugin load/unload,
  socket dispatch, stop, and main/plugin event pumping.

The copied scaffold is phase-0 only. Its `ping` response intentionally reports
`transport_only: true` and `cad_api_safe: false` until real Vectorworks SDK
handlers replace the placeholders and pass the smoke harness.

Recommended native flow:

1. Run `..\..\scripts\prepare-native-bridge-source.ps1` to create an ignored
   SDK-backed worktree from the official `ObjectExample`.
2. Run `..\..\scripts\build-native-bridge.ps1` and prove the unmodified example
   builds.
3. Run `..\..\scripts\copy-native-bridge-scaffold.ps1` to copy these reviewed
   scaffold files into `Source\VectorworksMCPBridge` inside the worktree.
4. Run `..\..\scripts\wire-native-bridge-project.ps1` to add the scaffold files
   to the SDK `.vcxproj`, then replace each placeholder with actual SDK entry
   points and handlers.
5. Build, install with `doctor-native-bridge.ps1`, and prove behavior with
   `smoke-native-bridge.ps1`.
