---
name: setup
description: Set up or repair the Vectorworks MCP connector for Claude Code on Windows 11. Use when the user asks to connect Claude Code to Vectorworks, install the Vectorworks MCP plugin/server, make the repo agent-ready, regenerate the Vectorworks listener launcher, or fix missing /mcp or MCP setup.
---

# Vectorworks MCP Setup

Use the plugin scripts from `${CLAUDE_PLUGIN_ROOT}` when available. If that variable is not visible in the shell, resolve this skill's plugin root from the skill path.

## Workflow

1. Resolve the `vectorworks-mcp` repo. Prefer, in order: plugin user config `vectorworks_repo`, `VW_MCP_REPO`, `CLAUDE_PROJECT_DIR`, current directory, then `~/repos/vectorworks-mcp`. If it is missing, the bootstrap wrapper clones `BhaveshY/vectorworks-mcp` into `~/repos/vectorworks-mcp`.
2. Run the plugin bootstrap wrapper:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}\scripts\bootstrap-vectorworks-mcp.ps1"
```

3. Confirm the generated launcher contains:

```python
os.environ["VW_MCP_MODE"] = "dialog"
os.environ["VW_MCP_DIALOG_TIMER_MS"] = "50"
```

4. Tell the user to replace any old Vectorworks Resource Manager or Plug-in Manager script with the generated `vw_load_listener_2024.py`. That loader runs the current `vw_start_listener_2024.py` from disk, so future setup repairs do not leave stale pasted listener code inside Vectorworks.
5. After the user runs the loader inside Vectorworks, run the ping wrapper:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}\scripts\test-vectorworks-listener.ps1"
```

6. If MCP tools are available, call `vw_ping`. If not, explain that the raw listener ping proves Vectorworks is reachable, but Claude Code still needs the plugin/MCP server loaded.

## Notes

- `/mcp` is only an interactive Claude Code command. Do not rely on it in Codex, Cursor, or non-interactive shells.
- The tool name is `vw_ping`, not `vw-ping`.
- If Vectorworks hangs or the raw ping times out while Vectorworks owns the port, the user is probably running a stale foreground/background/timer launcher. Create `~\.vectorworks-mcp\STOP`, regenerate the launcher, and have them replace the Vectorworks script/menu command with the stable `vw_load_listener_2024.py` loader.
- Background and Windows timer modes are transport-only diagnostics. They may answer `vw_ping`, but real CAD handlers can deadlock outside a normal Vectorworks script or plug-in event context.
- The long-term non-modal fix is the native Vectorworks SDK bridge scaffold in the companion `vectorworks-mcp` repo. Do not describe it as installed or production-ready until the SDK prerequisites pass and a compiled bridge has been smoke-tested in Vectorworks.
