@AGENTS.md

## Claude Code

- Register or refresh this MCP server with:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-claude-code.ps1 -Verify
```

- Restart Claude Code after registration changes.
- Use `/mcp` to confirm `vectorworks` is listed.
- With Vectorworks open and the native bridge loaded, `vw_status(action="health")` confirms readiness. Every tool preflights itself, so a separate health call before each edit is unnecessary.
- Failures carry `error.code` and `error.next_step`; follow `next_step` instead of retrying in a loop.
