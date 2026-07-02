[CmdletBinding()]
param(
    [switch]$SetupOnly
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ServerPath = Join-Path $RepoRoot "server.py"
$RequirementsPath = Join-Path $RepoRoot "requirements.txt"
$RepoVenvDir = Join-Path $RepoRoot ".venv"

$StateDir = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "vectorworks-mcp"
} else {
    Join-Path $env:TEMP "vectorworks-mcp"
}
$BaseLogDir = Join-Path $StateDir "logs"
$LogPath = Join-Path $BaseLogDir "mcp-server-bootstrap.log"
$ProtocolStateDir = if ($env:VW_MCP_STOP_DIR) {
    [System.IO.Path]::GetFullPath($env:VW_MCP_STOP_DIR)
} else {
    Join-Path $env:USERPROFILE ".vectorworks-mcp"
}
$AuthTokenPath = if ($env:VW_MCP_AUTH_TOKEN_FILE) {
    [System.IO.Path]::GetFullPath($env:VW_MCP_AUTH_TOKEN_FILE)
} else {
    Join-Path $ProtocolStateDir "auth-token"
}
$FallbackVenvDir = Join-Path $StateDir "venv"
$VenvDir = $RepoVenvDir
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
New-Item -ItemType Directory -Force -Path $BaseLogDir *> $null

function Write-BootstrapLog {
    param([string]$Message)
    Add-Content -Path $LogPath -Value ("{0} {1}" -f (Get-Date -Format "s"), $Message)
}

function Use-VenvDir {
    param([string]$Path)

    $script:VenvDir = $Path
    $script:VenvPython = Join-Path $script:VenvDir "Scripts\python.exe"
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

function Protect-AuthTokenFile {
    param([string]$Path)

    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }

    try {
        $ResolvedPath = (Resolve-Path -LiteralPath $Path).ProviderPath
        $Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        if ($null -eq $Identity -or $null -eq $Identity.User) {
            return
        }

        $Acl = New-Object System.Security.AccessControl.FileSecurity
        $Rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $Identity.User,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        $Acl.SetOwner($Identity.User)
        $Acl.SetAccessRuleProtection($true, $false)
        $Acl.AddAccessRule($Rule)
        Set-Acl -LiteralPath $ResolvedPath -AclObject $Acl
    } catch {
        return
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

function Test-PythonPip {
    param([string]$Path)

    if (-not (Test-PythonExecutable -Path $Path)) {
        return $false
    }

    try {
        & $Path -m pip --version *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Reset-Venv {
    param([string]$Reason)

    Write-BootstrapLog "$Reason; recreating $VenvDir"
    try {
        Remove-Item -LiteralPath $VenvDir -Recurse -Force
    } catch {
        if ($VenvDir -ne $FallbackVenvDir) {
            Write-BootstrapLog "Could not remove stale repo virtual environment: $($_.Exception.Message). Using fallback virtual environment at $FallbackVenvDir"
            Use-VenvDir -Path $FallbackVenvDir
            Ensure-Venv
            return $true
        }
        throw
    }

    return $false
}

function Ensure-Venv {
    if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        if (Test-PythonExecutable -Path $VenvPython) {
            if (Test-PythonPip -Path $VenvPython) {
                return
            }

            Write-BootstrapLog "Existing virtual environment pip could not run; attempting ensurepip in $VenvDir"
            try {
                Invoke-Logged { & $VenvPython -m ensurepip --upgrade }
            } catch {
                Write-BootstrapLog "ensurepip failed for existing virtual environment: $($_.Exception.Message)"
            }
            if (Test-PythonPip -Path $VenvPython) {
                return
            }

            if (Reset-Venv -Reason "Existing virtual environment pip could not run") {
                return
            }
        } else {
            if (Reset-Venv -Reason "Existing virtual environment python could not run") {
                return
            }
        }
    }

    Write-BootstrapLog "Creating virtual environment at $VenvDir"
    $HostPython = Get-HostPythonCommand
    try {
        Invoke-Logged { & $HostPython["Command"] @($HostPython["Args"] + @("-m", "venv", $VenvDir)) }
    } catch {
        if ($VenvDir -ne $FallbackVenvDir) {
            Write-BootstrapLog "Could not create repo virtual environment: $($_.Exception.Message). Using fallback virtual environment at $FallbackVenvDir"
            Use-VenvDir -Path $FallbackVenvDir
            Ensure-Venv
            return
        }
        throw
    }
}

function Test-FastMcpImport {
    try {
        & $VenvPython -c "import fastmcp" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Ensure-AuthToken {
    if ($env:VW_MCP_INSECURE_NO_AUTH) {
        return
    }
    $AuthDir = Split-Path -Parent $AuthTokenPath
    if ($AuthDir) {
        New-Item -ItemType Directory -Force -Path $AuthDir *> $null
    }
    if ($env:VW_MCP_AUTH_TOKEN) {
        $Token = $env:VW_MCP_AUTH_TOKEN.Trim()
    } elseif (Test-Path -LiteralPath $AuthTokenPath -PathType Leaf) {
        $Token = (Get-Content -Raw -LiteralPath $AuthTokenPath).Trim()
    } else {
        $Token = ([Guid]::NewGuid().ToString("N") + [Guid]::NewGuid().ToString("N"))
    }
    if (-not $Token) {
        throw "Generated Vectorworks MCP auth token was empty."
    }
    if ((-not (Test-Path -LiteralPath $AuthTokenPath -PathType Leaf)) -or ((Get-Content -Raw -LiteralPath $AuthTokenPath).Trim() -ne $Token)) {
        Set-Content -LiteralPath $AuthTokenPath -Value $Token -Encoding ASCII -NoNewline
    }
    Protect-AuthTokenFile -Path $AuthTokenPath
    $env:VW_MCP_AUTH_TOKEN_FILE = $AuthTokenPath
    Remove-Item Env:\VW_MCP_AUTH_TOKEN -ErrorAction SilentlyContinue
}

function Ensure-Requirements {
    $StampPath = Join-Path $VenvDir ".requirements.sha256"
    $RequirementsHash = (Get-FileHash -Algorithm SHA256 $RequirementsPath).Hash
    $ExistingHash = if (Test-Path $StampPath) { Get-Content -Raw $StampPath } else { "" }

    if (($ExistingHash.Trim() -ne $RequirementsHash) -or (-not (Test-FastMcpImport))) {
        Write-BootstrapLog "Installing requirements from $RequirementsPath"
        Invoke-Logged { & $VenvPython -m pip install -r $RequirementsPath }
        if (-not (Test-FastMcpImport)) {
            Write-BootstrapLog "fastmcp import still failed after normal install; force-reinstalling requirements from $RequirementsPath"
            Invoke-Logged { & $VenvPython -m pip install --upgrade --force-reinstall -r $RequirementsPath }
        }
        if (-not (Test-FastMcpImport)) {
            throw "fastmcp import failed after requirements installation. See $LogPath"
        }
        Set-Content -Path $StampPath -Value $RequirementsHash -Encoding ASCII
    }
}

try {
    Ensure-Venv
    Ensure-Requirements
    if (-not $env:VW_MCP_STOP_DIR) { $env:VW_MCP_STOP_DIR = Join-Path $env:USERPROFILE ".vectorworks-mcp" }
    Ensure-AuthToken
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

& $VenvPython $ServerPath
exit $LASTEXITCODE
