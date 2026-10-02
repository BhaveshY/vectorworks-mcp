# Agent guide (repo map)

Vectorworks MCP 1.0: an MCP stdio server (Python, `vectorworks_mcp/`) that
talks to a native Vectorworks SDK plug-in (`native_bridge/`) over loopback TCP.
The binding spec is [docs/V1_CONTRACT.md](docs/V1_CONTRACT.md); record any
deviation in its "Deviations" section instead of silently diverging.

To *use* Vectorworks from an agent, read the skill
[plugins/vectorworks/skills/vectorworks/SKILL.md](plugins/vectorworks/skills/vectorworks/SKILL.md)
(first call `vw_status()`; 10 tools; recipes; recovery per error code).

## Layout

| Path | What |
|---|---|
| `vectorworks_mcp/` | the host: stdlib MCP core (`mcp_core.py`), tools (`tools/`), bridge client + discovery (`bridge.py`, `host.py`), builders, capture, error envelope (`errors.py`), usage guide served as `vw://guide` (`guide.md`), `analysis/` (plan quality), `testing/mock_bridge.py` |
| `vectorworks_mcp/repair_server.py` | stdlib-only repair-mode server (only `vw_status` -> `setup_required` + repair command) |
| `legacy/` | frozen 0.7 server (`VW_MCP_TOOL_PROFILE=legacy`), old listener |
| `server.py` | thin shim for old entry points |
| `native_bridge/` | C++ bridge (`src/`, version-controlled project in `project/`, build output `project/out/<ver>/Release/`), protocol docs |
| `install.ps1` | one-command install / repair / `-Uninstall` (contract section 3) |
| `bin/*.cmd.template` | launcher + `vectorworksctl` templates rendered by the installer |
| `scripts/install-common.ps1` | shared PowerShell helpers (paths, MSIX detection, ACL, hashing, native exec) |
| `scripts/register-clients.ps1` + `register_clients.py` | Codex TOML (surgical) / Claude Code registration, inspect, Codex log summary |
| `scripts/doctor.ps1` + `doctor_probe.py` | `vectorworksctl doctor` (startup timing, bridge ping, duplicates, stale installs) |
| `scripts/package-release.ps1` | portable release zip (+ wheels, + prebuilt plug-in) and plugin zip |
| `scripts/build-native-bridge.ps1` & co. | native build / prerequisites / smoke (stream N) |
| `scripts/e2e_host.py` | host end-to-end test against the mock bridge (`--real`: read-only against Vectorworks) |
| `scripts/verify-no-vectorworks.ps1` | everything checkable without Vectorworks (CI entry point) |
| `scripts/check-skill-tool-names.py` | fails on unknown `vw_*` tools / arguments in skills and docs (`--live` = against tools/list) |
| `plugins/vectorworks/` | the one plugin for Codex (`.codex-plugin/`) and Claude (`.claude-plugin/`): skills only |
| `.agents/plugins/marketplace.json`, `.claude-plugin/marketplace.json` | one marketplace per client (version 1.0.0) |
| `docs/PARITY.md` | every 0.7 tool/action -> 1.0 call |
| `docs/archive/` | outdated 0.x documents (do not follow) |

Installed layout and client changes: [INSTALL.md](INSTALL.md).

## Rules

- Windows 11, Python 3.12 from python.org (`py -3.12`); never Codex's bundled Python.
- The MCP launcher path must stay PowerShell-free and do no pip/git/network
  work; stdout carries MCP frames only.
- Never edit `~/.codex/config.toml`, `~/.claude.json` or plugin caches by hand;
  use `scripts/register-clients.ps1` (backups, verification). For tests use
  `-Root`, `-CodexHome`, `-ClaudeConfig`, `-PluginsDir`, `-DryRun`.
- Never install into the Vectorworks Plug-ins folder or restart Vectorworks
  while the user works in it; the installer refuses while Vectorworks runs.
- Docs and skills may only name tools from contract section 2.3; run
  `py -3.12 scripts/check-skill-tool-names.py --live` after changing either.

## Verify

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify-no-vectorworks.ps1   # no Vectorworks needed
py -3.12 scripts\e2e_host.py                                                              # host vs mock bridge
%USERPROFILE%\.vectorworks-mcp\bin\vectorworksctl.cmd doctor                              # installed system
```
