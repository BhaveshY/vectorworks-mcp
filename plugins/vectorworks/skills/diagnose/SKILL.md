---
name: diagnose
description: Diagnose Vectorworks MCP connection failures on Windows. Use when Vectorworks hangs, vw_ping fails, MCP tools are missing, Claude Code cannot see the plugin, the listener port is busy, or setup worked before but stopped.
---

# Vectorworks MCP Diagnosis

Start with the deterministic diagnosis script:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}\scripts\diagnose-vectorworks-mcp.ps1"
```

Then map the result:

- `Repo: NOT FOUND`: configure plugin `vectorworks_repo`, set `VW_MCP_REPO`, or start Claude Code from the `vectorworks-mcp` repo.
- `Launcher agent-session mode: False`: run `/vectorworks:setup` or the bootstrap wrapper, then replace the old script inside Vectorworks.
- `Listener TCP ... reachable: False`: Vectorworks is not listening. Start Vectorworks and run the generated launcher.
- `Listener TCP ... reachable: True` plus raw ping timeout: Vectorworks owns the port, but the Python listener is not processing frames. Create `~\.vectorworks-mcp\STOP`; if it remains timed out, save work, restart Vectorworks, regenerate the dialog agent-session launcher, and rerun it.
- `vw_ping` passes but CAD handlers time out: the launcher is probably running in background or Windows timer mode. Regenerate the launcher and replace the old Vectorworks script with the dialog agent-session version.
- Raw listener ping passes but MCP tools are absent: plugin/MCP config is not loaded. Reload plugins or start Claude Code with `--plugin-dir`.
- Port busy or stale listener: call `vw_stop_listener` if available, otherwise create `~\.vectorworks-mcp\STOP`, wait, and restart Vectorworks if needed.

Avoid asking the user to use `/mcp` unless they are definitely inside Claude Code interactive mode and the command exists.
