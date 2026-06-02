[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [string]$RepoPath = "",
    [string]$VectorworksVersion = "2024",
    [string]$BuiltArtifact = "",
    [string]$SdkDir = "",
    [string]$SdkArchivePath = "",
    [string]$SdkExamplesDir = "",
    [string]$WorktreeRoot = "",
    [string]$InstallDir = "",
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Debug",
    [switch]$Install,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")
$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-RequireContract")
if ($RepoPath) { $ResolverArgs += @("-RepoPath", $RepoPath) }
elseif ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs
$Doctor = Join-Path $RepoRoot "scripts\doctor-native-bridge.ps1"
if (-not (Test-Path -LiteralPath $Doctor)) {
    throw "Companion repo native doctor script was not found at $Doctor"
}

$Args = @("-VectorworksVersion", $VectorworksVersion)
if ($BuiltArtifact) { $Args += @("-BuiltArtifact", $BuiltArtifact) }
if ($SdkDir) { $Args += @("-SdkDir", $SdkDir) }
if ($SdkArchivePath) { $Args += @("-SdkArchivePath", $SdkArchivePath) }
if ($SdkExamplesDir) { $Args += @("-SdkExamplesDir", $SdkExamplesDir) }
if ($WorktreeRoot) { $Args += @("-WorktreeRoot", $WorktreeRoot) }
if ($InstallDir) { $Args += @("-InstallDir", $InstallDir) }
if ($PSBoundParameters.ContainsKey("Configuration")) { $Args += @("-Configuration", $Configuration) }
if ($Install) { $Args += "-Install" }
if ($Json) { $Args += "-Json" }
if ($WhatIfPreference) { $Args += "-WhatIf" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Doctor @Args
exit $LASTEXITCODE
