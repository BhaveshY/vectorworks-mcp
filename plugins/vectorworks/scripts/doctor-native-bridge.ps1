[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [string]$VectorworksVersion = "2024",
    [string]$BuiltArtifact = "",
    [string]$WorktreeRoot = "",
    [string]$InstallDir = "",
    [switch]$Install,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")
$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-RequireContract")
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs
$Doctor = Join-Path $RepoRoot "scripts\doctor-native-bridge.ps1"
if (-not (Test-Path -LiteralPath $Doctor)) {
    throw "Companion repo native doctor script was not found at $Doctor"
}

$Args = @("-VectorworksVersion", $VectorworksVersion)
if ($BuiltArtifact) { $Args += @("-BuiltArtifact", $BuiltArtifact) }
if ($WorktreeRoot) { $Args += @("-WorktreeRoot", $WorktreeRoot) }
if ($InstallDir) { $Args += @("-InstallDir", $InstallDir) }
if ($Install) { $Args += "-Install" }
if ($Json) { $Args += "-Json" }
if ($WhatIfPreference) { $Args += "-WhatIf" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Doctor @Args
exit $LASTEXITCODE
