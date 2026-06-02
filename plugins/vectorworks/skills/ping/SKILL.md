---
name: ping
description: Check whether Vectorworks is reachable through the native bridge or fallback listener and Claude Code MCP tool. Use when the user says ping Vectorworks, vw_ping, test connection, verify bridge, or /mcp is unavailable.
---

# Vectorworks Ping

Run the control helper raw check:

```powershell
vectorworksctl ping
```

Then call `vw_ping` if the MCP tool is available. If CAD work is next, call
`vw_preflight_for_cad` before any CAD handler.

Interpretation:

- Raw ping fails: Vectorworks is not running a bridge/listener or the port is
  wrong. Run `vectorworksctl doctor --json`.
- Raw ping times out while Vectorworks owns the port: fallback Python listener
  is stale; create `~\.vectorworks-mcp\STOP`, wait, and restart Vectorworks if
  needed.
- Raw ping passes but MCP `vw_ping` is unavailable: Claude Code has not loaded
  the plugin/MCP server.
- Ping/preflight reports `cad_api_safe: true` and `transport_only: false`:
  proceed with CAD actions.
- Ping reports `native_bridge: true`, `native_phase: 0`, or
  `transport_only: true`: transport is alive, but CAD handlers are not ready.
  Do not call CAD tools.

Use `vw_ping` with an underscore. `/mcp vw-ping` is not the right call pattern.
