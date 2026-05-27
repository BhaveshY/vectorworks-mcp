[CmdletBinding()]
param(
    [string]$HostName = "",
    [ValidateRange(1, 65535)]
    [int]$Port = 0,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @()
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = (& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Resolver @ResolverArgs | Select-Object -Last 1).Trim()
$Doctor = Join-Path $RepoRoot "scripts\doctor-vectorworks-mcp.ps1"

if (-not (Test-Path -LiteralPath $Doctor)) {
    throw "Companion repo doctor script was not found at $Doctor"
}

$Args = @()
if ($HostName) { $Args += @("-HostName", $HostName) }
if ($Port -ne 0) { $Args += @("-Port", $Port) }
if ($Json) { $Args += "-Json" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Doctor @Args
exit $LASTEXITCODE
