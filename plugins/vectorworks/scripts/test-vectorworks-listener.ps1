[CmdletBinding()]
param(
    [string]$HostName = "",
    [ValidateRange(1, 65535)]
    [int]$Port = 0,
    [ValidateRange(1, 120)]
    [int]$TimeoutSeconds = 5
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-RequireContract")
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs
$Tester = Join-Path $RepoRoot "scripts\test-vectorworks-listener.ps1"

$Args = @()
if ($HostName) { $Args += @("-HostName", $HostName) }
if ($Port -ne 0) { $Args += @("-Port", $Port) }
$Args += @("-TimeoutSeconds", $TimeoutSeconds)

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Tester @Args
exit $LASTEXITCODE
