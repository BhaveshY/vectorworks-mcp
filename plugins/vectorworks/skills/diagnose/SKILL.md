---
name: diagnose
description: Diagnose Vectorworks MCP connection failures on Windows. Use when Vectorworks hangs, vw_ping fails, MCP tools are missing, Claude Code cannot see the plugin, the listener port is busy, or setup worked before but stopped.
---

# Vectorworks MCP Diagnosis

Start with the fast doctor wrapper for the clearest next action:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}\scripts\doctor-vectorworks-mcp.ps1"
```

Fallback deterministic diagnosis script:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}\scripts\diagnose-vectorworks-mcp.ps1"
```

Then map the result:

- `Plugin version`, `Plugin root`, and `Plugin marketplace`: confirm Claude Code loaded the expected plugin checkout before debugging Vectorworks.
- `Connector git` and `Connector contract`: confirm the plugin resolved a current `vectorworks-mcp` checkout on the expected branch/head.
- `Generated loader metadata: stale`: regenerate/copy the loader and replace the old script inside Vectorworks with the fresh `vw_load_listener_2024.py`.
- `Repo: NOT FOUND`: configure plugin `vectorworks_repo`, set `VW_MCP_REPO`, or start Claude Code from the `vectorworks-mcp` repo.
- `Launcher agent-session mode: False`: run `/vectorworks:setup` or the bootstrap wrapper, then replace the old script inside Vectorworks with `vw_load_listener_2024.py`.
- `Listener TCP ... reachable: False`: Vectorworks is not listening. Start Vectorworks and run the generated loader.
- `Listener TCP ... reachable: True` plus raw ping timeout: Vectorworks owns the port, but the Python listener is not processing frames. Create `~\.vectorworks-mcp\STOP`; if it remains timed out, save work, restart Vectorworks, regenerate the dialog agent-session launcher, and rerun the loader.
- `vw_ping` passes but CAD handlers time out: the launcher is probably running in background or Windows timer mode. Regenerate the launcher and replace the old Vectorworks script with `vw_load_listener_2024.py`.
- CAD tool returns `blocked: true`: the host-side safety guard prevented a real CAD call. Follow its `reason` and `next_action`; usually regenerate/copy/run the stable loader or switch to a compiled native SDK bridge.
- Native bridge source/build state is unclear: run `${CLAUDE_PLUGIN_ROOT}\scripts\invoke-native-bridge-next.ps1 -Json` first so safety flags are enforced. Use `${CLAUDE_PLUGIN_ROOT}\scripts\doctor-native-bridge.ps1 -Json` only for lower-level inspection of `nextCommand`, `nextCommandReason`, and `nextCommandSpec`.
- CAD tool reports unknown commit state: do not retry non-idempotent or destructive tools. Stabilize the listener, then inspect the document with read-only tools such as `vw_get_document_info`, `vw_get_layers`, `vw_get_objects`, or safe mixed variants from `vw_tool_safety`.
- `vw_ping` returns `cad_api_safe: false` or `transport_only: true`: do not call CAD handlers. Regenerate/copy/run the stable loader for today's workflow, or use the native bridge doctor JSON path if the user is explicitly building the long-term SDK bridge.
- Raw listener ping passes but MCP tools are absent: plugin/MCP config is not loaded. Reload plugins or start Claude Code with `--plugin-dir`.
- Port busy or stale listener: call `vw_stop_listener` if available, otherwise create `~\.vectorworks-mcp\STOP`, wait, and restart Vectorworks if needed.

Avoid asking the user to use `/mcp` unless they are definitely inside Claude Code interactive mode and the command exists.
