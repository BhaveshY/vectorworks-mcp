[CmdletBinding()]
param(
    [switch]$SkipVerify,
    [switch]$SkipContract,
    [switch]$SkipClipboard
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-InstallIfMissing", "-RequireContract")
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs

Write-Host "Vectorworks MCP repo: $RepoRoot"

$ContractCheck = Join-Path $PSScriptRoot "check-companion-contract.ps1"
$Runner = Join-Path $RepoRoot "scripts\run-mcp-server.ps1"
$Register = Join-Path $RepoRoot "scripts\register-claude-code.ps1"
$Verify = Join-Path $RepoRoot "scripts\verify-no-vectorworks.ps1"
$CopyLoader = Join-Path $PSScriptRoot "copy-vectorworks-loader.ps1"
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

if (-not $SkipClipboard) {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $CopyLoader -LauncherPath $Launcher -LoaderPath $Loader -BestEffort
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "OK: generated Vectorworks launcher at $Launcher"
Write-Host "OK: generated Vectorworks loader at $Loader"
if ($SkipClipboard) {
    Write-Host "Next: copy the loader with scripts\copy-vectorworks-loader.ps1, paste it inside Vectorworks, then run scripts\test-vectorworks-listener.ps1."
} else {
    Write-Host "Next: paste/run the clipboard loader inside Vectorworks, then run scripts\test-vectorworks-listener.ps1."
}
