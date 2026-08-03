# Codex

Use `AGENTS.md` as the main operating guide.

For one-click native-first setup:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/BhaveshY/vectorworks-mcp/main/install.ps1 | iex"
```

From an existing checkout:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

The default command prepares the MCP host, diagnoses native readiness, and
returns the guarded next step. Do not call setup complete until the non-modal
native SDK bridge is built, installed, loaded, and smoke-tested.

For a full non-technical PC install attempt, including dependency checks,
native bridge build/install, automatic Vectorworks launch/restart, and native
smoke attempts:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -FullNative -Json
```

`-FullNative` is explicit consent for network access, software installation,
large downloads, Vectorworks user Plug-ins writes, restart/launch automation,
and disposable-document smoke fixtures. Never add it implicitly or combine it
with `-EnablePythonDialogFallback`.

If the user explicitly accepts a modal compatibility session that blocks
manual Vectorworks use, enable the Python fallback with one of:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -EnablePythonDialogFallback -Json
py -3 .\plugins\vectorworks\bin\vectorworksctl agent-install --repo-path $PWD --allow-python-fallback --json
```

Keep the `VW MCP Listener` dialog open only while Codex controls Vectorworks,
then stop it before manual work. This is degraded fallback readiness, not native
production readiness.

The project `.mcp.json` is intentionally client-neutral and points at
`scripts/run-mcp-server.ps1` with a repo-relative path. If Codex runs MCP
servers from outside the checkout root, configure the same server with an
absolute `-File C:\path\to\vectorworks-mcp\scripts\run-mcp-server.ps1`.

Before CAD work, call `vw_preflight_for_cad` or `vw_ping` and require
`dispatch_mode=native_sdk`, `cad_api_safe=true`, `transport_only=false`, and
`main_context_pump_ready=true`. Accept `dispatch_mode=dialog` only when the user
explicitly enabled the modal Python fallback and has been told that manual UI
use is blocked.
