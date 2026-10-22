<#
.SYNOPSIS
  Register (or unregister) the Vectorworks MCP launcher with Codex and Claude Code.

.DESCRIPTION
  Codex: surgical edit of <CodexHome>\config.toml through scripts\register_clients.py
  ([mcp_servers.vectorworks] -> cmd.exe /d /c <launcher>, startup_timeout_sec 30,
  tool_timeout_sec 180, mcp_optional_startup_grace_ms >= 5000, local skills-only
  marketplace, duplicate plugin servers disabled). A timestamped backup is written
  next to the file. When the codex CLI is available the skills-only plugin is then
  refreshed with `codex plugin add vectorworks@vectorworks-mcp`.

  Claude Code: `claude mcp add -s user vectorworks -- cmd.exe /d /c <launcher>`
  with an argument array (safe under Windows PowerShell 5.1), verified by reading
  the JSON back. Falls back to a Python JSON merge (never ConvertTo-Json).

.EXAMPLE
  .\scripts\register-clients.ps1 -Client all -Launcher "$env:USERPROFILE\.vectorworks-mcp\bin\vectorworks-mcp.cmd"
.EXAMPLE
  .\scripts\register-clients.ps1 -Client codex -CodexHome C:\temp\codex -Launcher C:\temp\root\bin\vectorworks-mcp.cmd -DryRun
#>
[CmdletBinding()]
param(
    [ValidateSet("all", "codex", "claude-code", "none")]
    [string]$Client = "codex",
    [string]$Launcher = "",
    [string]$Root = "",
    [string]$CodexHome = "",
    [string]$ClaudeConfig = "",
    [ValidateSet("auto", "cli", "json")]
    [string]$ClaudeMethod = "auto",
    [string]$MarketplaceDir = "",
    [string]$Python = "",
    [switch]$SkipCodexPluginRefresh,
    [switch]$SkipClaudePlugin,
    [switch]$Unregister,
    [Alias("WhatIf")][switch]$DryRun,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "install-common.ps1")
if ($Json) { $script:VwQuiet = $true }

if (-not $Root) { $Root = Get-DefaultRoot }
$Root = Get-FullPath $Root
if (-not $Launcher) { $Launcher = Join-Path $Root "bin\vectorworks-mcp.cmd" }
$Launcher = Get-FullPath $Launcher
if (-not $MarketplaceDir) { $MarketplaceDir = Join-Path $Root "marketplace" }
$customCodexHome = [bool]$CodexHome
if (-not $CodexHome) { $CodexHome = Get-DefaultCodexHome }
$CodexHome = Get-FullPath $CodexHome
$customClaude = [bool]$ClaudeConfig
if (-not $ClaudeConfig) { $ClaudeConfig = Get-DefaultClaudeConfig }
$ClaudeConfig = Get-FullPath $ClaudeConfig

$report = [ordered]@{ ok = $true; launcher = $Launcher; dry_run = [bool]$DryRun; unregister = [bool]$Unregister; clients = [ordered]@{} }

function Resolve-HelperPython {
    if ($Python -and (Test-Path -LiteralPath $Python)) { return $Python }
    $receipt = Join-Path $Root "install-receipt.json"
    if (Test-Path -LiteralPath $receipt) {
        try {
            $r = Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json
            if ($r.python.base_executable -and (Test-Path -LiteralPath $r.python.base_executable)) { return [string]$r.python.base_executable }
        } catch { }
    }
    $found = Find-BasePython
    if ($found) { return $found.executable }
    throw "Python 3.11+ (python.org) is required for client registration. Install it with: winget install --id Python.Python.3.12 -e --scope user"
}

$helper = Join-Path $PSScriptRoot "register_clients.py"
$py = Resolve-HelperPython

