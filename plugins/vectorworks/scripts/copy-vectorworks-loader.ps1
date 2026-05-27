[CmdletBinding()]
param(
    [string]$LauncherPath = "",
    [string]$LoaderPath = "",
    [switch]$Regenerate,
    [switch]$Print,
    [switch]$BestEffort
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-InstallIfMissing", "-RequireContract")
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs
$Copier = Join-Path $RepoRoot "scripts\copy-vectorworks-loader.ps1"

if (-not (Test-Path -LiteralPath $Copier)) {
    throw "Companion repo loader copy script was not found at $Copier"
}

$Args = @()
if ($LauncherPath) { $Args += @("-LauncherPath", $LauncherPath) }
if ($LoaderPath) { $Args += @("-LoaderPath", $LoaderPath) }
if ($Regenerate) { $Args += "-Regenerate" }
if ($Print) { $Args += "-Print" }
if ($BestEffort) { $Args += "-BestEffort" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Copier @Args
exit $LASTEXITCODE
