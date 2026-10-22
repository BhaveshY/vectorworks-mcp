# Agent install runbook (Vectorworks MCP on a new PC)

You are an AI agent (Codex, Claude Code, ...) asked to install Vectorworks MCP on
this Windows PC. If the user only gave you a link to this file, that link is
the whole task. Don't ask what to do; start at step 1 and work through to
step 7 on your own. Stop only where a step tells you to ask the user.

- Run every command in PowerShell.
- Every script prints JSON with `-Json`. Read the JSON; don't guess.
- If you can't read this file locally yet, step 1 clones the repository. Keep
  reading it from `<clone>\AGENT_INSTALL.md`.

**Defaults** (use them unless the user said otherwise):

| What | Default |
|---|---|
| Folder | `%USERPROFILE%\Documents\vectorworks-mcp` |
| Client | Codex only (the installer's default) |
| Claude Code | Add `-Client claude-code` if the user works in Claude Code instead; `-Client all` registers both. |
| Vectorworks version | Every installed supported version (2024, 2025) |

**Talk to the user only when:**

- you need permissions (asked once, at the start);
- Vectorworks has to be closed or started;
- a step says **stop**;
- a cleanup item needs their OK (see "Remove old versions");
- you deliver the final report.

## Rules (always)

- **Never close, restart or kill Vectorworks yourself.** The user may have
  unsaved work. When Vectorworks has to be closed, ask the user to save and
  quit it, then continue.
- Never open, save or modify the user's Vectorworks files. The smoke test only
  creates its own untitled document and closes it without saving.
- Never edit `~/.codex/config.toml`, `~/.claude.json` or plugin caches by hand.
  The installer does it, with backups.
- Never print or copy `%USERPROFILE%\.vectorworks-mcp\auth-token`.
- No admin rights are needed. Don't use `Start-Process -Verb RunAs`.
- Never type passwords or licence data. The Vectorworks licence/login is the
  user's job.
- If your shell is sandboxed (Codex "workspace-write", Claude Code permission
  prompts), you need permission to write to `%USERPROFILE%\.vectorworks-mcp`,
  `%APPDATA%\Nemetschek\Vectorworks\<ver>\Plug-ins`, `~/.codex` and
  `~/.claude.json`, plus network access to github.com and pypi.org. Ask for
  that once, up front. In Codex, ask the user to switch this chat to full
  access, or request escalation for each of these commands.

## 1. Get the code

Use a folder outside OneDrive and outside `~\repos\vectorworks-mcp` (the doctor
reports the latter as a stale pre-1.0 copy).

```powershell
$src = "$env:USERPROFILE\Documents\vectorworks-mcp"
if (Get-Command git -ErrorAction SilentlyContinue) {
    if (Test-Path "$src\.git") { git -C $src pull --ff-only } else { git clone https://github.com/BhaveshY/vectorworks-mcp $src }
} else {
    $zip = "$env:TEMP\vectorworks-mcp-main.zip"
    Invoke-WebRequest https://github.com/BhaveshY/vectorworks-mcp/archive/refs/heads/main.zip -OutFile $zip -UseBasicParsing
    Expand-Archive $zip "$env:TEMP\vectorworks-mcp-main" -Force
    if (Test-Path $src) { Rename-Item $src "$src.old-$(Get-Date -Format yyyyMMddHHmmss)" }   # never delete
    Move-Item "$env:TEMP\vectorworks-mcp-main\vectorworks-mcp-main" $src
}
Get-ChildItem $src -Recurse | Unblock-File
cd $src
```

The repository includes the prebuilt Vectorworks plug-in (`native\2024\`,
`native\2025\`, described by `native\PREBUILT.json`). You don't need
Visual Studio or the Vectorworks SDK.

## 2. Preflight (read-only)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1 -Json
```

The output is `{ready, blockers, checks:[{id, status, detail, fix}], install_command}`.
Fix every `fail` using its `fix`, then run preflight again until `ready` is `true`.

| id | if `fail` / `warn` |
|---|---|
| `python` | Run `winget install --id Python.Python.3.12 -e --scope user --accept-source-agreements --accept-package-agreements`, then **open a new shell** (PATH changes) and rerun preflight. Never use Codex's bundled Python. |
| `vectorworks` | No supported version (2024 or 2025): **stop** and tell the user. "Never been started": ask the user to start Vectorworks once, get past the licence and start dialogs, then quit it. |
| `vectorworks_running` (warn) | Ask the user to **save their work and quit Vectorworks**. Wait for their reply, then continue. |
| `vectorworks_tested` (warn) | Vectorworks 2025 is built but not yet live-tested. Continue; the smoke test in step 5 decides. Include its result in your report. |
| `plugin_<ver>` | The download is incomplete. Repeat step 1. |
| `msix` (warn) | Your shell runs inside a packaged app (the Codex desktop app). Use the MSIX command in step 3. |
| `network` | Ask the user about proxy/firewall access to pypi.org. Without any internet access, use an offline zip built elsewhere with `scripts\package-release.ps1 -IncludeWheels` and install with `-Offline`. |
| `execution_policy` | Group Policy blocks PowerShell scripts. **Stop** and tell the user that IT has to allow it. |
| `clients` (warn) | Codex (the default client) was not found. If the user works in Claude Code instead, add `-Client claude-code` to the install command (`-Client all` registers both). Otherwise install anyway and register later with `scripts\register-clients.ps1 -Client codex`. |

## 3. Install

Vectorworks must be **closed** unless preflight says the plug-in is already up
to date. Run the `install_command` from the preflight output.

Normal shell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -WaitForVectorworksExit -WaitTimeoutSec 300 -Json
```

Inside the Codex desktop app (preflight check `msix` = warn), the installer
can't write the plug-in from your shell. This relay starts it through
explorer.exe outside the package: a console window opens, and the installer's
JSON comes back to you.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-outside-msix.ps1
```

Read the JSON:

- `ok: true`: the install succeeded.
- `steps.native.status`:
  - `ok`: the plug-in is installed.
  - `pending_vectorworks_running`: Vectorworks was still open. Ask the user to quit it and rerun.
  - `no_artifact`: Vectorworks version unsupported. Stop and report.
- `steps.clients`: shows what was registered for Codex and Claude Code. Backups sit next to each config file.
- `warnings`: include them in your report.
- On failure the message names the log file (`%USERPROFILE%\.vectorworks-mcp\logs\install-*.log`). Read its tail.

The installer is idempotent, so rerunning it after fixing something is safe.

## 4. Doctor

```powershell
& "$env:USERPROFILE\.vectorworks-mcp\bin\vectorworksctl.cmd" doctor -Json
```

The output is `{ok, failures, warnings, checks:[{id, status, message, fix}]}`.
`failures` and `warnings` are counts. `status` is one of `ok`, `info`, `warn`
or `fail`. With Vectorworks closed, `discovery` and `bridge_ping` are not
failures. Any `fail` has a `fix`.

### Remove old versions

Always do this, even on a PC that never had the MCP before. Run the doctor
with `-Fix`:

```powershell
& "$env:USERPROFILE\.vectorworks-mcp\bin\vectorworksctl.cmd" doctor -Fix -Json
```

`fixed` in the output lists what was done. Nothing is deleted. Folders and
config backups go to `%USERPROFILE%\.vectorworks-mcp\backups\stale-<time>\`.
`-Fix` handles:

- **Older runtimes** under `.vectorworks-mcp\runtime\` (`old_runtimes`). One
  still used by an open Codex/Claude stays until the user restarts that client.
  Rerun `-Fix` after the restart.
- **Earlier registrations under another name** (`stale_server`, warn), such as
  a 0.x `[mcp_servers.vectorworks-mcp]` in Codex or `vectorworks-old` in Claude Code.
  The config is backed up first, then the entry is removed with
  `codex mcp remove <name>` or `claude mcp remove -s user <name>`.
- **Old copies** in known places (`stale_clone`, `stale_runtime`):
  `~\repos\vectorworks-mcp`, `%LOCALAPPDATA%\VectorworksMCP`, and MSIX
  LocalCache copies made inside the Codex app.
- Client registrations that point anywhere other than the installed launcher.

The installer has already done the rest:

- removed the pre-1.0 `ObjectExample.vlb/.vwr` bridge from the Vectorworks
  Plug-ins folder (backed up);
- disabled other Codex plugins that start a second `vectorworks` server.

`-Fix` doesn't change some things on its own. Handle them like this:

| check | do |
|---|---|
| `stale_server` (info) | It mentions Vectorworks but may not be ours. Show it to the user; run its `fix` only if they confirm it is an old Vectorworks MCP. |
| `claude_plugin` | Run its `fix` (`claude plugin disable <name>`). |
| `stale_cowork` | Tell the user to restart the Claude desktop app so it syncs the current plugin. |
| `claude_project` | A project overrides the server. Show it to the user and run its `fix` only with their OK. |

Don't remove anything else: no other folders, and no other MCP servers.
Finally, run `doctor -Json` again. It should show `failures: 0` and no `old_*`
or warn-level `stale_*` checks.

## 5. Start Vectorworks and smoke-test

1. Ask the user to **start Vectorworks** and get past the licence and start
   dialogs. A fresh install creates the auth token, so Vectorworks has to be
   started *after* the install. The bridge plug-in loads on start.
2. Run the end-to-end smoke test. It uses the installed launcher, exactly as
   Codex and Claude start it:

   ```powershell
   py -3.12 scripts\smoke_installed.py
   ```

   If `py` isn't on PATH yet, use the `python.executable` path from the
   install JSON instead of `py -3.12`.

   It creates walls, a door, a window and a space in a **new untitled
   document**, reads them back, captures an image, and closes the document
   without saving. The last line is `{"ok": true, "passed": 8, "total": 8, ...}`.
   - `vw_status ready` fails: Vectorworks isn't running, or it was started
     before the install. Ask the user to restart it, wait 30 s, and rerun.
   - Anything else fails: rerun once. If it still fails, include the FAIL
     lines in the report and stop.

## 6. Hand over

- Tell the user to **restart Codex and/or Claude Code** (quit them fully,
  including the tray icon). Running sessions, including yours, don't see the
  new `vectorworks` server.
- After the restart, the first call in a chat is `vw_status()`. The skill
  `plugins/vectorworks/skills/vectorworks/SKILL.md` teaches the tools.
- Claude desktop/Cowork in an organisation that syncs the `vectorworks` plugin
  uses the same launcher. The installer keeps exactly one server registered.

## 7. Report

Report in this format:

```
Vectorworks MCP install - <PC name>, <date>
Vectorworks: <versions>, plug-in build <build_id> (live-tested: yes/no)
Python: <version>   Clients registered: <codex / claude-code>
Install: ok | failed (<reason>)    Doctor: <failures>/<warnings>
Removed old versions: <doctor -Fix "fixed" lines, or "none found">
Smoke test: <passed>/<total>  (<failed check names>)
Warnings: <installer + doctor warnings>
User actions still needed: <e.g. restart Codex>
```

## Update, repair, uninstall

- **Update:** `git -C <src> pull --ff-only`, then steps 2 to 5 again.
- **Repair** (from the installed copy): `& "$env:USERPROFILE\.vectorworks-mcp\bin\vectorworksctl.cmd" repair`
- **Uninstall** (Vectorworks closed): `& "$env:USERPROFILE\.vectorworks-mcp\bin\vectorworksctl.cmd" uninstall`.
  It removes our registrations, the plug-in (moved to `backups\`), the runtimes
  and the launcher, and keeps the auth token, logs and backups.

## Known limits (tell the user if relevant)

- Vectorworks 2024 (English UI) is live-tested. 2025 and non-English UIs
  (e.g. German) are expected to work but are untested. The smoke test is the
  check.
- Vectorworks 2026 and older than 2024 are not supported. They need a native
  build against that SDK.
- Imperial-unit documents are untested. Lengths are passed in mm unless a unit
  string is given.

## For maintainers: refreshing the prebuilt plug-in

After changing `native_bridge\src`:

1. Build: `scripts\build-native-bridge.ps1 -VectorworksVersion <ver> -Configuration Release`
2. Run the live acceptance.
3. Run `scripts\update-prebuilt.ps1 -Tested <versions that passed>`.
4. Commit `native\`.

`update-prebuilt.ps1` refuses dirty or mismatched builds. The installer and
preflight warn when `native\PREBUILT.json` is older than the source.
