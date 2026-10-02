@AGENTS.md

## Claude Code

- Install / register: `powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1`
  (runs `claude mcp add -s user vectorworks -- cmd.exe /d /c <launcher>` and
  installs the skills-only plugin `vectorworks@vectorworks-mcp`). Restart
  Claude Code, then `/mcp` should list `vectorworks`.
- Using Vectorworks: skill `vectorworks` - first call `vw_status()`; follow
  `error.next_step` on failures instead of retrying in a loop.
- Check the installation: `%USERPROFILE%\.vectorworks-mcp\bin\vectorworksctl.cmd doctor`.
