[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @("-InstallIfMissing", "-RequireContract")
if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = (& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Resolver @ResolverArgs | Select-Object -Last 1).Trim()
$Runner = Join-Path $RepoRoot "scripts\run-mcp-server.ps1"

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Runner
exit $LASTEXITCODE
