# Fresh PC Agent Install

Use this when an AI agent is pointed at a new Windows 11 PC and must install or
repair the Vectorworks MCP workflow without guessing script order.

## Baseline Tools

Required:

- Windows 11 PowerShell
- Git
- Python 3.10 or newer
- Codex, Claude Code, or another stdio MCP client
- Claude Code with plugin support when using `/plugin` or `/vectorworks:*`
- Vectorworks 2024 or 2025 for real CAD work

If Git or Python are missing, install them first:

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
runtime, generates durable Vectorworks handoff files, runs host verification,
and leaves `.mcp.json` ready for the MCP client to trust. It defaults to
`-Client HostOnly`, so it does not write Claude Code user config.

From an existing checkout:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

For machine-readable agent output:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Json
```

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

Then add or trust the repo `.mcp.json`. It is client-neutral and uses the
repo-relative `scripts/run-mcp-server.ps1` path. If the client launches MCP
servers from outside the repo root, configure the same stdio server with an
absolute `-File C:\path\to\vectorworks-mcp\scripts\run-mcp-server.ps1`.

For the bundled plugin helper from this connector repo:

```powershell
py -3 .\plugins\vectorworks\bin\vectorworksctl agent-install --repo-path $PWD --json
py -3 .\plugins\vectorworks\bin\vectorworksctl doctor --repo-path $PWD --json
```

`agent-install` prepares the MCP server and generated Vectorworks loader through
the Python dialog fallback while also returning the guarded native bridge plan.
If the JSON says `setup_complete: true` and `native_requires_action: true`, the
install is usable now; the native SDK bridge is only an optional non-modal
upgrade path.

## Native Long-Term Setup

The long-term non-modal target is a compiled Vectorworks SDK bridge. It needs
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
runner, then restart/load Vectorworks, run phase-0 stop/port-release smoke, load
the bridge again, run the default phase-1 read smoke, and run the phase-2 write
smoke in a disposable document before claiming native production readiness:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-native-bridge.ps1 -Phase 2 -PingCount 3 -ReadCount 2 -IncludeObjects -AllowWriteFixture -Json
```

## Result Fields

Agents should parse:

- `ok`: the install is usable now. For `agent-install --json`, this matches
  `setup_complete` and exits nonzero when the MCP install is not usable.
- `command_ok`: the helper command ran far enough to return diagnostics or a
  native setup plan. This can be true while `setup_complete` is false.
- `setup_complete` / `install_complete` / `usable_now`: the MCP install is
  usable now. This can be true with the Python dialog fallback even when native
  SDK setup is still pending.
- `user_message`: short install status string safe to show to users.
- `requires_action`: the usable MCP install still needs more action.
- `repo_root`: resolved companion checkout.
- `mcp_config_path`: MCP client config file to trust or add.
- `runner_path`: stdio runner path inside the companion checkout.
- `launcher_path`: generated machine-specific Vectorworks launcher.
- `loader_path`: stable Vectorworks script/menu loader to run in Vectorworks.
- `next_user_step`: concise next human-facing install step.
- `cad_ready`: Python fallback listener is running and safe for CAD handlers.
- `native_ready`: native bridge setup is complete according to the runner.
- `native_setup_complete`: native bridge setup is complete according to the runner.
- `native_requires_action`: native bridge setup still has optional follow-up work.
- `native_summary.next_stage`: the next native setup stage.
- `native_summary.missing_allow_flags`: required opt-in switches.
- `python_fallback_ready`: Python fallback launcher/loader setup succeeded.
- `python_fallback_setup`: fallback launcher/loader setup result.
- `listener_doctor.overall`: current Python fallback session state.

Treat `ok: true` as "the control command ran and returned a plan", not as full
CAD readiness. Do not report `native_requires_action: true` as an install
failure when `setup_complete` is true. Do not call CAD tools unless `cad_ready`
is true or a
smoke-tested native bridge reports `cad_api_safe: true`, `transport_only:
false`, `main_context_pump_ready: true`, and supports the requested
`implemented_actions` entry.
