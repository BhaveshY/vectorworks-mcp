[CmdletBinding()]
param(
    [string]$Name = "vectorworks",
    [string]$LauncherPath = "",
    [string]$LoaderPath = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunnerPath = Join-Path $RepoRoot "scripts\run-mcp-server.ps1"
$RegisterPath = Join-Path $RepoRoot "scripts\register-claude-code.ps1"
$ServerPath = Join-Path $RepoRoot "server.py"
$ListenerPath = Join-Path $RepoRoot "vw_listener.py"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$ProjectMcpPath = Join-Path $RepoRoot ".mcp.json"
$NativePrereqPath = Join-Path $RepoRoot "scripts\check-native-bridge-prereqs.ps1"
$FreshLauncher = $false
$FreshLoader = $false

if (-not $LauncherPath) {
    $LauncherPath = Join-Path ([System.IO.Path]::GetTempPath()) ("vectorworks-mcp-verify-launcher-{0}.py" -f $PID)
    $FreshLauncher = $true
}
if (-not $LoaderPath) {
    $LoaderPath = Join-Path ([System.IO.Path]::GetTempPath()) ("vectorworks-mcp-verify-loader-{0}.py" -f $PID)
    $FreshLoader = $true
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
Assert-Path $RegisterPath "Claude Code registration script"
Assert-Path $ServerPath "MCP server"
Assert-Path $ListenerPath "Vectorworks listener"
Assert-Path $ProjectMcpPath "Project MCP config"
Assert-Path $NativePrereqPath "Native bridge prerequisite checker"

Invoke-Checked "bootstrap venv/dependencies" {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $RunnerPath -SetupOnly
}

Assert-Path $VenvPython "Virtualenv Python"

if ($FreshLauncher -or $FreshLoader -or -not (Test-Path $LauncherPath) -or -not (Test-Path $LoaderPath)) {
    Invoke-Checked "generate Vectorworks launcher" {
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $RegisterPath -SkipInstall -NoClaudeConfig -LauncherPath $LauncherPath -LoaderPath $LoaderPath
    }
}

Assert-Path $LauncherPath "Generated Vectorworks launcher"
Assert-Path $LoaderPath "Generated Vectorworks loader"

$LauncherText = Get-Content -Raw -Path $LauncherPath
if ($LauncherText -notmatch 'os\.environ\["VW_MCP_MODE"\]\s*=\s*["'']dialog["'']') {
    throw "Generated Vectorworks launcher does not set VW_MCP_MODE=dialog; regenerate it with scripts\register-claude-code.ps1."
}
if ($LauncherText -notmatch 'os\.environ\["VW_MCP_DIALOG_TIMER_MS"\]\s*=\s*["'']50["'']') {
    throw "Generated Vectorworks launcher does not set VW_MCP_DIALOG_TIMER_MS=50; regenerate it with scripts\register-claude-code.ps1."
}
$LoaderText = Get-Content -Raw -Path $LoaderPath
if ($LoaderText -notmatch 'runpy\.run_path') {
    throw "Generated Vectorworks loader does not run the launcher with runpy.run_path; regenerate it with scripts\register-claude-code.ps1."
}
$ExpectedLauncherLiteral = $LauncherPath.Replace("\", "\\").Replace('"', '\"')
if (-not $LoaderText.Contains($ExpectedLauncherLiteral)) {
    throw "Generated Vectorworks loader does not point at $LauncherPath; regenerate it with scripts\register-claude-code.ps1."
}

Invoke-Checked "fastmcp import" {
    & $VenvPython -c "import fastmcp"
}

Invoke-Checked "server import" {
    & $VenvPython -c "import server"
}

Invoke-Checked "Python compilation" {
    & $VenvPython -m py_compile $ServerPath $ListenerPath $LauncherPath $LoaderPath
}

Invoke-Checked "unit tests" {
    & $VenvPython -m unittest discover -v
}

Invoke-Checked "native bridge prereq advisory" {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $NativePrereqPath -Advisory
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

if ($FreshLauncher -and (Test-Path -LiteralPath $LauncherPath)) {
    Remove-Item -LiteralPath $LauncherPath -Force -ErrorAction SilentlyContinue
}
if ($FreshLoader -and (Test-Path -LiteralPath $LoaderPath)) {
    Remove-Item -LiteralPath $LoaderPath -Force -ErrorAction SilentlyContinue
}

Write-Host "OK: no-Vectorworks verification passed."
