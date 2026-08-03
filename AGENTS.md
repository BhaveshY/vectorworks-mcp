# Agent Instructions

For fresh Windows PC onboarding, follow `AGENT_INSTALL.md` first.

## Project Shape

- `server.py` is the host-side stdio MCP server used by Codex, Claude Code, and other MCP clients.
- `vw_listener.py` is the explicit compatibility fallback inside Vectorworks 2024/2025 and listens on TCP `127.0.0.1:9877` by default. It may be selected only through `-EnablePythonDialogFallback` or `--allow-python-fallback`. Generated launchers run it with `VW_MCP_MODE=dialog`, the only pure-Python mode currently safe for real `vs.*` API calls; that dialog is modal and blocks manual Vectorworks UI use. Background and Windows timer modes are transport-only diagnostics.
- `native_bridge/` is the required production Vectorworks SDK bridge source. Phase 4 is the default fast-write runtime: it adds true polygon/polyline creation and idempotent atomic `apply_operations` transactions on top of phase-1 reads/primitives, phase-2 walls/text/dimensions/property/class handlers, and phase-3 compact queries. It must be built, installed into Vectorworks, and smoke-tested before normal non-modal use.
- `vw_capabilities` reports current bridge/native support and should be used when choosing between native-safe helpers and broader legacy tools.
- `vw_drawing_summary` is the preferred read-only production snapshot after preflight and before/after non-trivial edits; upgraded native bridges return compact counts/bounds/examples without dumping every object.
- `vw_batch_create_objects`, `vw_plan_schematic_floor_plan`, `vw_create_schematic_floor_plan`, `vw_create_schematic_room`, `vw_create_schematic_door`, and `vw_create_schematic_window` are drafting helpers. `atomic=true` requires the native bridge and creates all objects in one native undo event; phase 2 supports true walls, text, linear dimensions, verified property edits, native class management, and mixed batches. Schematic helpers still create 2D drafting geometry, not BIM doors/windows/spaces.
- `native_bridge/HANDLER_MATRIX.md` is the handler-by-handler implementation map for the native SDK bridge.
- `native_bridge/mock/mock_bridge.py` is a no-SDK contract harness for host/native protocol compatibility.
- `native_bridge/src/` contains SDK-agnostic native source scaffold files. They are not a standalone build and intentionally avoid Vectorworks SDK includes.
- `scripts/run-mcp-server.ps1` is the self-bootstrapping MCP entrypoint. It creates `.venv`, installs `requirements.txt`, then launches `server.py`.
- `install.ps1` is the primary one-click Windows installer. It can run from a checkout or from the raw GitHub URL, checks/installs base Git/Python dependencies, clones/updates the repo when needed, then reports guarded native readiness. Native non-modal readiness is the default completion criterion. Use `-FullNative` only when the user explicitly consents to the side-effecting native SDK bridge setup, Vectorworks open/restart, and smoke automation. Use `-EnablePythonDialogFallback` only when the user explicitly accepts a modal fallback that blocks manual UI use.
- `scripts/bootstrap-agent.ps1` is the checkout-level setup implementation. It refreshes dependencies and updates client registration unless `-Client HostOnly` is used. Only `-EnablePythonDialogFallback` generates `vw_start_listener_2024.py` plus the stable `vw_load_listener_2024.py` loader and permits clipboard handoff.
- `scripts/register-claude-code.ps1` is the Claude Code registration helper used by the Claude-specific bootstrap path.
- `scripts/copy-vectorworks-loader.ps1` is the first-class Vectorworks handoff helper. Use it whenever the user or an agent is unsure what to paste into Vectorworks.
- `plugins/vectorworks/bin/vectorworksctl` is the stable RADAR-style helper.
  Prefer `py -3 .\plugins\vectorworks\bin\vectorworksctl agent-install --repo-path $PWD --json`
  for fresh-PC setup and `py -3 .\plugins\vectorworks\bin\vectorworksctl doctor --repo-path $PWD --json`
  for diagnosis.
- `plugins/vectorworks/` is the Claude Code plugin. Keep its manifest, skills, scripts, and `.mcp.json` aligned with the repo scripts.
- The root `.mcp.json` is client-neutral and repo-relative for Codex/Claude Code project MCP loading. Do not put Claude-only variables such as `${CLAUDE_PROJECT_DIR}` back into the root config; plugin configs may still use `${CLAUDE_PLUGIN_ROOT}`.

## Windows Baseline

