---
name: setup
description: Set up or repair the Vectorworks Claude Code plugin/runtime on Windows 11. Use when the user asks to connect Claude Code to Vectorworks, install the Vectorworks plugin/server, make the repo agent-ready, or fix missing MCP setup.
---

# Vectorworks Setup

Use the RADAR-style control helper first. Do not make users run individual
PowerShell scripts unless `vectorworksctl` reports a specific next command.

## Native-First Runtime

For setup or repair:

```powershell
vectorworksctl setup-runtime --json
```

If `vectorworksctl` is not on PATH, use the plugin-local fallback:

```powershell
python "${CLAUDE_PLUGIN_ROOT}\bin\vectorworksctl" setup-runtime --json
```

This command resolves or installs the companion `vectorworks-mcp` checkout,
checks the current contract, and asks the native bridge doctor for a structured
next step. Follow `native_plan.nextCommandSpec`; do not improvise SDK, Visual
Studio, or Vectorworks plug-in install commands.

## Temporary Python Fallback

Use the Python dialog listener only when the user explicitly needs today’s
compatibility path before the native SDK bridge is built/installed:

```powershell
vectorworksctl setup-runtime --include-python-fallback --json
```

That fallback regenerates the stable loader (`vw_load_listener_2024.py`) and
does not make the Python listener the long-term default.

## Rules

- Normal daily use should not run setup. Use `vectorworksctl doctor --json` only
  for troubleshooting.
- `/mcp` is only an interactive Claude Code command. Do not rely on it in Codex,
  Cursor, or non-interactive shells.
- Raw socket reachability is not enough. CAD work requires `cad_api_safe: true`
  and `transport_only: false`.
- The long-term goal is a compiled Vectorworks SDK bridge that auto-loads in
  Vectorworks and owns the local transport. Python loader repair is fallback
  only.
