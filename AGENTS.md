# Agent Instructions

## Project Shape

- `server.py` is the host-side stdio MCP server used by Claude Code.
- `vw_listener.py` runs inside Vectorworks 2024/2025 and listens on TCP `127.0.0.1:9877` by default. Generated launchers normally run it with `VW_MCP_BACKGROUND=1` so Vectorworks returns control to the UI after starting the listener.
- `scripts/run-mcp-server.ps1` is the self-bootstrapping MCP entrypoint. It creates `.venv`, installs `requirements.txt`, then launches `server.py`.
- `scripts/register-claude-code.ps1` is the primary Windows setup command. It is idempotent: it refreshes dependencies, generates `vw_start_listener_2024.py`, and updates the `vectorworks` MCP server entry.
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
- `vw_start_listener_2024.py` with `os.environ["VW_MCP_BACKGROUND"] = "1"`
- project `.mcp.json`
- user `~\.claude.json` when the `claude` CLI is not available

For Claude Code plugin workflow, use:

```powershell
claude --plugin-dir C:\path\to\vectorworks-mcp\plugins\vectorworks
```

Plugin skills are namespaced as `/vectorworks:setup`, `/vectorworks:ping`,
`/vectorworks:diagnose`, and `/vectorworks:work`.

If the generated launcher does not set `VW_MCP_BACKGROUND=1`, rerun
`scripts\register-claude-code.ps1` or `scripts\bootstrap-claude-code.ps1`.

## Safe Verification

Run these before handing work back:

```powershell
.\.venv\Scripts\python.exe -m py_compile server.py vw_listener.py vw_start_listener_2024.py
.\.venv\Scripts\python.exe -m unittest discover -v
powershell -ExecutionPolicy Bypass -File .\scripts\verify-no-vectorworks.ps1
```

If `.venv` does not exist yet, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-mcp-server.ps1 -SetupOnly
```

## Vectorworks Handoff

End-to-end tests require the user to open Vectorworks. Do not claim full end-to-end success unless these have happened:

- Vectorworks 2024/2025 is open.
- The generated `vw_start_listener_2024.py` has been run from Resource Manager or installed as a Plug-in Manager menu command. It should start in background mode and return immediately.
- Claude Code has been restarted after MCP registration.
- `/mcp` shows `vectorworks`.
- First tool call is `vw_ping`; do not treat listener startup as fully proven until this works.

If port `9877` is busy:

- call `vw_stop_listener` if MCP is reachable, or
- create `C:\Users\<user>\.vectorworks-mcp\STOP`, or
- restart Vectorworks.

## Safety

- `vw_run_script` executes trusted Python inside Vectorworks. Ask before using it for destructive document changes.
- Avoid changing user/global MCP configs by hand unless the setup script path is broken. If hand-editing is necessary, back up `~\.claude.json` first.
- Preserve the TCP length-prefixed JSON protocol tests; they are the main no-Vectorworks safety net.
