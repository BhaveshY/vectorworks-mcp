# Fresh PC Agent Install

Use this when an AI agent is pointed at a new Windows 11 PC and must install or
repair the Vectorworks MCP workflow without guessing script order.

## Baseline Tools

Required:

- Windows 11 PowerShell
- Git, auto-installed by `install.ps1` with winget when missing
- Python 3.10 or newer, auto-installed as Python 3.12 by `install.ps1` with
  winget when missing
- Codex, Claude Code, or another stdio MCP client
- Claude Code with plugin support when using `/plugin` or `/vectorworks:*`
- Vectorworks 2024 or 2025 for real CAD work

If winget is blocked by policy, install Git or Python manually first:

```powershell
winget install --id Git.Git --exact --source winget --accept-package-agreements --accept-source-agreements
winget install --id Python.Python.3.12 --exact --source winget --accept-package-agreements --accept-source-agreements
```

Open a new PowerShell after installing host tools so `git`, `py`, and `python`
are visible on PATH.

## One-Click Agent Install

Use this as the default Codex/direct MCP install command:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/BhaveshY/vectorworks-mcp/main/install.ps1 | iex"
```

The one-click installer clones or updates the repo at
`$env:USERPROFILE\repos\vectorworks-mcp`, installs the repo-local Python
runtime, runs host verification, leaves `.mcp.json` ready for the MCP client to
trust, and returns a guarded native bridge plan. Native non-modal readiness is
the default required outcome. The command does not silently install Visual
Studio, download a large SDK, modify the Vectorworks user Plug-ins folder,
restart Vectorworks, or select the modal Python fallback. It defaults to
`-Client HostOnly`, so it does not write Claude Code user config.

From an existing checkout:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

For machine-readable agent output:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Json
```

For a non-technical Windows PC where the agent should install/check base
dependencies and attempt the native SDK bridge too:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -FullNative -Json
```

`-FullNative` is intentionally a single-shot agent path. It checks or installs
Git and Python first, then drives the guarded native runner with opt-ins for
network access, Visual Studio Build Tools install, large SDK downloads, native
plug-in folder writes, and reboot risk. After the bridge is installed, it
automatically opens or restarts Vectorworks, waits for the native bridge socket,
runs phase-0 transport smoke, and attempts phase-2 production smoke. If
Vectorworks is on the Home/no-document screen, the native bridge opens a
default blank document before write fixtures. If Vectorworks blocks automation
with license, recovery, plug-in approval, or startup prompts, JSON reports
`native_summary.vectorworks_automation_attempted: true` plus the exact
`native_summary.next_command` or `native_summary.acceptance_next_command` to
resume after the prompt is cleared.

`-FullNative` is explicit consent for side effects. Never add it automatically
because a user asked to connect, inspect, diagnose, or plan setup. It cannot be
combined with `-EnablePythonDialogFallback`.

If native setup must be deferred and the user explicitly accepts a modal agent
session that prevents parallel manual Vectorworks use, enable the compatibility
fallback:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -EnablePythonDialogFallback -Json
```

For the bundled helper, the equivalent opt-in is:

```powershell
py -3 .\plugins\vectorworks\bin\vectorworksctl agent-install --repo-path $PWD --allow-python-fallback --json
```

Keep the `VW MCP Listener` dialog open while the agent works. Its modality
blocks manual Vectorworks UI use; close or stop it before the user resumes
manual work. Treat this as degraded fallback readiness, not native production
readiness.

## Preferred Claude Code Plugin Path

Inside Claude Code:

```text
/plugin marketplace add BhaveshY/vectorworks-claude-plugin
/plugin install vectorworks@vectorworks-claude-plugin
/reload-plugins
/vectorworks:setup
```

If an agent is pointed directly at this connector repo instead of the standalone
plugin repo, the connector also exposes a repo-root marketplace entry:

```text
/plugin marketplace add BhaveshY/vectorworks-mcp
/plugin install vectorworks@vectorworks-mcp
/reload-plugins
/vectorworks:setup
```

Then run:

```text
/vectorworks:diagnose
/vectorworks:ping
```

## Direct Connector Setup

For a direct connector checkout used by Codex, Claude Code project MCP, or any
other MCP client:

```powershell
git clone https://github.com/BhaveshY/vectorworks-mcp.git $env:USERPROFILE\repos\vectorworks-mcp
cd $env:USERPROFILE\repos\vectorworks-mcp
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

For Codex/non-Claude installs where you do not want to touch Claude Code config:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-agent.ps1 -Client HostOnly -Verify
```

This prepares host/client files only; it does not satisfy native runtime
readiness or authorize the Python dialog fallback. Then add or trust the repo
`.mcp.json`. It is client-neutral and uses the
repo-relative `scripts/run-mcp-server.ps1` path. If the client launches MCP
servers from outside the repo root, configure the same stdio server with an
absolute `-File C:\path\to\vectorworks-mcp\scripts\run-mcp-server.ps1`.

For the bundled plugin helper from this connector repo:

