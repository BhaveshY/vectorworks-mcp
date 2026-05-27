[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$SdkDir = "",
    [string]$SdkExamplesDir = "",
    [switch]$DownloadSdk,
    [switch]$InstallVisualStudioBuildTools,
    [switch]$CloneSdkExamples,
    [switch]$PrepareSource,
    [switch]$Build,
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Debug",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-RequireContract")
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = (& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Resolver @ResolverArgs | Select-Object -Last 1).Trim()
$Bootstrap = Join-Path $RepoRoot "scripts\bootstrap-native-bridge.ps1"

if (-not (Test-Path -LiteralPath $Bootstrap)) {
    throw "Companion repo native bootstrap script was not found at $Bootstrap"
}

$Args = @("-VectorworksVersion", $VectorworksVersion, "-Configuration", $Configuration)
if ($SdkDir) { $Args += @("-SdkDir", $SdkDir) }
if ($SdkExamplesDir) { $Args += @("-SdkExamplesDir", $SdkExamplesDir) }
if ($DownloadSdk) { $Args += "-DownloadSdk" }
if ($InstallVisualStudioBuildTools) { $Args += "-InstallVisualStudioBuildTools" }
if ($CloneSdkExamples) { $Args += "-CloneSdkExamples" }
if ($PrepareSource) { $Args += "-PrepareSource" }
if ($Build) { $Args += "-Build" }
if ($Force) { $Args += "-Force" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Bootstrap @Args
exit $LASTEXITCODE
