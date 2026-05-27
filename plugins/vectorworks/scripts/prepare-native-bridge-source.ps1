[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$SdkDir = "",
    [string]$SdkExamplesDir = "",
    [switch]$CloneSdkExamples,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-RequireContract")
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = (& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Resolver @ResolverArgs | Select-Object -Last 1).Trim()
$Prepare = Join-Path $RepoRoot "scripts\prepare-native-bridge-source.ps1"

if (-not (Test-Path -LiteralPath $Prepare)) {
    throw "Companion repo native source preparation script was not found at $Prepare"
}

$Args = @("-VectorworksVersion", $VectorworksVersion)
if ($SdkDir) { $Args += @("-SdkDir", $SdkDir) }
if ($SdkExamplesDir) { $Args += @("-SdkExamplesDir", $SdkExamplesDir) }
if ($CloneSdkExamples) { $Args += "-CloneSdkExamples" }
if ($Force) { $Args += "-Force" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Prepare @Args
exit $LASTEXITCODE
