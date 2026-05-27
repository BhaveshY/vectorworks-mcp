@AGENTS.md

## Claude Code

- Register or refresh this MCP server with:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap-claude-code.ps1 -Verify
```

- Restart Claude Code after registration changes.
- Use `/mcp` to confirm `vectorworks` is listed.
- With Vectorworks open and the generated listener running, call `vw_ping` before any document-modifying tool.