- Target environment is Windows 11 PowerShell.
- Prefer `powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File ...` for setup scripts.
- Prefer `py -3` for ad-hoc Python checks, but setup scripts should use the repo-local `.venv` after bootstrap.
- Do not assume `python` points to a real interpreter; on Windows it can be a Microsoft Store alias.

## Bootstrap

Use this when an agent is pointed at a fresh checkout and only the connector is
being installed:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

This prepares the host and returns the native plan. It does not authorize
large downloads, software installation, user Plug-ins writes, Vectorworks
restart, write fixtures, or the modal Python fallback.

For a non-technical PC full native install attempt, only after explicit
side-effect consent:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -FullNative -Json
```

Use this when the bundled plugin helper is available:

```powershell
py -3 .\plugins\vectorworks\bin\vectorworksctl agent-install --repo-path $PWD --json
```

The helper is native-required by default. Add `--allow-python-fallback` only
after the user accepts that Vectorworks becomes modal and cannot be used
manually while the fallback dialog is open.

Equivalent Claude Code-specific command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-claude-code.ps1 -Verify
```

Codex/non-Claude host-only setup:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-agent.ps1 -Client HostOnly -Verify
```

This host-only bootstrap does not require Vectorworks and does not establish
production runtime readiness. It should create/update:

- `.venv\`
- project `.mcp.json`
- user `~\.claude.json` when the Claude Code setup path is used and the `claude` CLI is not available

Only with `-EnablePythonDialogFallback` should it also create
`vw_start_listener_2024.py`, `vw_load_listener_2024.py`, and optional clipboard
handoff text. Those artifacts select a modal fallback only when the user runs
the loader; they are not native readiness.

For Claude Code plugin workflow, use:

```powershell
claude --plugin-dir C:\path\to\vectorworks-mcp\plugins\vectorworks
```

Plugin skills are namespaced as `/vectorworks:setup`, `/vectorworks:ping`,
`/vectorworks:diagnose`, and `/vectorworks:work`.

If the user explicitly selected the Python fallback and the generated launcher
does not set `VW_MCP_MODE=dialog`, rerun `scripts\bootstrap-agent.ps1 -EnablePythonDialogFallback -Verify`
or, for host-only clients,
`scripts\bootstrap-agent.ps1 -Client HostOnly -EnablePythonDialogFallback -Verify`. Do not use launcher
generation as evidence of native readiness.

## Bridge Modes

| Mode | Use | CAD/API handlers |
|------|-----|------------------|
| Python `dialog` | Explicit modal fallback; blocks manual UI | Allowed only after fallback opt-in |
| Python `foreground` | Legacy diagnostic only; can block the UI | Must reject |
| Python `background` | Transport diagnostics only | Must reject |
| Python `win_timer` | Transport diagnostics only | Must reject |
| Native SDK bridge | Required non-modal production runtime | Phase-4 `apply_operations` fast path plus advertised native actions |

Do not route users to `background` or `win_timer` for real Vectorworks work.
Do not route users to Python `dialog` implicitly. It requires
`-EnablePythonDialogFallback` or `--allow-python-fallback` plus a clear warning
that the `VW MCP Listener` dialog blocks parallel manual Vectorworks use.
Host tools whose `TOOL_SAFETY` entry has `requires_cad_preflight: true`
auto-block when bridge status is missing or reports `cad_api_safe: false` /
`transport_only: true`; treat that block as authoritative and fix the listener
before retrying CAD work.
Do not claim native non-modal CAD support is installed unless a compiled bridge
has been built from the Vectorworks SDK and phase-0 stop, phase-1/phase-2
read/write smoke tests, and one phase-4 transaction smoke pass in Vectorworks. The host must block native actions or variants
that are not present in the bridge `implemented_actions` surface instead of
forwarding them as unknown bridge actions.
For normal Codex work, require phase 4 and `apply_operations`; do not silently
fall back to batch decomposition, legacy Python handlers, or the modal listener.
Keep the native handler matrix in sync whenever `vw_listener.py` adds, removes,
or renames a handler.
Do not mark the default setup complete merely because the host MCP server,
Python launcher, or modal fallback is available. Without explicit fallback
consent, `native_requires_action: true` is required follow-up.

## Safe Verification

Run these before handing work back:

```powershell
.\.venv\Scripts\python.exe -m py_compile server.py vw_listener.py
.\.venv\Scripts\python.exe -m unittest discover -v
powershell -ExecutionPolicy Bypass -File .\scripts\verify-no-vectorworks.ps1
```

For fast diagnosis during setup or while Vectorworks is open, prefer the
structured helper:

```powershell
py -3 .\plugins\vectorworks\bin\vectorworksctl doctor --repo-path $PWD --json
```

Lower-level fallback:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\doctor-vectorworks-mcp.ps1
```

