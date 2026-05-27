[CmdletBinding()]
param(
    [string]$Name = "vectorworks",
    [string]$LauncherPath = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunnerPath = Join-Path $RepoRoot "scripts\run-mcp-server.ps1"
$ServerPath = Join-Path $RepoRoot "server.py"
$ListenerPath = Join-Path $RepoRoot "vw_listener.py"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$ProjectMcpPath = Join-Path $RepoRoot ".mcp.json"

if (-not $LauncherPath) {
    $LauncherPath = Join-Path $RepoRoot "vw_start_listener_2024.py"
}

function Assert-Path {
    param(
        [string]$Path,
        [string]$Label
    )
    if (-not (Test-Path $Path)) {
        throw "$Label was not found at $Path"
    }
}

function Invoke-Checked {
    param(
        [string]$Label,
        [scriptblock]$Command
    )
    Write-Host "Checking: $Label"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

Assert-Path $RunnerPath "MCP runner"
Assert-Path $ServerPath "MCP server"
Assert-Path $ListenerPath "Vectorworks listener"
Assert-Path $ProjectMcpPath "Project MCP config"

Invoke-Checked "bootstrap venv/dependencies" {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $RunnerPath -SetupOnly
}

Assert-Path $VenvPython "Virtualenv Python"
Assert-Path $LauncherPath "Generated Vectorworks launcher"

$LauncherText = Get-Content -Raw -Path $LauncherPath
if ($LauncherText -notmatch 'os\.environ\["VW_MCP_MODE"\]\s*=\s*["'']win_timer["'']') {
    throw "Generated Vectorworks launcher does not set VW_MCP_MODE=win_timer; regenerate it with scripts\register-claude-code.ps1."
}
if ($LauncherText -notmatch 'os\.environ\["VW_MCP_DIALOG_TIMER_MS"\]\s*=\s*["'']50["'']') {
    throw "Generated Vectorworks launcher does not set VW_MCP_DIALOG_TIMER_MS=50; regenerate it with scripts\register-claude-code.ps1."
}

Invoke-Checked "fastmcp import" {
    & $VenvPython -c "import fastmcp"
}

Invoke-Checked "server import" {
    & $VenvPython -c "import server"
}

Invoke-Checked "Python compilation" {
    & $VenvPython -m py_compile $ServerPath $ListenerPath $LauncherPath
}

Invoke-Checked "unit tests" {
    & $VenvPython -m unittest discover -v
}

try {
    $ProjectMcp = Get-Content -Raw $ProjectMcpPath | ConvertFrom-Json
} catch {
    throw ".mcp.json is not valid JSON: $($_.Exception.Message)"
}
if (-not ($ProjectMcp.PSObject.Properties.Name -contains "mcpServers")) {
    throw ".mcp.json does not contain mcpServers"
}
if (-not ($ProjectMcp.mcpServers.PSObject.Properties.Name -contains $Name)) {
    throw ".mcp.json does not contain mcpServers.$Name"
}

$ClaudeJsonPath = Join-Path $env:USERPROFILE ".claude.json"
if (Test-Path $ClaudeJsonPath) {
    try {
        $ClaudeJson = Get-Content -Raw $ClaudeJsonPath | ConvertFrom-Json
    } catch {
        throw "$ClaudeJsonPath is not valid JSON: $($_.Exception.Message)"
    }
    if ($ClaudeJson.PSObject.Properties.Name -contains "mcpServers" -and
        $ClaudeJson.mcpServers.PSObject.Properties.Name -contains $Name) {
        $ServerConfig = $ClaudeJson.mcpServers.$Name
        if (-not $ServerConfig.command) {
            throw "$ClaudeJsonPath mcpServers.$Name has no command"
        }
        Write-Host "Claude user config contains mcpServers.$Name."
    } else {
        Write-Warning "$ClaudeJsonPath does not contain mcpServers.$Name. Project .mcp.json still exists."
    }
}

Write-Host "OK: no-Vectorworks verification passed."
