---
name: setup
description: Set up or repair the Vectorworks MCP connector for Claude Code on Windows 11. Use when the user asks to connect Claude Code to Vectorworks, install the Vectorworks MCP plugin/server, make the repo agent-ready, regenerate the Vectorworks listener launcher, or fix missing /mcp or MCP setup.
---

# Vectorworks MCP Setup

Use the plugin scripts from `${CLAUDE_PLUGIN_ROOT}` when available. If that variable is not visible in the shell, resolve this skill's plugin root from the skill path.

## Workflow

1. Resolve the `vectorworks-mcp` repo. For shell setup wrappers, prefer an explicit `-RepoPath` or `VW_MCP_REPO`; the MCP server also receives plugin user config `vectorworks_repo` as `VW_MCP_REPO` at runtime. If the repo is missing, the bootstrap wrapper clones `BhaveshY/vectorworks-mcp` into `~/repos/vectorworks-mcp`.
2. Run the plugin bootstrap wrapper:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}\scripts\bootstrap-vectorworks-mcp.ps1"
```

The bootstrap wrapper regenerates the launcher and stable loader
(`vw_load_listener_2024.py`), verifies them, and copies the stable loader script
to the clipboard by default. Use `-SkipClipboard` only when the shell cannot
access the clipboard.

3. Confirm the generated launcher contains:

```python
os.environ["VW_MCP_MODE"] = "dialog"
os.environ["VW_MCP_DIALOG_TIMER_MS"] = "50"
```

4. Tell the user to paste the clipboard contents into any old Vectorworks Resource Manager or Plug-in Manager script. Paste only `vw_load_listener_2024.py`, never `vw_listener.py`, `vw_start_listener_2024.py`, or old foreground/background/timer launcher code. The stable loader runs the current `vw_start_listener_2024.py` from disk, so future setup repairs do not leave stale pasted listener code inside Vectorworks.

To re-copy the stable loader later without running the full bootstrap:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}\scripts\copy-vectorworks-loader.ps1"
```

5. After the user runs the loader inside Vectorworks, run the ping wrapper:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}\scripts\test-vectorworks-listener.ps1"
```

6. If MCP tools are available, call `vw_ping`. For CAD-safe work, the Python listener must report `dispatch_mode=dialog`, `bridge_kind=python_dialog_agent_session`, `cad_api_safe=true`, and `transport_only=false`. If MCP tools are not available, explain that the raw listener ping proves Vectorworks is reachable, but Claude Code still needs the plugin/MCP server loaded.

## Notes

- `/mcp` is only an interactive Claude Code command. Do not rely on it in Codex, Cursor, or non-interactive shells.
- The tool name is `vw_ping`, not `vw-ping`.
- Raw socket reachability is not enough. A listener that answers ping but reports `transport_only=true` is not safe for CAD handlers.
- If Vectorworks hangs or the raw ping times out while Vectorworks owns the port, the user is probably running a stale foreground/background/timer launcher. Create `~\.vectorworks-mcp\STOP`, wait a few seconds, save/restart Vectorworks if needed, regenerate the launcher, and have them re-copy/paste the stable loader with `scripts\copy-vectorworks-loader.ps1`.
- Background and Windows timer modes are transport-only diagnostics. They may answer `vw_ping`, but real CAD handlers can deadlock outside a normal Vectorworks script or plug-in event context.
- The long-term non-modal fix is the native Vectorworks SDK bridge scaffold in the companion `vectorworks-mcp` repo. For native bridge development, run `${CLAUDE_PLUGIN_ROOT}\scripts\invoke-native-bridge-next.ps1 -Json` first; it follows and validates the doctor `nextCommandSpec` plan and reports `status`, `missingAllowFlags`, `safetyBlocks`, `validationErrors`, and `nextCommandReason`. Copy the reviewed scaffold only after the unmodified SDK example builds, then let the doctor stage `wire-native-bridge-project.ps1` before rebuilding. Do not describe it as installed or production-ready until the SDK prerequisites pass and a compiled bridge has been smoke-tested in Vectorworks.
