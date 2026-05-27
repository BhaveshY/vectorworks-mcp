[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$WorktreeRoot = "",
    [string]$DestinationDir = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-InstallIfMissing", "-RequireContract")
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs
$Copier = Join-Path $RepoRoot "scripts\copy-native-bridge-scaffold.ps1"
if (-not (Test-Path -LiteralPath $Copier)) {
    throw "Companion repo native scaffold copy script was not found at $Copier"
}

$Args = @("-VectorworksVersion", $VectorworksVersion)
if ($WorktreeRoot) { $Args += @("-WorktreeRoot", $WorktreeRoot) }
if ($DestinationDir) { $Args += @("-DestinationDir", $DestinationDir) }
if ($Force) { $Args += "-Force" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Copier @Args
exit $LASTEXITCODE
