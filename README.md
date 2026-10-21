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

**Installing with an AI agent?** Tell it: *"Install Vectorworks MCP from
github.com/BhaveshY/vectorworks-mcp following AGENT_INSTALL.md"*. The
[agent runbook](AGENT_INSTALL.md) covers the requirement check
(`scripts\preflight.ps1`), the install, the doctor, a live smoke test and the
report.

By hand: check the requirements, then run the installer in a normal PowerShell
window (not inside Codex), from this folder or an extracted release zip:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -WaitForVectorworksExit
```

The installer:

- puts the runtime in `%USERPROFILE%\.vectorworks-mcp`;
- registers the `vectorworks` MCP server with Codex and Claude Code;
- installs the skills;
- installs the prebuilt Vectorworks bridge plug-in from `native\<version>\`.
  Vectorworks must be closed for this step; the installer can wait. Use
  `-FullNative` to build the plug-in yourself (needs Visual Studio and the SDK).

Needs Python 3.12 from python.org (`winget install --id Python.Python.3.12 -e --scope user`).
After installing, start Vectorworks and run `py -3.12 scripts\smoke_installed.py`.
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

**Existing (big) projects:** the model orients with `vw_read(what="summary")`,
finds objects by type / layer / class / record criteria / name or room label
(`search`) / area (`within`, `near`), looks with `vw_capture(view="layer", ...)`
(sharp plan images that carry an `image_to_world` mapping) and can act on what it
sees with `vw_read(what="query", within_px=[[u1,v1],[u2,v2]])`. Tested on a
7,000-object, 5-storey project: open, find, look, bulk-edit and verify in ~17 s
of tool time (`scripts/e2e_existing_project.py`). Prompts for testing in Codex:
[docs/CODEX_E2E_PROMPT.md](docs/CODEX_E2E_PROMPT.md).

**IFC:** Vectorworks exports IFC only when design layers sit on stories
(`vw_organize(kind="story", action="create", name="Ground Floor", layers=["..."])`);
the first export of a document briefly runs Vectorworks' own export dialog once.

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
| `vectorworks_busy` | Close the open dialog / finish the drag in Vectorworks. The Home screen and the "new version available" notice are closed automatically. |
| Vectorworks asks for the licence server at every start ("Login Settings") | Press Login, or tick "Don't show at startup" in that dialog so unattended starts work. |
| Old 0.x tool names appear, or two Vectorworks servers | A stale plugin or old registration is active; `doctor` names it and the cleanup command. |

Security: loopback only, per-user token in `%USERPROFILE%\.vectorworks-mcp\auth-token`
(private ACL); `vw_script` runs code inside Vectorworks and can be disabled with
`VW_MCP_ALLOW_SCRIPT=0` in the Vectorworks environment.

Developers: [AGENTS.md](AGENTS.md) (repo map), [docs/V1_CONTRACT.md](docs/V1_CONTRACT.md) (spec).
