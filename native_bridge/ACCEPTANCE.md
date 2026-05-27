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
no read-handler timeouts.

5. Create and delete a simple rectangle in a test document.
6. Stop the bridge and confirm port `9877` is released.

Record:

- Vectorworks version and update number.
- Native bridge commit.
- SDK version.
- Visual Studio version/toolset.
- Any failing handler names and exact errors.
