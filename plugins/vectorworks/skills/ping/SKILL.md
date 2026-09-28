---
name: ping
description: Check whether Vectorworks is reachable through the native bridge and the Vectorworks MCP tools. Use when the user says ping Vectorworks, vw_ping, test connection, verify bridge, or /mcp is unavailable.
---

# Vectorworks Ping

The default `fast-native` MCP profile has no `vw_ping` tool. The MCP health
check is `vw_status(action="health")`. Call it once; do not poll it in a loop.

If the MCP tools are not loaded, run the control helper raw check instead:

```powershell
vectorworksctl ping
```

Interpretation of `vw_status(action="health")`:

- `ok: true`: the bridge is CAD-ready (`native_phase >= 4`,
  `cad_api_safe: true`, `transport_only: false`, ready main-context pump).
  Proceed with the task; other tools run their own preflight.
- `error.code="bridge_unreachable"`: nothing is listening on
  `127.0.0.1:9877`. Vectorworks is not running, or the native bridge plug-in is
  not installed/loaded. Ask the user to start Vectorworks, then check once more.
  If it still fails, run `vectorworksctl doctor --json` (see the diagnose
  skill).
- `error.code="vectorworks_busy"`: the bridge is loaded but the Vectorworks
  main thread is not responding (open dialog, long render/export, frozen UI).
  Ask the user to clear it, then check once more.
- `error.code="auth_failed"`: host and bridge tokens differ. Rerun
  `install.ps1` and restart Vectorworks. Do not disable authentication.
- `error.code="preflight_failed"`: the loaded bridge is too old or not
  CAD-safe; `error.detail` lists what is missing. Upgrade/restart the native
  bridge. Do not call CAD tools.

Raw `vectorworksctl ping` results:

- Fails: Vectorworks is not running a bridge or the port is wrong. Run
  `vectorworksctl doctor --json`.
- Times out while Vectorworks owns the port: a stale fallback Python listener
  may hold it; create `~\.vectorworks-mcp\STOP`, wait, and restart Vectorworks
  if needed.
- Passes but the MCP tools are unavailable: the MCP client has not loaded the
  `vectorworks` server. In Claude Code, reload plugins or check `/mcp`. In
  Codex, confirm that the bundled plugin package is installed and loaded. For
  a direct checkout or another client, trust/add the repo `.mcp.json`.
