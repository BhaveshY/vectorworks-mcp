# Installing Vectorworks MCP 1.0

Everything installs per Windows user, without admin rights, into the real
user profile (never into an app's private MSIX storage):

```
%USERPROFILE%\.vectorworks-mcp\
  bin\vectorworks-mcp.cmd     MCP launcher (what Codex / Claude start)
  bin\vectorworksctl.cmd      doctor / repair / uninstall
  bin\repair_server.py        stdlib "repair mode" server used when the runtime is missing
  runtime\<version>\app\      server code (vectorworks_mcp, legacy, scripts, plugin, skills)
  runtime\<version>\venv\     Python 3.12 venv with the locked dependencies
  current.txt                 active runtime id
  install-receipt.json        version, file hashes, plug-in hash, client changes
  auth-token                  shared secret between host and bridge (private ACL)
  marketplace\                skills-only Codex plugin marketplace
  instances\ logs\ captures\ backups\
%APPDATA%\Nemetschek\Vectorworks\<2024|2025>\Plug-ins\VectorworksMCPBridge.vlb/.vwr
```

## 1. Fresh PC

An AI agent doing the install follows [AGENT_INSTALL.md](AGENT_INSTALL.md).
Check the requirements first, read-only:
`powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1 [-Json]`.

1. Install Python 3.12 from python.org (Codex's bundled Python is never used):
   `winget install --id Python.Python.3.12 -e --scope user`
2. Get the code: `git clone https://github.com/BhaveshY/vectorworks-mcp`
   or extract a release zip (see 2).
3. Open a **normal** PowerShell window (not a terminal inside the Codex app -
   the installer refuses to write the plug-in from an MSIX-packaged process)
   and run from the folder:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
   ```

   - The installer is idempotent: rerun it at any time; unchanged runtimes are
     reused, changed ones are installed side by side when a client still uses
     the old one.
   - Dependencies are installed once from `requirements.lock` with
     `pip --require-hashes` (internet needed unless the zip contains `wheels\`).
4. Native bridge plug-in (Vectorworks must be closed):

   ```powershell
   .\install.ps1 -FullNative -WaitForVectorworksExit
   ```

   A plain `install.ps1` already installs the prebuilt
   `native\<version>\VectorworksMCPBridge.vlb/.vwr`. These files are tracked in
   git and listed in `native\PREBUILT.json` with their build id, hashes and
   live-test status. They also ship in the release zip, or can come from
   `-NativeArtifactDir`. `-FullNative` builds the plug-in instead with
   `scripts\build-native-bridge.ps1` when Visual Studio C++ tools and the
   Vectorworks SDK are present. The pre-1.0 `ObjectExample.vlb/.vwr` is backed
   up to `backups\plugins-<time>\` and removed.
5. Restart Vectorworks, Codex and Claude Code. Check:

   ```powershell
   %USERPROFILE%\.vectorworks-mcp\bin\vectorworksctl.cmd doctor
   ```

Useful switches: `-Client codex|claude-code|all|none`, `-SkipNative`,
`-VectorworksVersion 2025`, `-Force` (rebuild the venv), `-DryRun`
(print the plan, change nothing), `-Json`. Test/sandbox overrides:
`-Root <dir> -CodexHome <dir> -ClaudeConfig <file> -PluginsDir <dir>`.

## 2. Another PC (portable zip)

On a PC that has the code (and ideally a built plug-in):

```powershell
.\scripts\package-release.ps1 -IncludeWheels
```

This writes `dist\vectorworks-mcp-<version>-win64.zip` (runtime source,
installer, prebuilt `native\<ver>\` plug-in if one was built, and with
`-IncludeWheels` all dependency wheels for offline install) and
`dist\vectorworks-plugin-<version>.zip` (skills-only plugin for upload to a
Claude organisation / Cowork). Copy the first zip to the other PC, extract,
then run step 1.3 there (`-Offline` forces wheel-only installs).

## 3. What the installer changes in client configs

- **Codex** (`%USERPROFILE%\.codex\config.toml`, backup
  `config.toml.vectorworks-mcp-<time>.bak` next to it). Only these entries
  are touched; comments and all other content stay byte-identical:

  ```toml
  mcp_optional_startup_grace_ms = 5000          # only raised, never lowered

  [marketplaces.vectorworks-mcp]
  source_type = "local"
  source = '\\?\C:\Users\<you>\.vectorworks-mcp\marketplace'

  [plugins."vectorworks@vectorworks-mcp"]
  enabled = true                                # skills only, no MCP server

  [mcp_servers.vectorworks]
  command = 'C:\Windows\System32\cmd.exe'
  args = ['/d', '/c', 'C:\Users\<you>\.vectorworks-mcp\bin\vectorworks-mcp.cmd']
  env_vars = [...]
  startup_timeout_sec = 30
  tool_timeout_sec = 180
  enabled = true
  ```

  Other enabled plugins that still define a `vectorworks` MCP server are
  disabled so exactly one server is active.
- **Claude Code**: `claude mcp add -s user vectorworks -- cmd.exe /d /c <launcher>`
  (verified by reading `~/.claude.json` back; falls back to a JSON merge that
  keeps every other key). Skills: install the plugin from this repo's
  marketplace (`/plugin marketplace add <folder>` then `/plugin install vectorworks@vectorworks-mcp`).
- **Claude desktop / Cowork plugin**: the `vectorworks` plugin from the org
  marketplace (github.com/BhaveshY/vectorworks-claude-plugin, 1.0) starts this
  same launcher and ships the skill. When Claude has synced it (1.0 or later),
  the installer removes the user-scope server and the local skills plugin so
  exactly one Vectorworks server runs; without it, the user-scope registration
  above is used. Keep the plugin repo's skill in sync with
  `plugins/vectorworks/skills/vectorworks`.

Register again without reinstalling:

```powershell
.\scripts\register-clients.ps1 -Client all
```

## 4. Uninstall

```powershell
%USERPROFILE%\.vectorworks-mcp\bin\vectorworksctl.cmd uninstall
# or: .\install.ps1 -Uninstall [-Purge]
```

Driven by `install-receipt.json`: removes the client registrations that point
at our launcher, the plug-in files whose hash matches the receipt (Vectorworks
must be closed; files are moved to `backups\`), the runtimes, launcher and
marketplace. Keeps `auth-token`, `logs\` and `backups\` (`-Purge` also removes
the token, captures and discovery files).

## 5. Troubleshooting

`vectorworksctl doctor` checks: runtime + receipt hashes, launcher on the real
filesystem, exactly one `vectorworks` server per client (Codex config +
enabled plugins, Claude user scope + project overrides), stale copies (old
`~\repos\vectorworks-mcp` clone, `%LOCALAPPDATA%\Packages\*\LocalCache\Local\VectorworksMCP`,
old Cowork plugin), execution policy, measured launcher -> initialize ->
tools/list time (budget 800 ms), the native plug-in and its hash, discovery
files + bridge ping, and recent Codex log events (servers skipped at thread
start). Each problem prints the exact fix; `-Fix` applies the safe ones
(re-registration; stale copies are moved to `backups\`, never deleted).

- **Execution policy**: the MCP launcher is a plain `.cmd` and never needs
  PowerShell. Only install/doctor do; if Group Policy enforces `AllSigned`,
  ask IT to allow them.
- **Repair mode**: when the runtime is missing the launcher still starts a
  tiny server whose only tool, `vw_status`, returns `setup_required` and the
  exact repair command - clients never show "connector unavailable".
