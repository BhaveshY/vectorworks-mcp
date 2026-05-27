[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$SdkDir = "",
    [string]$SourceDir = "",
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Debug",
    [switch]$SkipPrereqCheck
)

$ErrorActionPreference = "Stop"

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-RequireContract")
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = (& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Resolver @ResolverArgs | Select-Object -Last 1).Trim()
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
