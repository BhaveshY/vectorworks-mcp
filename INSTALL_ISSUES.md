# vectorworks-mcp install log

Historical note: this is an archived install log from an early setup attempt,
not the current setup guide. Prefer `README.md`, `AGENTS.md`, and
`scripts\doctor-vectorworks-mcp.ps1` for current agent instructions.

Archived warning: do not follow the old checklist below for listener startup.
Do not paste or run `vw_listener.py` directly in Vectorworks. Current setup
uses the generated stable loader `vw_load_listener_2024.py`, which loads the
current dialog-mode launcher from disk and avoids stale foreground/background
listener code that can freeze Vectorworks.

Install attempt on 2026-05-20 — Windows 11 Pro 10.0.26200, Python 3.12.10, PowerShell + Git Bash.
Repo head at install time: `b81e24e` (after `git pull` from origin/main).

## Outcome

Installed and registered. MCP server boots over stdio and stays alive.
**Not yet end-to-end verified** — Vectorworks 2025 was not running, so the TCP
ping to `vw_listener.py` on `127.0.0.1:9877` was not exercised. Verify after
launching VW and running the listener.

## Issues found during install

### 0. README and repo description claim "Vectorworks 2025" but code targets 2024+

**Severity:** medium — actively misleading for users on VW 2024 (e.g. me — I'm
on VW 2024, not 2025).

**Where:**
- GitHub repo description: "MCP server bridging Claude Code to **Vectorworks 2025**".
- `README.md` heading: "Vectorworks **2025** MCP Server", install step 3 says
  "Open **Vectorworks 2025**".
- But `server.py` and `vw_listener.py` module docstrings both say
  "Vectorworks **2024/2025**".
- The listener code itself is Python-3.9-compatible (f-strings only, no
  walrus, no `match`, no `@dataclass(slots=True)`) — VW 2024 ships Python 3.9,
  so syntactically it should load.
- No `vs.GetVersion` gate, no 2025-only `vs.*` call I can spot on a skim of
  the handlers — looks like the standard `vs` API surface that's been stable
  since at least VW 2022.

**What this means in practice:** Almost certainly works on VW 2024, but the
README scares 2024 users away and there's no CI / test matrix proving it.

**Suggested fix:** Update README title + step 3 + repo description to say
"Vectorworks 2024 / 2025", or add an explicit "tested versions" line. If
there are any 2025-only `vs` calls hiding in the 22 handlers, gate them with
`hasattr(vs, ...)` and surface a clearer error.

**Verification still owed (VW 2024 specific):** confirm `vw_listener.py` runs
from VW 2024 Script Editor without import / syntax errors and that
`vw_ping` round-trips.

### 1. `register-claude-code.ps1` assumes the `claude` CLI is on PATH

**Severity:** medium — blocks the documented one-command install on a machine
that has Claude Code Desktop but not the standalone `claude` CLI.

**Where:** [`scripts/register-claude-code.ps1`](scripts/register-claude-code.ps1) lines 35-50.

**What happened:** The script does `Get-Command claude` and throws
`"Claude Code CLI was not found on PATH"` if absent. On this machine the
Desktop app is installed (Claude Code is currently running from it) but the
`claude` shim is not on PATH — neither under `%APPDATA%\npm`, nor
`%LOCALAPPDATA%\Programs`, nor anywhere reachable from PowerShell or Git Bash.

**Workaround used:** Edited `C:\Users\Bhavesh\.claude.json` directly and added
the `vectorworks` entry under `mcpServers` with the same shape the script
would have produced (`command: "py"`, `args: ["-3", "<server.py>"]`, env vars
for host/port). This is the same effect `claude mcp add-json` has.

**Suggested fix:** Make the script fall back to editing `~/.claude.json`
directly (or the project-scoped `.mcp.json`) when the CLI is missing, behind a
`-NoCli` switch or an auto-detect. At minimum, the README should call this
out — currently README step 2 reads as if `claude mcp add` always works.

### 3. README's "Tools > Plug-ins > Script Editor" path doesn't exist in VW 2024

**Severity:** medium — blocks the README's quick-start path A entirely.
A user on VW 2024 following the README hits a dead end: under
`Tools > Plug-ins` the only relevant item is **"Encrypt Script…"**, not a
"Script Editor".

**Where:** [`README.md`](README.md) Setup step 3, option A.

**What happened in practice (VW 2024):** To run a Python script ad-hoc, the
real paths are:
- **Resource Manager** (`Cmd/Ctrl+R`) → New Resource → Script… → Python
  Script → paste → OK → double-click to run.
- Or `Tools > Plug-ins > Plug-in Manager` → New… → Menu Command → Edit
  Script (Python) → paste → save → add to workspace menus.

**Suggested fix:** Replace README step 3 option A with the Resource Manager
path. Rename option B from "Plug-in Manager" → "Plug-in Manager → New →
Menu Command" and make explicit that **Plug-in Manager** is the dialog name
(not "Script Editor"). Add a screenshot or at least the keyboard shortcut
(`Cmd/Ctrl+R` for Resource Manager).

### 2. README install instructions don't mention the `py -3` launcher caveat

**Severity:** low — cosmetic / consistency.

**Where:** README "Setup → 2. Register the MCP server with Claude Code".

**What happened:** The manual one-liner in the README is
`claude mcp add vectorworks -- python C:\path\to\...\server.py`, but the
PowerShell script (correctly) prefers `py -3` when available, because that's
the recommended Python launcher on Windows 11 and it dodges the
`python` → Microsoft Store stub trap. The README's manual command could leave
a user pointing at the wrong interpreter (e.g. the Store stub, which exits 9009).

**Suggested fix:** Reword README step 2 to use `py -3` in the manual snippet,
matching the script.

## Non-issues (verified during install)

- `py -3 -m pip install -r requirements.txt` succeeded cleanly. fastmcp 3.3.1
  pulled in. No version conflicts on this machine.
- `import server` from a Python REPL succeeds and exposes the FastMCP `mcp`
  object (so all 22 tools register at import time).
- Launching `py -3 server.py` over stdio — the exact invocation Claude Code
  Desktop will use — keeps the process alive with no stderr output. The
  fastmcp banner stays quiet on stdio (good — it would corrupt the JSON-RPC
  stream otherwise).

## Verification still owed at the time of this archived log

- [ ] Launch Vectorworks, paste/run the generated `vw_load_listener_2024.py`
      stable loader, and confirm `vw_ping` reports
      `dispatch_mode=dialog`, `bridge_kind=python_dialog_agent_session`,
      `cad_api_safe=true`, and `transport_only=false`.
- [ ] Restart Claude Code Desktop so it picks up the new `mcpServers` entry,
      then call `vw_ping` and confirm it returns the listener version.
- [ ] Try one geometry call (e.g. `vw_create_object` rectangle) end-to-end.
