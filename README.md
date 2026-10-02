# Vectorworks MCP

Lets Codex and Claude Code drive Vectorworks 2024/2025 on Windows: read and
count objects, draw floor plans (walls, doors, windows, slabs, roofs, spaces),
edit, capture images of the drawing to verify work, manage layers / classes /
saved views / worksheets, export sheets to PDF/DWG/IFC and work across several
open documents - through 10 MCP tools, while you keep using Vectorworks.

```
Codex / Claude Code --stdio--> vectorworks-mcp.cmd --> Python host (vectorworks_mcp)
                                                          | TCP 127.0.0.1:9877.., token auth
                                                          v
                                   Vectorworks + VectorworksMCPBridge plug-in (native SDK)
```

## Install (one command)

In a normal PowerShell window (not inside Codex), from this folder or an
extracted release zip:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

This installs the runtime to `%USERPROFILE%\.vectorworks-mcp`, registers the
`vectorworks` MCP server with Codex and Claude Code, installs the skills, and
installs the Vectorworks bridge plug-in if a prebuilt one is available and
Vectorworks is closed. To build/install the plug-in as well (Vectorworks must be
closed; the installer can wait):

```powershell
.\install.ps1 -FullNative -WaitForVectorworksExit
```

Needs Python 3.12 from python.org (`winget install --id Python.Python.3.12 -e --scope user`).
Details, other PCs and uninstall: [INSTALL.md](INSTALL.md).

## Connect

Restart Codex / Claude Code after installing.

- **Codex**: `~/.codex/config.toml` gets one `[mcp_servers.vectorworks]` entry
  (`cmd.exe /d /c %USERPROFILE%\.vectorworks-mcp\bin\vectorworks-mcp.cmd`,
  startup 30 s, tools 180 s, `mcp_optional_startup_grace_ms = 5000`) and the
  skills-only plugin `vectorworks@vectorworks-mcp`.
- **Claude Code**: `claude mcp add -s user vectorworks -- cmd.exe /d /c <launcher>`;
  check with `/mcp`. Skills: plugin `plugins/vectorworks` (marketplace `.claude-plugin/marketplace.json`).

Then ask, for example: *"Check the Vectorworks connection and summarise the
active drawing"* or *"Draw an 8 x 6 m two-room plan with doors and windows and
show me a picture."* The model starts with `vw_status`.

Tools: `vw_status`, `vw_read`, `vw_capture`, `vw_create`, `vw_edit`, `vw_apply`,
`vw_view`, `vw_organize`, `vw_document`, `vw_script` - see
[the skill](plugins/vectorworks/skills/vectorworks/SKILL.md) and the
[tool map](plugins/vectorworks/skills/vectorworks/references/tool-map.md).

## Troubleshooting

```powershell
%USERPROFILE%\.vectorworks-mcp\bin\vectorworksctl.cmd doctor        # human report
%USERPROFILE%\.vectorworks-mcp\bin\vectorworksctl.cmd doctor -Json  # machine report
%USERPROFILE%\.vectorworks-mcp\bin\vectorworksctl.cmd doctor -Fix   # re-register clients, move stale copies to backups
```

| Symptom | Fix |
|---|---|
| Client shows "connector unavailable" / no `vw_*` tools | Run `install.ps1`, restart the client. `doctor` must show one registration per client and MCP startup < 800 ms. |
| `vw_status` says `setup_required` (repair mode) | Run the `repair_command` it prints. |
| `bridge_unreachable` | Start Vectorworks; check the plug-in with `doctor` (native check). |
| `upgrade_required` | Close Vectorworks, run `install.ps1 -FullNative`. |
| `vectorworks_busy` | Close the open dialog / finish the drag in Vectorworks. |
| Old 0.x tool names appear, or two Vectorworks servers | A stale plugin or old registration is active; `doctor` names it and the cleanup command. |

Security: loopback only, per-user token in `%USERPROFILE%\.vectorworks-mcp\auth-token`
(private ACL); `vw_script` runs code inside Vectorworks and can be disabled with
`VW_MCP_ALLOW_SCRIPT=0` in the Vectorworks environment.

Developers: [AGENTS.md](AGENTS.md) (repo map), [docs/V1_CONTRACT.md](docs/V1_CONTRACT.md) (spec).
