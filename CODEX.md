# Codex

Repo guide: [AGENTS.md](AGENTS.md). Using Vectorworks: skill `vectorworks`
(`plugins/vectorworks/skills/vectorworks/SKILL.md`) - first call `vw_status()`.

- Install / register: `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1`
  from a normal PowerShell window (not inside the Codex app). It writes one
  `[mcp_servers.vectorworks]` (cmd.exe /d /c `%USERPROFILE%\.vectorworks-mcp\bin\vectorworks-mcp.cmd`,
  `startup_timeout_sec = 30`, `tool_timeout_sec = 180`), raises
  `mcp_optional_startup_grace_ms` to 5000 and points the `vectorworks-mcp`
  marketplace at the local skills-only plugin.
- Restart Codex afterwards. Check: `%USERPROFILE%\.vectorworks-mcp\bin\vectorworksctl.cmd doctor`.
