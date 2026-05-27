[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$WorktreeRoot = "",
    [string]$ProjectPath = "",
    [string]$SourceDir = "",
    [string]$FiltersPath = "",
    [switch]$CheckOnly,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-InstallIfMissing", "-RequireContract")
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs
$Wire = Join-Path $RepoRoot "scripts\wire-native-bridge-project.ps1"
if (-not (Test-Path -LiteralPath $Wire)) {
    throw "Companion repo native project wiring script was not found at $Wire"
}

$Args = @("-VectorworksVersion", $VectorworksVersion)
if ($WorktreeRoot) { $Args += @("-WorktreeRoot", $WorktreeRoot) }
if ($ProjectPath) { $Args += @("-ProjectPath", $ProjectPath) }
if ($SourceDir) { $Args += @("-SourceDir", $SourceDir) }
if ($FiltersPath) { $Args += @("-FiltersPath", $FiltersPath) }
if ($CheckOnly) { $Args += "-CheckOnly" }
if ($Json) { $Args += "-Json" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Wire @Args
exit $LASTEXITCODE
