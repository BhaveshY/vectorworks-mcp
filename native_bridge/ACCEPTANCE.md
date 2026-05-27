# Native Bridge Acceptance Checklist

Use this checklist before claiming the native bridge is production-ready.

## No-Vectorworks Checks

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify-no-vectorworks.ps1
```

Expected result: unit tests pass and the native prerequisite checker reports
what is installed or missing.

## Vectorworks 2024 Smoke Test

1. Start Vectorworks 2024.
2. Plan or install the compiled bridge artifact with the native doctor:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -BuiltArtifact C:\path\to\VectorworksMCPBridge.vwlibrary -Install -WhatIf
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -BuiltArtifact C:\path\to\VectorworksMCPBridge.vwlibrary -Install
```

3. Enable the native bridge plug-in.
4. Confirm Vectorworks can still be clicked, panned, and used while the bridge
   is idle.
5. If only the phase-0 scaffold is wired, run the transport shutdown check
   first:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-native-bridge.ps1 -Phase 0 -Stop -Json
```

Expected result: `ok: true`, `native_bridge: true`, and
`"stop_port_released": true`. A phase-0 scaffold may still report
`cad_api_safe: false` and `transport_only: true`; that is not production-ready.

6. After real CAD handlers are implemented, run the default native smoke
   harness:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-native-bridge.ps1 -Json
```

Expected result: `ok: true`, `native_bridge: true`, `cad_api_safe: true`, and
no phase-1 read-handler timeouts or schema failures. The default smoke phase
covers `ping`, `get_document_info`, `get_layers`, `get_objects`, and read-only
`selection.get`, and requires `implemented_actions` to list the phase-1 bridge
capabilities.

7. In a disposable test document, run the explicit write fixture:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-native-bridge.ps1 -AllowWriteFixture -Json
```

Expected result: the bridge creates a uniquely named rectangle, finds it,
proves the selection contains exactly that fixture, deletes it, and verifies
cleanup. If any identity check fails, the smoke harness must skip deletion.

8. Confirm the JSON report includes `"stop_port_released": true` for the phase-0
   shutdown check.

Record:

- Vectorworks version and update number.
- Native bridge commit.
- SDK version.
- Visual Studio version/toolset.
- Any failing handler names and exact errors.
