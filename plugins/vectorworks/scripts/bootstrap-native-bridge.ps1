[CmdletBinding()]
param(
    [string]$RepoPath = "",
    [string]$VectorworksVersion = "2024",
    [string]$SdkDir = "",
    [string]$SdkArchivePath = "",
    [string]$SdkExamplesDir = "",
    [string]$WorktreeRoot = "",
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

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-RequireContract")
if ($RepoPath) { $ResolverArgs += @("-RepoPath", $RepoPath) }
elseif ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs
$Bootstrap = Join-Path $RepoRoot "scripts\bootstrap-native-bridge.ps1"

if (-not (Test-Path -LiteralPath $Bootstrap)) {
    throw "Companion repo native bootstrap script was not found at $Bootstrap"
}

$Args = @("-VectorworksVersion", $VectorworksVersion, "-Configuration", $Configuration)
if ($SdkDir) { $Args += @("-SdkDir", $SdkDir) }
if ($SdkArchivePath) { $Args += @("-SdkArchivePath", $SdkArchivePath) }
if ($SdkExamplesDir) { $Args += @("-SdkExamplesDir", $SdkExamplesDir) }
if ($WorktreeRoot) { $Args += @("-WorktreeRoot", $WorktreeRoot) }
if ($DownloadSdk) { $Args += "-DownloadSdk" }
if ($InstallVisualStudioBuildTools) { $Args += "-InstallVisualStudioBuildTools" }
if ($CloneSdkExamples) { $Args += "-CloneSdkExamples" }
if ($PrepareSource) { $Args += "-PrepareSource" }
if ($Build) { $Args += "-Build" }
if ($Force) { $Args += "-Force" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Bootstrap @Args
exit $LASTEXITCODE
