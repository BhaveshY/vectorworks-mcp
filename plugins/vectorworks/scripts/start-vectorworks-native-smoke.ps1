[CmdletBinding()]
param(
    [string]$RepoPath = "",
    [string]$VectorworksVersion = "2024",
    [string]$VectorworksExe = "",
    [string]$HostName = "",
    [ValidateRange(1, 65535)]
    [int]$Port = 0,
    [ValidateRange(5, 600)]
    [int]$StartupTimeoutSeconds = 90,
    [ValidateRange(100, 10000)]
    [int]$ProbeIntervalMilliseconds = 1000,
    [switch]$RestartIfRunning,
    [switch]$ForceKillIfCloseFails,
    [switch]$NoStart,
    [switch]$RunPhase2,
    [switch]$AllowWriteFixture,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-RequireContract")
if ($RepoPath) { $ResolverArgs += @("-RepoPath", $RepoPath) }
elseif ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs
$StartSmoke = Join-Path $RepoRoot "scripts\start-vectorworks-native-smoke.ps1"

if (-not (Test-Path -LiteralPath $StartSmoke)) {
    throw "Companion repo native Vectorworks launch/smoke script was not found at $StartSmoke"
}

$Args = @("-VectorworksVersion", $VectorworksVersion)
if ($VectorworksExe) { $Args += @("-VectorworksExe", $VectorworksExe) }
if ($HostName) { $Args += @("-HostName", $HostName) }
if ($Port -ne 0) { $Args += @("-Port", $Port) }
$Args += @("-StartupTimeoutSeconds", $StartupTimeoutSeconds, "-ProbeIntervalMilliseconds", $ProbeIntervalMilliseconds)
if ($RestartIfRunning) { $Args += "-RestartIfRunning" }
if ($ForceKillIfCloseFails) { $Args += "-ForceKillIfCloseFails" }
if ($NoStart) { $Args += "-NoStart" }
if ($RunPhase2) { $Args += "-RunPhase2" }
if ($AllowWriteFixture) { $Args += "-AllowWriteFixture" }
if ($Json) { $Args += "-Json" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $StartSmoke @Args
exit $LASTEXITCODE