function Invoke-Helper([string[]]$HelperArgs) {
    $r = Invoke-Native -FilePath $py -Arguments (@("-I", "-X", "utf8", $helper) + $HelperArgs) -TimeoutSec 120
    try { $obj = $r.StdOut | ConvertFrom-Json } catch { throw "register_clients.py failed (exit $($r.ExitCode)): $($r.StdErr) $($r.StdOut)" }
    return $obj
}

# ------------------------------------------------------------------ Codex
if ($Client -in @("all", "codex")) {
    $cfg = Join-Path $CodexHome "config.toml"
    $entry = [ordered]@{ config = $cfg; ok = $true; changes = @(); notes = @() }
    if (-not (Test-Path -LiteralPath $CodexHome)) {
        $entry.notes += "Codex home $CodexHome does not exist; skipped (install Codex first, then rerun)."
        Write-Step "Codex: $CodexHome not found - skipped" "warn"
    } else {
        $codex = Get-Command codex -ErrorAction SilentlyContinue
        $codexExe = if ($codex) { $codex.Source } else { Join-Path $env:LOCALAPPDATA "Programs\OpenAI\Codex\bin\codex.exe" }
        if ($Unregister -and -not $DryRun -and (Test-Path -LiteralPath $codexExe)) {
            # Let Codex drop its cached copy of the skills plugin while our marketplace still exists.
            $rm = Invoke-Native -FilePath $codexExe -Arguments @("plugin", "remove", "vectorworks@vectorworks-mcp") -Environment @{ CODEX_HOME = $CodexHome } -TimeoutSec 120
            $entry.notes += "codex plugin remove vectorworks@vectorworks-mcp (exit $($rm.ExitCode))"
        }
        $a = @("codex", "--config", $cfg, "--launcher", $Launcher)
        if (-not $Unregister) { $a += @("--marketplace-dir", $MarketplaceDir) } else { $a += @("--unregister", "--marketplace-dir", $MarketplaceDir) }
        if ($DryRun) { $a += "--dry-run" }
        $res = Invoke-Helper $a
        $entry.ok = [bool]$res.ok
        $entry.changes = @($res.changes)
        if ($res.PSObject.Properties["backup"]) { $entry.backup = $res.backup }
        if ($res.PSObject.Properties["error"]) { $entry.error = $res.error; $report.ok = $false }
        foreach ($c in $entry.changes) { Write-Step "Codex: $c" ($(if ($DryRun) { "plan" } else { "ok" })) }
        if (-not $res.ok) { Write-Step "Codex: $($res.error)" "fail" }

        # Refresh the skills-only plugin from the local marketplace with the
        # codex CLI when available (keeps Codex's own plugin cache consistent).
        $codex = Get-Command codex -ErrorAction SilentlyContinue
        $codexExe = if ($codex) { $codex.Source } else { Join-Path $env:LOCALAPPDATA "Programs\OpenAI\Codex\bin\codex.exe" }
        if (-not $Unregister -and -not $DryRun -and -not $SkipCodexPluginRefresh -and $res.ok -and (Test-Path -LiteralPath $codexExe) -and (Test-Path -LiteralPath (Join-Path $MarketplaceDir ".agents\plugins\marketplace.json"))) {
            $envs = @{ CODEX_HOME = $CodexHome }
            $add = Invoke-Native -FilePath $codexExe -Arguments @("plugin", "add", "vectorworks@vectorworks-mcp", "--json") -Environment $envs -TimeoutSec 120
            if ($add.ExitCode -eq 0) {
                $entry.notes += "codex plugin add vectorworks@vectorworks-mcp: ok"
                Write-Step "Codex: refreshed skills-only plugin vectorworks@vectorworks-mcp" "ok"
                # Re-run the edit: re-enables the plugin if the refreshed cache has no server.
                $res2 = Invoke-Helper @("codex", "--config", $cfg, "--launcher", $Launcher, "--marketplace-dir", $MarketplaceDir)
                foreach ($c in @($res2.changes)) { if ($c -ne "already up to date") { $entry.changes += $c; Write-Step "Codex: $c" "ok" } }
            } else {
                $entry.notes += "codex plugin add failed (exit $($add.ExitCode)); skills refresh on next Codex start. $($add.StdErr.Trim())"
                Write-Step "Codex: plugin refresh failed (skills update on next Codex start)" "warn"
            }
        }
        $insp = Invoke-Helper @("inspect", "--codex-home", $CodexHome, "--claude-config", $ClaudeConfig, "--launcher", $Launcher)
        $defs = @($insp.codex.definitions)
        $entry.active_definitions = $defs
        if (-not $Unregister -and -not $DryRun -and $defs.Count -ne 1) {
            $entry.ok = $false; $report.ok = $false
            Write-Step "Codex: expected exactly one active vectorworks server, found $($defs.Count)" "fail"
        }
    }
    $report.clients["codex"] = $entry
}

