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
4. Run `vw_ping` ten times.
5. Run `vw_get_document_info` ten times.
6. Run `vw_get_layers` ten times.
7. Create and delete a simple rectangle in a test document.
8. Stop the bridge and confirm port `9877` is released.

Record:

- Vectorworks version and update number.
- Native bridge commit.
- SDK version.
- Visual Studio version/toolset.
- Any failing handler names and exact errors.
