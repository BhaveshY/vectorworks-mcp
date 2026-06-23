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
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -BuiltArtifact C:\path\to\ObjectExample.vlb -Install -WhatIf
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -BuiltArtifact C:\path\to\ObjectExample.vlb -Install
```

The official SDK example scaffold currently emits `ObjectExample.vlb`; use the
actual built artifact path if the project packaging has been renamed.

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
`main_context_pump_ready: true`, with no schema failures or phase-1
read-handler timeouts. The default smoke phase covers `ping`, `get_document_info`,
`get_layers`, `get_objects`, and read-only `selection.get`, and requires
`implemented_actions` to list the phase-1 bridge capabilities.

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

## Latest Validation Record

Recorded on 2026-06-23 against Vectorworks Architect 2024 on Windows.

- No-Vectorworks checks: `py -3 -m unittest discover -v` passed 177 tests;
  `scripts\check-bundled-plugin-contract.ps1` passed; `git diff --check`
  passed.
- Native build: `scripts\copy-native-bridge-scaffold.ps1 -Force`,
  `scripts\wire-native-bridge-project.ps1`, and
  `scripts\build-native-bridge.ps1` completed successfully. MSBuild reported
  0 warnings and 0 errors.
- Built artifact:
  `native_bridge\worktree\SDKExamples\Output\2024\_Output\Debug\ObjectExample.vlb`
  with SHA-256
  `D4E19AA1CC0310A860A85D3609739310CEAC337E32B343A2A1ADC5E585F80AB8`.
- Install check: `scripts\doctor-native-bridge.ps1 -BuiltArtifact ... -Install
  -Json` reported `installedArtifactMatchesCandidate: true`.
- Live phase-2 smoke:
  `scripts\smoke-native-bridge.ps1 -Phase 2 -PingCount 3 -ReadCount 2
  -TimeoutSeconds 25 -IncludeObjects -AllowWriteFixture -Json` passed with
  `ok: true`, `cad_api_safe: true`, `transport_only: false`,
  `main_context_pump_ready: true`, and native actions for `create_wall`,
  `create_text`, `create_linear_dimension`, `create_object`, and atomic
  `batch_create_objects`.
- Cleanup: a follow-up exact-name cleanup pass removed four leftover smoke
  objects from an earlier failed cleanup attempt.
