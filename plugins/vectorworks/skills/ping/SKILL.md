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
3. If CAD work is next and `vw_preflight_for_cad` is available, call it before any CAD handler.

Interpretation:

- Raw ping fails: Vectorworks is not running the listener, the port is wrong, or an old listener is stuck. Have the user run the generated `vw_load_listener_2024.py` inside Vectorworks.
- Raw ping passes but `vw_ping` is unavailable: the plugin/MCP server is not loaded in this Claude Code session. Start Claude Code with this plugin enabled or from the repo with `.mcp.json` trusted.
- Both pings pass and preflight returns `ok: true` / `cad_api_safe: true`: proceed with Vectorworks CAD actions.
- Ping passes but preflight returns `ok: false`, `cad_api_safe: false`, or `transport_only: true`: do not call CAD handlers. Regenerate/run the dialog launcher or use the compiled native SDK bridge when available.

Use `vw_ping` with an underscore. `/mcp vw-ping` is not the right call pattern.