```powershell
py -3 .\plugins\vectorworks\bin\vectorworksctl agent-install --repo-path $PWD --json
py -3 .\plugins\vectorworks\bin\vectorworksctl doctor --repo-path $PWD --json
```

`agent-install` prepares the MCP host and returns the guarded native bridge
plan. It does not select the Python dialog listener unless
`--allow-python-fallback` is present. Without that explicit escape hatch,
`native_requires_action: true` means setup remains incomplete and the reported
native step is required.

## Required Native Setup

The production non-modal runtime is a compiled Vectorworks SDK bridge. It needs
the official Vectorworks SDK and Visual Studio C++ build tools.

First inspect the plan:

```powershell
py -3 .\plugins\vectorworks\bin\vectorworksctl doctor --repo-path $PWD --json
```

Only after the user accepts large downloads/software installation, run the
guarded native step with the exact missing allow flags from JSON:

```powershell
py -3 .\plugins\vectorworks\bin\vectorworksctl native-next --repo-path $PWD --json --allow-network --allow-install-software --allow-download-large-files --allow-reboot-risk
```

If JSON reports `sdkArchiveCandidates`, reuse the archive:

```powershell
py -3 .\plugins\vectorworks\bin\vectorworksctl native-next --repo-path $PWD --json --sdk-archive-path C:\path\to\SDK.zip --allow-install-software --allow-reboot-risk
```

After a native artifact is built, install only through the guarded doctor/native
runner, then use the launch/smoke helper to restart/open Vectorworks and run
phase-0 stop/port-release smoke. Phase 2 should run in a disposable document
before claiming native production readiness; the phase-2 gate includes native
walls, text, dimensions, verified property edits, class management, and mixed
atomic batches:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-vectorworks-native-smoke.ps1 -VectorworksVersion 2024 -RestartIfRunning -RunPhase2 -AllowWriteFixture -Json
```

## Result Fields

Agents should parse:

- `ok`: the requested runtime is usable now. For `agent-install --json`, this
  matches `setup_complete` and exits nonzero when the requested runtime is not
  usable.
- `command_ok`: the helper command ran far enough to return diagnostics or a
  native setup plan. This can be true while `setup_complete` is false.
- `setup_complete` / `install_complete` / `usable_now`: native production
  readiness is complete by default. These fields may describe degraded modal
  readiness only when `-EnablePythonDialogFallback` or
  `--allow-python-fallback` was explicitly supplied; the handoff must say that
  manual Vectorworks UI use is blocked.
- `user_message`: short install status string safe to show to users.
- `requires_action`: setup still needs the reported action.
- `repo_root`: resolved companion checkout.
- `mcp_config_path`: MCP client config file to trust or add.
- `runner_path`: stdio runner path inside the companion checkout.
- `launcher_path`: generated machine-specific Vectorworks launcher.
- `loader_path`: stable Vectorworks script/menu loader to run in Vectorworks.
- `next_user_step`: concise next human-facing install step.
- `cad_ready`: the requested runtime is safe for CAD handlers. When this is
  true only because the explicit Python fallback is active, it does not imply
  manual UI availability.
- `native_ready`: native bridge setup is complete according to the runner.
- `native_summary`: root installer/native status summary. For `install.ps1
  -FullNative -Json`, parse `native_summary.next_stage`,
  `native_summary.next_command`, `native_summary.missing_allow_flags`,
  `native_summary.bridge_built`, `native_summary.bridge_installed`,
  `native_summary.vectorworks_automation_attempted`,
  `native_summary.phase0_smoke_tested`,
  `native_summary.phase2_smoke_attempted`,
  `native_summary.phase2_smoke_tested`, and
  `native_summary.vectorworks_interaction_required`. If phase 0 passes but
  phase 2 needs a retry, use `native_summary.acceptance_next_command`.
- `native_setup_complete`: native bridge setup is complete according to the runner.
- `native_requires_action`: native bridge setup still has required follow-up
  work unless the user explicitly accepted degraded modal fallback readiness.
- `native_summary.next_stage`: the next native setup stage.
- `native_summary.acceptance_next_command`: the command to resume final native
  production smoke after Vectorworks prompts are cleared.
- `native_summary.missing_allow_flags`: required opt-in switches.
- `python_fallback_ready`: Python fallback launcher/loader setup succeeded.
- `python_fallback_setup`: fallback launcher/loader setup result.
- `listener_doctor.overall`: current Python fallback session state.

Treat `command_ok: true` as "the control command ran and returned a plan", not
as full CAD readiness. By default, do not report setup complete while
`native_requires_action: true`. The only exception is an explicitly requested
Python fallback, which must be labelled modal and incompatible with simultaneous
manual Vectorworks use. Do not call CAD tools unless a smoke-tested native
bridge reports `cad_api_safe: true`, `transport_only: false`,
`main_context_pump_ready: true`, and supports the requested
`implemented_actions` entry, or the user explicitly selected a CAD-safe modal
fallback. Current phase-2 production work requires
`create_wall`, `create_text`, `create_linear_dimension`, `set_property`, and
`manage_classes` when those tool families are requested.
