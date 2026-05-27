[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [string]$VectorworksVersion = "2024",
    [string]$BuiltArtifact = "",
    [string]$InstallDir = "",
    [switch]$Install,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-RequireContract")
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = (& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Resolver @ResolverArgs | Select-Object -Last 1).Trim()
$Doctor = Join-Path $RepoRoot "scripts\doctor-native-bridge.ps1"
if (-not (Test-Path -LiteralPath $Doctor)) {
    throw "Companion repo native doctor script was not found at $Doctor"
}

$Args = @("-VectorworksVersion", $VectorworksVersion)
if ($BuiltArtifact) { $Args += @("-BuiltArtifact", $BuiltArtifact) }
if ($InstallDir) { $Args += @("-InstallDir", $InstallDir) }
if ($Install) { $Args += "-Install" }
if ($Json) { $Args += "-Json" }
if ($WhatIfPreference) { $Args += "-WhatIf" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Doctor @Args
exit $LASTEXITCODE
