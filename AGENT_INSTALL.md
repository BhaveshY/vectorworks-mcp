# Fresh PC Agent Install

Use this when an AI agent is pointed at a new Windows 11 PC and must install or
repair the Vectorworks MCP workflow without guessing script order.

## Baseline Tools

Required:

- Windows 11 PowerShell
- Git
- Python 3.10 or newer
- Claude Code with plugin support
- Vectorworks 2024 or 2025 for real CAD work

If Git or Python are missing, install them first:

```powershell
winget install --id Git.Git --exact --source winget --accept-package-agreements --accept-source-agreements
winget install --id Python.Python.3.12 --exact --source winget --accept-package-agreements --accept-source-agreements
```

Open a new PowerShell after installing host tools so `git`, `py`, and `python`
are visible on PATH.

## Preferred Claude Plugin Path

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

For a direct connector checkout:

```powershell
git clone https://github.com/BhaveshY/vectorworks-mcp.git $env:USERPROFILE\repos\vectorworks-mcp
cd $env:USERPROFILE\repos\vectorworks-mcp
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-agent.ps1 -Verify
```

For the bundled plugin helper from this connector repo:

```powershell
py -3 .\plugins\vectorworks\bin\vectorworksctl agent-install --repo-path $PWD --json
py -3 .\plugins\vectorworks\bin\vectorworksctl doctor --repo-path $PWD --json
```

`agent-install` prepares the MCP server and generated Vectorworks loader through
the Python dialog fallback while also returning the guarded native bridge plan.

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
runner, then restart/load Vectorworks and run phase-0 smoke before CAD work.

## Result Fields

Agents should parse:

- `setup_complete`: long-term native setup is complete.
- `requires_action`: more native setup/install steps are still required.
- `cad_ready`: Python fallback listener is running and safe for CAD handlers.
- `native_ready`: native bridge setup is complete according to the runner.
- `native_summary.next_stage`: the next native setup stage.
- `native_summary.missing_allow_flags`: required opt-in switches.
- `python_fallback_ready`: Python fallback launcher/loader setup succeeded.
- `python_fallback_setup`: fallback launcher/loader setup result.
- `listener_doctor.overall`: current Python fallback session state.

Treat `ok: true` as "the control command ran and returned a plan", not as full
CAD readiness. Do not call CAD tools unless `cad_ready` is true or a
smoke-tested native bridge reports `cad_api_safe: true` and
`transport_only: false`.
