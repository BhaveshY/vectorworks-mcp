[CmdletBinding()]
param(
    [switch]$SetupOnly
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ServerPath = Join-Path $RepoRoot "server.py"
$RequirementsPath = Join-Path $RepoRoot "requirements.txt"
$VenvDir = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

$BaseLogDir = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "vectorworks-mcp\logs"
} else {
    Join-Path $env:TEMP "vectorworks-mcp\logs"
}
$LogPath = Join-Path $BaseLogDir "mcp-server-bootstrap.log"
New-Item -ItemType Directory -Force -Path $BaseLogDir *> $null

function Write-BootstrapLog {
    param([string]$Message)
    Add-Content -Path $LogPath -Value ("{0} {1}" -f (Get-Date -Format "s"), $Message)
}

function Invoke-Logged {
    param([scriptblock]$Command)
    $OldErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Command *>> $LogPath
        $ExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $OldErrorActionPreference
    }
    if ($ExitCode -ne 0) {
        throw "Command failed with exit code $ExitCode. See $LogPath"
    }
}

function Get-HostPythonCommand {
    $Py = Get-Command py -ErrorAction SilentlyContinue
    if ($Py) {
        return @{ Command = $Py.Source; Args = @("-3") }
    }

    $Python = Get-Command python -ErrorAction SilentlyContinue
    if ($Python) {
        return @{ Command = $Python.Source; Args = @() }
    }

    throw "Python 3 was not found. Install Python 3.10+ and rerun setup. See $LogPath"
}

function Ensure-Venv {
    if (-not (Test-Path $VenvPython)) {
        Write-BootstrapLog "Creating virtual environment at $VenvDir"
        $HostPython = Get-HostPythonCommand
        Invoke-Logged { & $HostPython["Command"] @($HostPython["Args"] + @("-m", "venv", $VenvDir)) }
    }
}

function Test-FastMcpImport {
    & $VenvPython -c "import fastmcp" *> $null
    return ($LASTEXITCODE -eq 0)
}

function Ensure-Requirements {
    $StampPath = Join-Path $VenvDir ".requirements.sha256"
    $RequirementsHash = (Get-FileHash -Algorithm SHA256 $RequirementsPath).Hash
    $ExistingHash = if (Test-Path $StampPath) { Get-Content -Raw $StampPath } else { "" }

    if (($ExistingHash.Trim() -ne $RequirementsHash) -or (-not (Test-FastMcpImport))) {
        Write-BootstrapLog "Installing requirements from $RequirementsPath"
        Invoke-Logged { & $VenvPython -m pip install -r $RequirementsPath }
        Set-Content -Path $StampPath -Value $RequirementsHash -Encoding ASCII
    }
}

try {
    Ensure-Venv
    Ensure-Requirements
} catch {
    $ErrorText = ($_ | Out-String).Trim()
    Write-BootstrapLog "Bootstrap failed: $ErrorText"
    [Console]::Error.WriteLine("Vectorworks MCP bootstrap failed: $ErrorText")
    exit 1
}

if ($SetupOnly) {
    exit 0
}

if (-not $env:VW_MCP_HOST) { $env:VW_MCP_HOST = "127.0.0.1" }
if (-not $env:VW_MCP_PORT) { $env:VW_MCP_PORT = "9877" }
if (-not $env:VW_MCP_TIMEOUT) { $env:VW_MCP_TIMEOUT = "60" }
if (-not $env:VW_MCP_STOP_DIR) { $env:VW_MCP_STOP_DIR = Join-Path $env:USERPROFILE ".vectorworks-mcp" }

& $VenvPython $ServerPath
exit $LASTEXITCODE
