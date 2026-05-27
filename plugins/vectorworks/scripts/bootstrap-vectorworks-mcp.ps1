[CmdletBinding()]
param(
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @()
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = (& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Resolver @ResolverArgs | Select-Object -Last 1).Trim()

Write-Host "Vectorworks MCP repo: $RepoRoot"

$Runner = Join-Path $RepoRoot "scripts\run-mcp-server.ps1"
$Register = Join-Path $RepoRoot "scripts\register-claude-code.ps1"
$Verify = Join-Path $RepoRoot "scripts\verify-no-vectorworks.ps1"
$Launcher = Join-Path $RepoRoot "vw_start_listener_2024.py"

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Runner -SetupOnly
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Register -SkipInstall -NoClaudeConfig -LauncherPath $Launcher
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not $SkipVerify) {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Verify -LauncherPath $Launcher
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "OK: generated Vectorworks launcher at $Launcher"
Write-Host "Next: paste/run that launcher inside Vectorworks, then run scripts\test-vectorworks-listener.ps1."
