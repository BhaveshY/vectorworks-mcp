<#
.SYNOPSIS
  Developer runner: start the MCP server from THIS checkout over stdio.

.DESCRIPTION
  Clients never use this script; they use the installed launcher
  %USERPROFILE%\.vectorworks-mcp\bin\vectorworks-mcp.cmd. This runner does no
  pip/venv work: it uses the installed runtime's venv interpreter when present
  (for the optional third-party packages of the legacy profile), otherwise
  python.org Python via `py -3.12`. The default tool profile is stdlib-only.

  Example (in an MCP client config for development only):
    powershell -NoProfile -ExecutionPolicy Bypass -File <repo>\scripts\run-mcp-server.ps1
#>
[CmdletBinding()]
param([string]$Root = "")

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Root) { $Root = Join-Path $env:USERPROFILE ".vectorworks-mcp" }
$py = $null
$cur = Join-Path $Root "current.txt"
if (Test-Path -LiteralPath $cur) {
    $cand = Join-Path $Root ("runtime\" + (Get-Content -LiteralPath $cur -TotalCount 1).Trim() + "\venv\Scripts\python.exe")
    if (Test-Path -LiteralPath $cand) { $py = $cand }
}
$pre = @()
if (-not $py) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $launcher) { [Console]::Error.WriteLine("Python 3.12 not found: winget install --id Python.Python.3.12 -e --scope user"); exit 2 }
    $py = $launcher.Source; $pre = @("-3.12")
}
$env:PYTHONUTF8 = "1"
Set-Location -LiteralPath $repo   # -m puts the checkout first on sys.path
& $py @pre -X utf8 -u -m vectorworks_mcp @args
exit $LASTEXITCODE
