---
name: diagnose
description: Diagnose the Vectorworks MCP runtime, native bridge, listener, or client integration on Windows. Use when Vectorworks hangs, ping fails, MCP tools are missing, setup stopped working, or native bridge setup is unclear.
---

# Vectorworks Diagnosis

Start with the control helper:

```powershell
vectorworksctl doctor --json
```

The JSON includes `native_plan`, which is the guarded native bridge next step.
Run `native-next` directly only when you need to re-plan that step with
different SDK/artifact paths:

```powershell
vectorworksctl native-next --plan-only --json
```

Follow `nextCommandSpec` and safety fields. `missingAllowFlags`,
`safetyBlocks`, and `validationErrors` are authoritative; do not bypass them.

## Mapping

- `Plugin version`, `Plugin root`, and `Plugin marketplace`: confirm Claude
  Code loaded the expected plugin checkout.
- `Connector git` and `Connector contract`: confirm the plugin resolved the
  expected `vectorworks-mcp` checkout and contract.
- `Generated loader metadata`: relevant only for the Python fallback loader;
  `Generated loader metadata: stale` means regenerate the fallback, not that
  native setup is done.
- `native_plan` or `native-next` reports a bootstrap/build/install stage:
  report that native bridge setup is not complete yet and follow only the
  structured next command.
- Native production readiness means `native_phase >= 2`, `cad_api_safe: true`,
  `transport_only: false`, `main_context_pump_ready: true`, and implemented
  actions for the requested wall/text/dimension/property/class/batch work.
- `sdkArchiveCandidates` exists: reuse the downloaded SDK ZIP with
  `--sdk-archive-path` / `-SdkArchivePath`; do not download another copy.
- `vw_ping` or raw ping reports `cad_api_safe: false` or
  `transport_only: true`: do not call CAD handlers.
- Python listener timeout while Vectorworks owns the port: use the STOP file and
  restart Vectorworks if needed, but treat this as fallback listener recovery,
  not the long-term fix.
- MCP tools absent but raw listener works: the MCP client has not loaded the
  `vectorworks` server. In Claude Code, reload plugins or start Claude Code
  with this plugin. In Codex/direct clients, trust or add the repo `.mcp.json`.
- Tool result contains `blocked: true`: stop and follow the reported reason
  before attempting CAD work.
- Tool result reports `unknown commit state`: do not retry non-idempotent or destructive tools. Use
  read-only inspection after the bridge is stable.

## Fallback Scripts

If `vectorworksctl` is unavailable:

```powershell
python "${CLAUDE_PLUGIN_ROOT}\bin\vectorworksctl" doctor --json
```

Use individual PowerShell scripts only when `vectorworksctl` points to them.
