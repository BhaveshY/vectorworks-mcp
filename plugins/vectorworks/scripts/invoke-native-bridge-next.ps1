[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$BuiltArtifact = "",
    [string]$SdkDir = "",
    [string]$SdkExamplesDir = "",
    [string]$WorktreeRoot = "",
    [string]$InstallDir = "",
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Debug",
    [switch]$Install,
    [ValidateRange(1, 20)]
    [int]$MaxSteps = 1,
    [switch]$AllowNetwork,
    [switch]$AllowInstallSoftware,
    [switch]$AllowDownloadLargeFiles,
    [switch]$AllowModifyVectorworksUserPlugins,
    [switch]$AllowVectorworksRestartStep,
    [switch]$AllowRebootRisk,
    [switch]$PlanOnly,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-InstallIfMissing", "-RequireContract")
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs
$Runner = Join-Path $RepoRoot "scripts\invoke-native-bridge-next.ps1"
if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Companion repo native next-step runner was not found at $Runner"
}

$Args = @("-VectorworksVersion", $VectorworksVersion, "-Configuration", $Configuration, "-MaxSteps", [string]$MaxSteps)
if ($BuiltArtifact) { $Args += @("-BuiltArtifact", $BuiltArtifact) }
if ($SdkDir) { $Args += @("-SdkDir", $SdkDir) }
if ($SdkExamplesDir) { $Args += @("-SdkExamplesDir", $SdkExamplesDir) }
if ($WorktreeRoot) { $Args += @("-WorktreeRoot", $WorktreeRoot) }
if ($InstallDir) { $Args += @("-InstallDir", $InstallDir) }
if ($Install) { $Args += "-Install" }
if ($AllowNetwork) { $Args += "-AllowNetwork" }
if ($AllowInstallSoftware) { $Args += "-AllowInstallSoftware" }
if ($AllowDownloadLargeFiles) { $Args += "-AllowDownloadLargeFiles" }
if ($AllowModifyVectorworksUserPlugins) { $Args += "-AllowModifyVectorworksUserPlugins" }
if ($AllowVectorworksRestartStep) { $Args += "-AllowVectorworksRestartStep" }
if ($AllowRebootRisk) { $Args += "-AllowRebootRisk" }
if ($PlanOnly) { $Args += "-PlanOnly" }
if ($Json) { $Args += "-Json" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Runner @Args
exit $LASTEXITCODE