# ------------------------------------------------------------ Claude Code
if ($Client -in @("all", "claude-code")) {
    $entry = [ordered]@{ config = $ClaudeConfig; ok = $true; method = $null; changes = @(); notes = @() }
    # Claude desktop syncs the 'vectorworks' plugin from github.com/BhaveshY/vectorworks-claude-plugin
    # (org marketplace). From 1.0 it starts this same launcher and ships the skill, so a user-scope
    # server or the local skills plugin would only duplicate it: remove them in that case.
    $syncedPlugin = $null
    if (-not $customClaude -and $env:APPDATA) {
        $sessions = Join-Path $env:APPDATA "Claude\local-agent-mode-sessions"
        if (Test-Path -LiteralPath $sessions) {
            foreach ($m in @(Get-ChildItem -LiteralPath $sessions -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                        Get-ChildItem -LiteralPath $_.FullName -Directory -ErrorAction SilentlyContinue } | ForEach-Object {
                        Get-ChildItem -Path (Join-Path $_.FullName "rpm\*\.claude-plugin\plugin.json") -ErrorAction SilentlyContinue })) {
                try {
                    $j = Get-Content -Raw -LiteralPath $m.FullName | ConvertFrom-Json
                    if ($j.name -eq "vectorworks") { $syncedPlugin = [pscustomobject]@{ version = [string]$j.version; path = $m.Directory.Parent.FullName }; break }
                } catch { }
            }
        }
    }
    $syncedMajor = 0
    if ($syncedPlugin) { [int]::TryParse(($syncedPlugin.version -split "\.")[0], [ref]$syncedMajor) | Out-Null }
    $viaPlugin = (-not $Unregister) -and $syncedPlugin -and $syncedMajor -ge 1
    $claudeUnregister = $Unregister -or $viaPlugin
    if ($viaPlugin) {
        $entry.via_synced_plugin = $syncedPlugin.version
        $entry.notes += "Claude uses the synced plugin 'vectorworks' $($syncedPlugin.version) (server + skill); removing duplicate user-scope server and local skills plugin."
        Write-Step "Claude: synced plugin vectorworks $($syncedPlugin.version) provides the server and skill" "ok"
    } elseif ($syncedPlugin) {
        $entry.notes += "Claude has the synced plugin 'vectorworks' $($syncedPlugin.version) (< 1.0); it updates from github.com/BhaveshY/vectorworks-claude-plugin after Claude restarts."
    }
    $claude = Get-Command claude -ErrorAction SilentlyContinue
    $method = $ClaudeMethod
    if ($method -eq "auto") {
        # The CLI only knows its own config location; custom paths use the JSON merge.
        $cliUsable = $claude -and ((-not $customClaude) -or ((Split-Path -Leaf $ClaudeConfig) -eq ".claude.json"))
        $method = if ($cliUsable) { "cli" } else { "json" }
    }
    if ($method -eq "cli" -and -not $claude) { $method = "json"; $entry.notes += "claude CLI not on PATH; used JSON merge" }
    $entry.method = $method
    if (-not (Test-Path -LiteralPath $ClaudeConfig) -and -not $claude) {
        $entry.notes += "Claude Code is not installed ($ClaudeConfig missing); skipped."
        Write-Step "Claude Code: not installed - skipped" "warn"
    } elseif ($method -eq "cli") {
        $envs = @{}
        if ($customClaude) { $envs["CLAUDE_CONFIG_DIR"] = (Split-Path -Parent $ClaudeConfig) }
        if ($DryRun) {
            $cmdExePath = Join-Path $env:SystemRoot "System32\cmd.exe"
            if ($claudeUnregister) { $planText = "would run: claude mcp remove -s user vectorworks" }
            else { $planText = 'would run: claude mcp add -s user vectorworks -- {0} /d /c "{1}"' -f $cmdExePath, $Launcher }
            $entry.changes += $planText
            Write-Step "Claude Code: $($entry.changes[-1])" "plan"
        } else {
            $insp = Invoke-Helper @("inspect", "--codex-home", $CodexHome, "--claude-config", $ClaudeConfig, "--launcher", $Launcher)
            $current = $insp.claude.user
            $isOurs = $current -and $current.points_at_launcher
            if ($claudeUnregister) {
                if ($isOurs) {
                    $r = Invoke-Native -FilePath $claude.Source -Arguments @("mcp", "remove", "-s", "user", "vectorworks") -Environment $envs -TimeoutSec 60
                    $entry.changes += "claude mcp remove -s user vectorworks (exit $($r.ExitCode))"
                } else { $entry.changes += "nothing to remove" }
            } else {
                $cmdExe = Join-Path $env:SystemRoot "System32\cmd.exe"
                $ok = $current -and $current.command -eq $cmdExe -and (@($current.args) -join "|") -eq (@("/d", "/c", $Launcher) -join "|")
                if ($ok) {
                    $entry.changes += "already up to date"
                } else {
                    if ($current) {
                        $null = Invoke-Native -FilePath $claude.Source -Arguments @("mcp", "remove", "-s", "user", "vectorworks") -Environment $envs -TimeoutSec 60
                        $entry.changes += "removed previous user-scope vectorworks ($($current.command) $(@($current.args) -join ' '))"
                    }
                    $r = Invoke-Native -FilePath $claude.Source -Arguments @("mcp", "add", "-s", "user", "vectorworks", "--", $cmdExe, "/d", "/c", $Launcher) -Environment $envs -TimeoutSec 60
                    $verify = Invoke-Helper @("inspect", "--codex-home", $CodexHome, "--claude-config", $ClaudeConfig, "--launcher", $Launcher)
                    $u = $verify.claude.user
                    if ($r.ExitCode -eq 0 -and $u -and $u.command -eq $cmdExe -and (@($u.args) -join "|") -eq (@("/d", "/c", $Launcher) -join "|")) {
                        $entry.changes += "claude mcp add -s user vectorworks -- cmd.exe /d /c $Launcher"
                    } else {
                        $entry.notes += "claude mcp add did not produce the expected entry (exit $($r.ExitCode)); used JSON merge instead"
                        $res = Invoke-Helper @("claude", "--config", $ClaudeConfig, "--launcher", $Launcher)
                        $entry.method = "json"
                        $entry.changes += @($res.changes)
                        if (-not $res.ok) { $entry.ok = $false; $report.ok = $false; $entry.error = $res.error }
                    }
                }
            }
        }
        foreach ($c in $entry.changes) { Write-Step "Claude Code: $c" ($(if ($DryRun) { "plan" } else { "ok" })) }
    } else {
        $a = @("claude", "--config", $ClaudeConfig, "--launcher", $Launcher)
        if ($claudeUnregister) { $a += "--unregister" }
        if ($DryRun) { $a += "--dry-run" }
        $res = Invoke-Helper $a
        $entry.ok = [bool]$res.ok
        $entry.changes = @($res.changes)
        if ($res.PSObject.Properties["backup"]) { $entry.backup = $res.backup }
        if (-not $res.ok) { $entry.error = $res.error; $report.ok = $false; Write-Step "Claude Code: $($res.error)" "fail" }
        foreach ($c in $entry.changes) { Write-Step "Claude Code: $c" ($(if ($DryRun) { "plan" } else { "ok" })) }
    }
    # Skills: the skills-only plugin from the local marketplace (same folder Codex uses).
    if ($claude -and -not $SkipClaudePlugin -and ((-not $customClaude) -or ((Split-Path -Leaf $ClaudeConfig) -eq ".claude.json"))) {
        $envs = @{}
        if ($customClaude) { $envs["CLAUDE_CONFIG_DIR"] = (Split-Path -Parent $ClaudeConfig) }
        $mkJson = Join-Path $MarketplaceDir ".claude-plugin\marketplace.json"
        if ($DryRun) {
            $entry.changes += $(if ($claudeUnregister) { "would run: claude plugin uninstall vectorworks@vectorworks-mcp; claude plugin marketplace remove vectorworks-mcp" } else { "would run: claude plugin marketplace add `"$MarketplaceDir`"; claude plugin install vectorworks@vectorworks-mcp" })
            Write-Step "Claude Code: $($entry.changes[-1])" "plan"
        } elseif ($claudeUnregister) {
            $u = Invoke-Native -FilePath $claude.Source -Arguments @("plugin", "uninstall", "vectorworks@vectorworks-mcp") -Environment $envs -TimeoutSec 120
            $m = Invoke-Native -FilePath $claude.Source -Arguments @("plugin", "marketplace", "remove", "vectorworks-mcp") -Environment $envs -TimeoutSec 120
            $entry.changes += "skills plugin uninstalled (exit $($u.ExitCode)/$($m.ExitCode))"
        } elseif (Test-Path -LiteralPath $mkJson) {
            $m = Invoke-Native -FilePath $claude.Source -Arguments @("plugin", "marketplace", "add", $MarketplaceDir) -Environment $envs -TimeoutSec 120
            $i = Invoke-Native -FilePath $claude.Source -Arguments @("plugin", "install", "vectorworks@vectorworks-mcp") -Environment $envs -TimeoutSec 120
            if ($m.ExitCode -eq 0 -and $i.ExitCode -eq 0) {
                $entry.changes += "skills plugin vectorworks@vectorworks-mcp installed (loads from $MarketplaceDir)"
                Write-Step "Claude Code: skills plugin vectorworks@vectorworks-mcp" "ok"
            } else {
                $entry.notes += "skills plugin install failed (marketplace exit $($m.ExitCode), install exit $($i.ExitCode)); in Claude Code run: /plugin marketplace add $MarketplaceDir"
                Write-Step "Claude Code: skills plugin not installed - in Claude Code run /plugin marketplace add $MarketplaceDir" "warn"
            }
        }
    }
    if (-not $DryRun -and (Test-Path -LiteralPath $ClaudeConfig)) {
        $insp = Invoke-Helper @("inspect", "--codex-home", $CodexHome, "--claude-config", $ClaudeConfig, "--launcher", $Launcher)
        $entry.project_overrides = @($insp.claude.project_overrides)
        foreach ($o in $entry.project_overrides) {
            Write-Step "Claude Code: project $($o.project) has its own 'vectorworks' server that overrides the user one. Remove it with: claude mcp remove -s local vectorworks (run inside that project)" "warn"
        }
    }
    $report.clients["claude-code"] = $entry
}

if ($Json) { $report | ConvertTo-Json -Depth 8 } else { if (-not $report.ok) { Write-Step "Client registration finished with problems" "fail" } }
if (-not $report.ok) { exit 1 }