Native SDK bridge readiness is the required production gate:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check-native-bridge-prereqs.ps1 -Advisory
powershell -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -Json
```

For native bridge implementation work, prefer the native doctor's JSON
`nextCommand` and `nextCommandReason`, but use
`scripts\invoke-native-bridge-next.ps1 -Json` as the first execution loop. It
reads and validates `nextCommandSpec`, blocks on missing safety flags /
allow-flags, reports `status`, `missingAllowFlags`, `safetyBlocks`, and
`validationErrors`, runs executable/arguments as an array, and reruns the native
doctor when `rerunDoctorAfter` is true. Treat `invalid_spec` as a hard stop and
only pass missing allow switches after explicit user review. The manual sequence
below is only a
fallback/reference:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-native-bridge.ps1 -InstallVisualStudioBuildTools -DownloadSdk -CloneSdkExamples -PrepareSource
powershell -ExecutionPolicy Bypass -File .\scripts\prepare-native-bridge-source.ps1 -CloneSdkExamples
powershell -ExecutionPolicy Bypass -File .\scripts\build-native-bridge.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\copy-native-bridge-scaffold.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\wire-native-bridge-project.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\build-native-bridge.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -BuiltArtifact C:\path\to\ObjectExample.vlb -Install -WhatIf
powershell -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -BuiltArtifact C:\path\to\ObjectExample.vlb -Install
# Start/restart Vectorworks and prove phase-0 stop/release first.
powershell -ExecutionPolicy Bypass -File .\scripts\start-vectorworks-native-smoke.ps1 -VectorworksVersion 2024 -RestartIfRunning -Json
```

The installer flags are opt-in because they can download large SDK files and
launch the Visual Studio Build Tools installer. `-FullNative` is an explicit
bundle of those side-effecting permissions; never infer or add it from a normal
setup request.
If `check-native-bridge-prereqs.ps1 -Json` reports `sdkArchiveCandidates`, pass
the candidate through `-SdkArchivePath` so setup reuses the downloaded SDK ZIP
instead of downloading it again.
After phase 0 passes, run `scripts\start-vectorworks-native-smoke.ps1
-RunPhase2 -AllowWriteFixture` in a disposable document before claiming native
production write readiness. Do not run the default native smoke against a
non-SDK/transport-only build; it is only valid after the SDK-backed project is
wired and built.

If `.venv` does not exist yet, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-mcp-server.ps1 -SetupOnly
```

## Vectorworks Handoff

End-to-end tests require the user to open Vectorworks. Do not claim full end-to-end success unless these have happened:

- Vectorworks 2024/2025 is open.
- The compiled native SDK bridge is installed, enabled, and loaded.
- The MCP client has been restarted or reloaded after registration/trust changes.
- `vectorworks` is trusted and loaded in the MCP client; Claude Code users can confirm this with `/mcp`.
- First tool call is `vw_ping`; require `dispatch_mode=native_sdk`,
  `cad_api_safe=true`, `transport_only=false`, and
  `main_context_pump_ready=true`.
- Before real CAD work, prefer `vw_preflight_for_cad` when available. If it blocks, do not call CAD handlers.

Explicit fallback handoff is different: only after
`-EnablePythonDialogFallback` or `--allow-python-fallback`, copy/paste
`vw_load_listener_2024.py`, keep the `VW MCP Listener` dialog open while the
agent works, and tell the user that manual Vectorworks use is blocked until the
dialog is stopped. Label this degraded modal fallback, not native completion.

If port `9877` is busy:

- call `vw_stop_listener` if MCP is reachable, or
- create `C:\Users\<user>\.vectorworks-mcp\STOP`, or
- restart Vectorworks.

## Safety

- Never enable the Python dialog fallback without explicit consent. It is
  CAD-safe but modal and prevents parallel manual Vectorworks use.
- `vw_run_script` executes trusted Python inside Vectorworks. Ask before using it for destructive document changes.
- Avoid changing user/global MCP configs by hand unless the setup script path is broken. If hand-editing is necessary, back up `~\.claude.json` first.
- Preserve the TCP length-prefixed JSON protocol tests; they are the main no-Vectorworks safety net.
