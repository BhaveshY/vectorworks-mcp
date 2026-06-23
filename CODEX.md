# Codex

Use `AGENTS.md` as the main operating guide.

For host-only setup from a fresh checkout:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-agent.ps1 -Client HostOnly -Verify
```

The project `.mcp.json` is intentionally client-neutral and points at
`scripts/run-mcp-server.ps1` with a repo-relative path. If Codex runs MCP
servers from outside the checkout root, configure the same server with an
absolute `-File C:\path\to\vectorworks-mcp\scripts\run-mcp-server.ps1`.

Before CAD work, call `vw_preflight_for_cad` or `vw_ping` and require
`cad_api_safe=true` plus `transport_only=false`.
