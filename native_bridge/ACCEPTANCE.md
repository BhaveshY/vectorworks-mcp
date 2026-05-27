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
2. Enable the native bridge plug-in.
3. Confirm Vectorworks can still be clicked, panned, and used while the bridge
   is idle.
4. Run the native smoke harness:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-native-bridge.ps1 -Json
```

Expected result: `ok: true`, `native_bridge: true`, `cad_api_safe: true`, and
no phase-1 read-handler timeouts. The default smoke phase covers `ping`,
`get_document_info`, `get_layers`, and `get_objects`.

5. In a disposable test document, run the explicit write fixture:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-native-bridge.ps1 -AllowWriteFixture -Json
```

Expected result: the bridge creates a uniquely named rectangle, finds it,
proves the selection contains exactly that fixture, deletes it, and verifies
cleanup. If any identity check fails, the smoke harness must skip deletion.

6. For phase-0 transport shutdown, run:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-native-bridge.ps1 -Phase 0 -Stop -Json
```

7. Confirm the JSON report includes `"stop_port_released": true`.

Record:

- Vectorworks version and update number.
- Native bridge commit.
- SDK version.
- Visual Studio version/toolset.
- Any failing handler names and exact errors.
