[CmdletBinding()]
param(
    [string]$RepoPath = "",
    [string]$VectorworksVersion = "2024",
    [string]$SdkDir = "",
    [string]$SourceDir = "",
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release",
    [switch]$SkipPrereqCheck
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-RequireContract")
if ($RepoPath) { $ResolverArgs += @("-RepoPath", $RepoPath) }
elseif ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs
$Build = Join-Path $RepoRoot "scripts\build-native-bridge.ps1"

if (-not (Test-Path -LiteralPath $Build)) {
    throw "Companion repo native build script was not found at $Build"
}

$Args = @("-VectorworksVersion", $VectorworksVersion, "-Configuration", $Configuration)
if ($SdkDir) { $Args += @("-SdkDir", $SdkDir) }
if ($SourceDir) { $Args += @("-SourceDir", $SourceDir) }
if ($SkipPrereqCheck) { $Args += "-SkipPrereqCheck" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Build @Args
exit $LASTEXITCODE
