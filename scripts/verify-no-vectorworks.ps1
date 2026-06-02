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
$RepoVenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$ProjectMcpPath = Join-Path $RepoRoot ".mcp.json"
$NativePrereqPath = Join-Path $RepoRoot "scripts\check-native-bridge-prereqs.ps1"
$NativeDoctorPath = Join-Path $RepoRoot "scripts\doctor-native-bridge.ps1"
$NativeNextRunnerPath = Join-Path $RepoRoot "scripts\invoke-native-bridge-next.ps1"
$NativeScaffoldTestPath = Join-Path $RepoRoot "scripts\test-native-bridge-scaffold.ps1"
$FreshLauncher = $false
$FreshLoader = $false
$PyCacheRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("vectorworks-mcp-verify-pycache-{0}" -f $PID)

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

function Invoke-InRepo {
    param([scriptblock]$Command)

    Push-Location $RepoRoot
    try {
        & $Command
    } finally {
        Pop-Location
    }
}

function Test-PythonExecutable {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    try {
        & $Path -c "import sys; sys.exit(0)" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Resolve-ActiveVenvPython {
    $Candidates = [System.Collections.Generic.List[string]]::new()
    $Candidates.Add($RepoVenvPython)
    if ($env:LOCALAPPDATA) {
        $Candidates.Add((Join-Path $env:LOCALAPPDATA "vectorworks-mcp\venv\Scripts\python.exe"))
    }
    if ($env:TEMP) {
        $Candidates.Add((Join-Path $env:TEMP "vectorworks-mcp\venv\Scripts\python.exe"))
    }

    foreach ($Candidate in ($Candidates | Select-Object -Unique)) {
        if (Test-PythonExecutable -Path $Candidate) {
            return $Candidate
        }
    }

    throw "Usable virtualenv Python was not found after bootstrap. Checked: $($Candidates -join ', ')"
}

Assert-Path $RunnerPath "MCP runner"
Assert-Path $RegisterPath "Claude Code registration script"
Assert-Path $ServerPath "MCP server"
Assert-Path $ListenerPath "Vectorworks listener"
Assert-Path $ProjectMcpPath "Project MCP config"
Assert-Path $NativePrereqPath "Native bridge prerequisite checker"
Assert-Path $NativeDoctorPath "Native bridge doctor"
Assert-Path $NativeNextRunnerPath "Native bridge next-step runner"
Assert-Path $NativeScaffoldTestPath "Native bridge scaffold smoke test"

Invoke-Checked "bootstrap venv/dependencies" {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $RunnerPath -SetupOnly
}

$VenvPython = Resolve-ActiveVenvPython
New-Item -ItemType Directory -Force -Path $PyCacheRoot *> $null
$env:PYTHONPYCACHEPREFIX = $PyCacheRoot

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
    Invoke-InRepo {
        & $VenvPython -c "import server"
    }
}

Invoke-Checked "Python compilation" {
    & $VenvPython -m py_compile $ServerPath $ListenerPath $LauncherPath $LoaderPath
}

Invoke-Checked "unit tests" {
    Invoke-InRepo {
        & $VenvPython -m unittest discover -v
    }
}

Invoke-Checked "native bridge scaffold compile smoke" {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $NativeScaffoldTestPath
}

Invoke-Checked "native bridge prereq advisory" {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $NativePrereqPath -Advisory
}

Invoke-Checked "native bridge doctor next command" {
    $ProbeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("vectorworks-mcp-verify-native-doctor-{0}" -f $PID)
    $DoctorJson = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $NativeDoctorPath -WorktreeRoot (Join-Path $ProbeRoot "SDKExamples") -InstallDir (Join-Path $ProbeRoot "Plug-ins") -Json
    $DoctorReport = $DoctorJson | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace([string]$DoctorReport.nextCommand) -or
        [string]::IsNullOrWhiteSpace([string]$DoctorReport.nextCommandReason) -or
        [string]::IsNullOrWhiteSpace([string]$DoctorReport.nextCommandSpec.command) -or
        [string]::IsNullOrWhiteSpace([string]$DoctorReport.nextCommandSpec.stage) -or
        @($DoctorReport.nextActions).Count -eq 0) {
        throw "Native bridge doctor JSON did not include nextCommand, nextCommandReason, nextCommandSpec, and nextActions."
    }
    if ([string]$DoctorReport.nextCommandSpec.command -ne [string]$DoctorReport.nextCommand -or
        @($DoctorReport.nextCommandSpec.arguments).Count -lt 6) {
        throw "Native bridge doctor nextCommandSpec does not match nextCommand."
    }
}

Invoke-Checked "native bridge guarded next-step plan" {
    $ProbeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("vectorworks-mcp-verify-native-runner-{0}" -f $PID)
    $RunnerJson = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $NativeNextRunnerPath -WorktreeRoot (Join-Path $ProbeRoot "SDKExamples") -InstallDir (Join-Path $ProbeRoot "Plug-ins") -PlanOnly -Json
    $RunnerReport = $RunnerJson | ConvertFrom-Json
    if ($RunnerReport.status -ne "plan_only" -or $RunnerReport.blocked -or $RunnerReport.failed -or -not $RunnerReport.planOnly -or @($RunnerReport.steps).Count -ne 1) {
        throw "Native bridge next-step runner did not emit a single non-mutating plan."
    }
    if (-not $RunnerReport.steps[0].plannedOnly -or [string]::IsNullOrWhiteSpace([string]$RunnerReport.steps[0].stage)) {
        throw "Native bridge next-step runner plan is missing plannedOnly/stage metadata."
    }
    if ($RunnerReport.PSObject.Properties.Name -notcontains "missingAllowFlags" -or
        $RunnerReport.PSObject.Properties.Name -notcontains "validationErrors" -or
        $RunnerReport.steps[0].PSObject.Properties.Name -notcontains "safetyBlocks" -or
        $RunnerReport.steps[0].PSObject.Properties.Name -notcontains "missingAllowFlags" -or
        $RunnerReport.steps[0].PSObject.Properties.Name -notcontains "validationErrors") {
        throw "Native bridge next-step runner plan is missing structured status/safety validation fields."
    }
    if (@($RunnerReport.validationErrors).Count -ne 0 -or @($RunnerReport.steps[0].validationErrors).Count -ne 0) {
        throw "Native bridge next-step runner plan unexpectedly reported validation errors."
    }
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
if (Test-Path -LiteralPath $PyCacheRoot) {
    Remove-Item -LiteralPath $PyCacheRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "OK: no-Vectorworks verification passed."
