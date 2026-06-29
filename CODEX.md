# Codex

Use `AGENTS.md` as the main operating guide.

For one-click host-only setup:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/BhaveshY/vectorworks-mcp/main/install.ps1 | iex"
```

From an existing checkout:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

For a full non-technical PC install attempt, including dependency checks,
native bridge build/install, automatic Vectorworks launch/restart, and native
smoke attempts:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -FullNative -Json
```

The project `.mcp.json` is intentionally client-neutral and points at
`scripts/run-mcp-server.ps1` with a repo-relative path. If Codex runs MCP
servers from outside the checkout root, configure the same server with an
absolute `-File C:\path\to\vectorworks-mcp\scripts\run-mcp-server.ps1`.

Before CAD work, call `vw_preflight_for_cad` or `vw_ping` and require
`cad_api_safe=true` plus `transport_only=false`.
