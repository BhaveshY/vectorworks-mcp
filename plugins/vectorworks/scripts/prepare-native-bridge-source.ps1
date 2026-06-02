[CmdletBinding()]
param(
    [string]$RepoPath = "",
    [string]$VectorworksVersion = "2024",
    [string]$SdkDir = "",
    [string]$SdkExamplesDir = "",
    [string]$WorktreeRoot = "",
    [switch]$CloneSdkExamples,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-RequireContract")
if ($RepoPath) { $ResolverArgs += @("-RepoPath", $RepoPath) }
elseif ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs
$Prepare = Join-Path $RepoRoot "scripts\prepare-native-bridge-source.ps1"

if (-not (Test-Path -LiteralPath $Prepare)) {
    throw "Companion repo native source preparation script was not found at $Prepare"
}

$Args = @("-VectorworksVersion", $VectorworksVersion)
if ($SdkDir) { $Args += @("-SdkDir", $SdkDir) }
if ($SdkExamplesDir) { $Args += @("-SdkExamplesDir", $SdkExamplesDir) }
if ($WorktreeRoot) { $Args += @("-WorktreeRoot", $WorktreeRoot) }
if ($CloneSdkExamples) { $Args += "-CloneSdkExamples" }
if ($Force) { $Args += "-Force" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Prepare @Args
exit $LASTEXITCODE
