# Agent Instructions

## Project Shape

- `server.py` is the host-side stdio MCP server used by Claude Code.
- `vw_listener.py` runs inside Vectorworks 2024/2025 and listens on TCP `127.0.0.1:9877` by default. Generated launchers normally run it with `VW_MCP_MODE=dialog`, the only pure-Python mode currently safe for real `vs.*` API calls. Background and Windows timer modes are transport-only diagnostics.
- `native_bridge/` is the long-term native Vectorworks SDK bridge scaffold. It is planned, not compiled, and not wired into `.mcp.json` by default.
- `native_bridge/HANDLER_MATRIX.md` is the handler-by-handler implementation map for the native SDK bridge.
- `native_bridge/mock/mock_bridge.py` is a no-SDK contract harness for host/native protocol compatibility.
- `native_bridge/src/` contains SDK-agnostic native source scaffold files. They are not a standalone build and intentionally avoid Vectorworks SDK includes.
- `scripts/run-mcp-server.ps1` is the self-bootstrapping MCP entrypoint. It creates `.venv`, installs `requirements.txt`, then launches `server.py`.
- `scripts/register-claude-code.ps1` is the primary Windows setup command. It is idempotent: it refreshes dependencies, generates `vw_start_listener_2024.py` plus the stable `vw_load_listener_2024.py` Vectorworks loader, can copy the loader text to the clipboard, and updates the `vectorworks` MCP server entry.
- `scripts/copy-vectorworks-loader.ps1` is the first-class Vectorworks handoff helper. Use it whenever the user or an agent is unsure what to paste into Vectorworks.
- `plugins/vectorworks/` is the Claude Code plugin. Keep its manifest, skills, scripts, and `.mcp.json` aligned with the repo scripts.

## Windows Baseline

- Target environment is Windows 11 PowerShell.
- Prefer `powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File ...` for setup scripts.
- Prefer `py -3` for ad-hoc Python checks, but setup scripts should use the repo-local `.venv` after bootstrap.
- Do not assume `python` points to a real interpreter; on Windows it can be a Microsoft Store alias.

## Bootstrap

Use this when an agent is pointed at a fresh checkout:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-agent.ps1 -Verify
```

Equivalent Claude Code-specific command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-claude-code.ps1 -Verify
```

This does not require Vectorworks. It should create/update:

- `.venv\`
- `vw_start_listener_2024.py` with `os.environ["VW_MCP_MODE"] = "dialog"`
- `vw_load_listener_2024.py`, the stable script/menu loader to paste into Vectorworks
- clipboard contents set to that stable loader script when the desktop clipboard is available
- project `.mcp.json`
- user `~\.claude.json` when the `claude` CLI is not available

For Claude Code plugin workflow, use:

```powershell
claude --plugin-dir C:\path\to\vectorworks-mcp\plugins\vectorworks
```

Plugin skills are namespaced as `/vectorworks:setup`, `/vectorworks:ping`,
`/vectorworks:diagnose`, and `/vectorworks:work`.

If the generated launcher does not set `VW_MCP_MODE=dialog`, rerun
`scripts\register-claude-code.ps1` or `scripts\bootstrap-claude-code.ps1`.

## Bridge Modes

| Mode | Use | CAD/API handlers |
|------|-----|------------------|
| Python `dialog` | Current safe fallback agent session | Allowed |
| Python `foreground` | Legacy diagnostic only; can block the UI | Must reject |
| Python `background` | Transport diagnostics only | Must reject |
| Python `win_timer` | Transport diagnostics only | Must reject |
| Native SDK bridge | Long-term non-modal target | Not available until compiled and installed |

Do not route users to `background` or `win_timer` for real Vectorworks work.
Host tools whose `TOOL_SAFETY` entry has `requires_cad_preflight: true`
auto-block when bridge status is missing or reports `cad_api_safe: false` /
`transport_only: true`; treat that block as authoritative and fix the listener
before retrying CAD work.
Do not claim native non-modal support is installed unless a compiled bridge has
been built from the Vectorworks SDK and smoke-tested in Vectorworks.
Keep the native handler matrix in sync whenever `vw_listener.py` adds, removes,
or renames a handler.

## Safe Verification

Run these before handing work back:

```powershell
.\.venv\Scripts\python.exe -m py_compile server.py vw_listener.py vw_start_listener_2024.py vw_load_listener_2024.py
.\.venv\Scripts\python.exe -m unittest discover -v
powershell -ExecutionPolicy Bypass -File .\scripts\verify-no-vectorworks.ps1
```

For fast diagnosis during setup or while Vectorworks is open, prefer:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\doctor-vectorworks-mcp.ps1
```

Native SDK bridge readiness is separate and advisory unless the user is
specifically working on the native bridge:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check-native-bridge-prereqs.ps1 -Advisory
```

For native bridge implementation work, first prepare an ignored SDK example
worktree and prove the unmodified Vectorworks example builds:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-native-bridge.ps1 -InstallVisualStudioBuildTools -DownloadSdk -CloneSdkExamples -PrepareSource
powershell -ExecutionPolicy Bypass -File .\scripts\prepare-native-bridge-source.ps1 -CloneSdkExamples
powershell -ExecutionPolicy Bypass -File .\scripts\build-native-bridge.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\copy-native-bridge-scaffold.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\build-native-bridge.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -Json
powershell -ExecutionPolicy Bypass -File .\scripts\smoke-native-bridge.ps1 -Json
```

The installer flags are opt-in because they can download large SDK files and
launch the Visual Studio Build Tools installer.

If `.venv` does not exist yet, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-mcp-server.ps1 -SetupOnly
```

## Vectorworks Handoff

End-to-end tests require the user to open Vectorworks. Do not claim full end-to-end success unless these have happened:

- Vectorworks 2024/2025 is open.
- The generated `vw_load_listener_2024.py` has been copied with `scripts\copy-vectorworks-loader.ps1`, then pasted into Resource Manager or installed as a Plug-in Manager menu command. It should run the current `vw_start_listener_2024.py` from disk and open a `VW MCP Listener` dialog; leave that dialog open while the agent controls Vectorworks, then stop/close it for manual work.
- Claude Code has been restarted after MCP registration.
- `/mcp` shows `vectorworks`.
- First tool call is `vw_ping`; do not treat listener startup as fully proven until this works.
- Before real CAD work, prefer `vw_preflight_for_cad` when available. If it blocks, do not call CAD handlers.

If port `9877` is busy:

- call `vw_stop_listener` if MCP is reachable, or
- create `C:\Users\<user>\.vectorworks-mcp\STOP`, or
- restart Vectorworks.

## Safety

- `vw_run_script` executes trusted Python inside Vectorworks. Ask before using it for destructive document changes.
- Avoid changing user/global MCP configs by hand unless the setup script path is broken. If hand-editing is necessary, back up `~\.claude.json` first.
- Preserve the TCP length-prefixed JSON protocol tests; they are the main no-Vectorworks safety net.
