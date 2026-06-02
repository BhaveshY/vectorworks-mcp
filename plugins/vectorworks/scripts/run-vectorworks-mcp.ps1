[CmdletBinding()]
param(
    [string]$RepoPath = ""
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-InstallIfMissing", "-RequireContract")
if ($RepoPath) { $ResolverArgs += @("-RepoPath", $RepoPath) }
elseif ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs
$Runner = Join-Path $RepoRoot "scripts\run-mcp-server.ps1"

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Runner
exit $LASTEXITCODE
