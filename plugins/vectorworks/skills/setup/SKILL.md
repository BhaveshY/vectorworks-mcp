---
name: setup
description: Set up or repair the Vectorworks MCP runtime, Claude Code plugin, or companion checkout on Windows 11. Use when the user asks to connect Claude Code or Codex to Vectorworks, install the Vectorworks plugin/server, make the repo agent-ready, or fix missing MCP setup.
---

# Vectorworks Setup

Use the RADAR-style control helper first. Do not make users run individual
PowerShell scripts unless `vectorworksctl` reports a specific next command.

## Native-First Runtime

For setup or repair:

```powershell
vectorworksctl agent-install --json
```

If `vectorworksctl` is not on PATH, use the plugin-local fallback:

```powershell
py -3 "${CLAUDE_PLUGIN_ROOT}\bin\vectorworksctl" agent-install --json
```

This command resolves or installs the companion `vectorworks-mcp` checkout,
checks the current contract, and asks the native bridge doctor for a structured
next step. Native non-modal readiness is required by default. If JSON reports
`native_requires_action: true`, setup is incomplete unless the user explicitly
selected the modal Python fallback. `command_ok` only means the helper produced
diagnostics; `ok`, `setup_complete`, and `usable_now` mean the requested runtime
can actually be used. Use
top-level `mcp_config_path`, `loader_path`, `runner_path`, and `next_user_step`
for the user handoff. Follow `native_plan.nextCommandSpec` for required native
setup; do not improvise SDK, Visual Studio, or Vectorworks plug-in install
commands.

For Codex or non-Claude host-only setup, use the companion repo command:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

For a non-technical PC full install attempt including native bridge setup:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -FullNative -Json
```

`-FullNative` is explicit consent for side effects: network access, Visual
Studio installation, large downloads, Vectorworks user Plug-ins writes,
launch/restart automation, and disposable-document smoke fixtures. Never append
it automatically or infer consent from a generic setup request.

If `native_summary.next_stage` is `smoke-phase-0`, native setup is not yet
accepted. A `-FullNative` run may already have attempted to open or restart
Vectorworks and run smoke. If Vectorworks blocks automation with license,
recovery, startup, or plug-in approval prompts, report
`native_summary.next_command` or `native_summary.acceptance_next_command` as
the exact resume command after the prompt is cleared; do not claim completion
until native smoke passes.

Then use the repo `.mcp.json`, or configure the same `powershell.exe -File
scripts\run-mcp-server.ps1` stdio server with an absolute path if the client
does not launch from the repo root.

## Temporary Python Fallback

Use the Python dialog listener only when the user explicitly accepts a modal
compatibility path before the native SDK bridge is built/installed:

```powershell
vectorworksctl agent-install --allow-python-fallback --json
```

For the companion root installer, use:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -EnablePythonDialogFallback -Json
```

That fallback regenerates the stable loader (`vw_load_listener_2024.py`) and
opens the `VW MCP Listener` dialog. The dialog blocks manual Vectorworks UI use;
the user cannot work in Vectorworks in parallel with Codex until it is stopped.
State this before enabling the fallback and label the result degraded/modal,
not native production readiness.

## Rules

- Normal daily use should not run setup. Use `vectorworksctl doctor --json` only
  for troubleshooting.
- `/mcp` is only an interactive Claude Code command. Do not rely on it in Codex,
  Cursor, or non-interactive shells.
- Raw socket reachability is not enough. CAD work requires `cad_api_safe: true`
  and `transport_only: false`.
- The production non-modal path is the compiled Vectorworks SDK bridge. Phase 2
  supports native walls, text, linear dimensions, verified property edits,
  class management, and mixed atomic batches. Python loader repair is explicit
  fallback only and never satisfies the default native completion criterion.
