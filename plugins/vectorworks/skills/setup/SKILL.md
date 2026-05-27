---
name: setup
description: Set up or repair the Vectorworks MCP connector for Claude Code on Windows 11. Use when the user asks to connect Claude Code to Vectorworks, install the Vectorworks MCP plugin/server, make the repo agent-ready, regenerate the Vectorworks listener launcher, or fix missing /mcp or MCP setup.
---

# Vectorworks MCP Setup

Use the plugin scripts from `${CLAUDE_PLUGIN_ROOT}` when available. If that variable is not visible in the shell, resolve this skill's plugin root from the skill path.

## Workflow

1. Resolve the `vectorworks-mcp` repo. Prefer, in order: plugin user config `vectorworks_repo`, `VW_MCP_REPO`, `CLAUDE_PROJECT_DIR`, current directory, then `~/repos/vectorworks-mcp`.
2. Run the plugin bootstrap wrapper:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}\scripts\bootstrap-vectorworks-mcp.ps1"
```

3. Confirm the generated launcher contains:

```python
os.environ["VW_MCP_MODE"] = "win_timer"
os.environ["VW_MCP_DIALOG_TIMER_MS"] = "50"
```

4. Tell the user to replace any old Vectorworks Resource Manager or Plug-in Manager script with the generated `vw_start_listener_2024.py`.
5. After the user runs the launcher inside Vectorworks, run the ping wrapper:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}\scripts\test-vectorworks-listener.ps1"
```

6. If MCP tools are available, call `vw_ping`. If not, explain that the raw listener ping proves Vectorworks is reachable, but Claude Code still needs the plugin/MCP server loaded.

## Notes

- `/mcp` is only an interactive Claude Code command. Do not rely on it in Codex, Cursor, or non-interactive shells.
- The tool name is `vw_ping`, not `vw-ping`.
- If Vectorworks hangs or the raw ping times out while Vectorworks owns the port, the user is probably running a stale foreground/background/modal launcher. Create `~\.vectorworks-mcp\STOP`, regenerate the launcher, and have them paste the new Windows timer launcher into Vectorworks.
