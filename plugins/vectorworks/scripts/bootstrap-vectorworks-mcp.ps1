[CmdletBinding()]
param(
    [switch]$SkipVerify,
    [switch]$SkipContract
)

$ErrorActionPreference = "Stop"

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-InstallIfMissing", "-RequireContract")
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = (& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Resolver @ResolverArgs | Select-Object -Last 1).Trim()

Write-Host "Vectorworks MCP repo: $RepoRoot"

$ContractCheck = Join-Path $PSScriptRoot "check-companion-contract.ps1"
$Runner = Join-Path $RepoRoot "scripts\run-mcp-server.ps1"
$Register = Join-Path $RepoRoot "scripts\register-claude-code.ps1"
$Verify = Join-Path $RepoRoot "scripts\verify-no-vectorworks.ps1"
$Launcher = Join-Path $RepoRoot "vw_start_listener_2024.py"
$Loader = Join-Path $RepoRoot "vw_load_listener_2024.py"

if (-not $SkipContract) {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $ContractCheck -RepoPath $RepoRoot
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Runner -SetupOnly
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Register -SkipInstall -NoClaudeConfig -LauncherPath $Launcher -LoaderPath $Loader
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not $SkipVerify) {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Verify -LauncherPath $Launcher -LoaderPath $Loader
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "OK: generated Vectorworks launcher at $Launcher"
Write-Host "OK: generated Vectorworks loader at $Loader"
Write-Host "Next: paste/run the loader inside Vectorworks, then run scripts\test-vectorworks-listener.ps1."
