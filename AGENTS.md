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
| `native_bridge/live_acceptance_v2.py` | live native acceptance (creates and discards its own untitled documents) |
| `scripts/e2e_connectivity.py` | live: one MCP session (installed launcher) must survive a full Vectorworks restart |
| `scripts/codex_e2e.py` | live: real `codex exec` scenarios (weak + strong model) with tool-call/error summary |
| `scripts/dev-restart-vectorworks.ps1` | test-PC helper: discard untitled docs, restart Vectorworks, press the licence Login, wait for the bridge |
| `scripts/verify-no-vectorworks.ps1` | everything checkable without Vectorworks (CI entry point) |
| `scripts/check-skill-tool-names.py` | fails on unknown `vw_*` tools / arguments in skills and docs (`--live` = against tools/list) |
| `plugins/vectorworks/` | the one plugin for Codex (`.codex-plugin/`) and Claude (`.claude-plugin/`): skills only |
| `.agents/plugins/marketplace.json`, `.claude-plugin/marketplace.json` | one marketplace per client (version 1.0.0) |
| `docs/PARITY.md` | every 0.7 tool/action -> 1.0 call |

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

Live (Vectorworks running, test PC only; never while someone works in it):

```powershell
py -3.12 native_bridge\live_acceptance_v2.py     # native bridge, 59 checks
py -3.12 scripts\e2e_connectivity.py             # restart survival, 10 checks
py -3.12 scripts\codex_e2e.py                    # real Codex runs (uses OpenAI quota)
```

Last live results (2026-10-11, Vectorworks 2024): acceptance 59/59, connectivity 10/10;
Codex weak model (gpt-5.6-luna) room scenario 10 calls / 0 errors (52 calls / 12 errors
before the round-2 fixes); strong model (gpt-6-astra) multi-document scenario 25 calls / 0 errors.

Live lessons baked into the bridge: run Python through `ScriptContext_Begin/RunEx`
(bare `ExecuteScript` faults as soon as a script creates objects); Vectorworks buttons
are owner-drawn; VectorScript compile shows an OK box even with dialogs disabled (the
bridge acknowledges handler-owned single-button message boxes); doors/windows live
inside wall containers; the Home screen and update notice block the UI after start.
