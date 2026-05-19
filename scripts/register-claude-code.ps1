[CmdletBinding()]
param(
    [string]$Name = "vectorworks",
    [string]$ListenHost = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 9877,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ServerPath = Join-Path $RepoRoot "server.py"
$RequirementsPath = Join-Path $RepoRoot "requirements.txt"

if (-not (Test-Path $ServerPath)) {
    throw "server.py was not found at $ServerPath"
}

$PythonCommand = $null
$PythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonCommand = "py"
    $PythonArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonCommand = "python"
} else {
    throw "Python was not found. Install Python 3, then rerun this script."
}

if (-not $SkipInstall) {
    & $PythonCommand @PythonArgs -m pip install -r $RequirementsPath
}

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    throw "Claude Code CLI was not found on PATH. Install Claude Code first, then rerun this script."
}

$Config = @{
    type = "stdio"
    command = $PythonCommand
    args = @($PythonArgs + @($ServerPath))
    env = @{
        VW_MCP_HOST = $ListenHost
        VW_MCP_PORT = "$Port"
    }
}

$Json = $Config | ConvertTo-Json -Depth 5 -Compress
& claude mcp add-json $Name $Json

Write-Host "Registered Claude Code MCP server '$Name' -> $ServerPath"
Write-Host "Listener address: $ListenHost`:$Port"
