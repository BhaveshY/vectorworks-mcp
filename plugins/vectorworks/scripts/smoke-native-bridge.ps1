[CmdletBinding()]
param(
    [string]$HostName = "",
    [ValidateRange(1, 65535)]
    [int]$Port = 0,
    [ValidateRange(1, 120)]
    [int]$TimeoutSeconds = 5,
    [ValidateRange(1, 100)]
    [int]$PingCount = 10,
    [ValidateRange(1, 100)]
    [int]$ReadCount = 10,
    [ValidateRange(0, 1)]
    [int]$Phase = 1,
    [switch]$AllowNonNative,
    [switch]$IncludeObjects,
    [switch]$AllowWriteFixture,
    [switch]$Stop,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-RequireContract")
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = (& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Resolver @ResolverArgs | Select-Object -Last 1).Trim()
$Smoke = Join-Path $RepoRoot "scripts\smoke-native-bridge.ps1"

if (-not (Test-Path -LiteralPath $Smoke)) {
    throw "Companion repo native smoke script was not found at $Smoke"
}

$Args = @()
if ($HostName) { $Args += @("-HostName", $HostName) }
if ($Port -ne 0) { $Args += @("-Port", $Port) }
$Args += @("-TimeoutSeconds", $TimeoutSeconds, "-PingCount", $PingCount, "-ReadCount", $ReadCount)
$Args += @("-Phase", $Phase)
if ($AllowNonNative) { $Args += "-AllowNonNative" }
if ($IncludeObjects) { $Args += "-IncludeObjects" }
if ($AllowWriteFixture) { $Args += "-AllowWriteFixture" }
if ($Stop) { $Args += "-Stop" }
if ($Json) { $Args += "-Json" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Smoke @Args
exit $LASTEXITCODE
