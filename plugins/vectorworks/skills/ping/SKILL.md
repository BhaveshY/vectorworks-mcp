---
name: ping
description: Check whether Vectorworks is reachable through the Vectorworks MCP listener and Claude Code MCP tool. Use when the user says ping Vectorworks, vw_ping, test the listener, verify the connection, or says /mcp is unavailable.
---

# Vectorworks Ping

Run a two-layer check.

1. Raw listener check:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}\scripts\test-vectorworks-listener.ps1"
```

2. MCP tool check: if the `vw_ping` tool is available in this Claude Code session, call it.

Interpretation:

- Raw ping fails: Vectorworks is not running the listener, the port is wrong, or an old listener is stuck. Have the user run the generated `vw_start_listener_2024.py` inside Vectorworks.
- Raw ping passes but `vw_ping` is unavailable: the plugin/MCP server is not loaded in this Claude Code session. Start Claude Code with this plugin enabled or from the repo with `.mcp.json` trusted.
- Both pass: proceed with Vectorworks CAD actions.

Use `vw_ping` with an underscore. `/mcp vw-ping` is not the right call pattern.
