[CmdletBinding()]
param(
    [ValidateSet("HostOnly", "ClaudeCode")]
    [string]$Client = "HostOnly",
    [string]$InstallDir = "",
    [switch]$NoVerify,
    [switch]$SkipClipboard,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/BhaveshY/vectorworks-mcp.git"
$RawInstallUrl = "https://raw.githubusercontent.com/BhaveshY/vectorworks-mcp/main/install.ps1"
$DefaultInstallDir = Join-Path $env:USERPROFILE "repos\vectorworks-mcp"

function Test-ConnectorRepoRoot {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Container)) {
        return $false
    }
    $Root = (Resolve-Path -LiteralPath $Path).Path
    return (
        (Test-Path -LiteralPath (Join-Path $Root "server.py") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Root ".mcp.json") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Root "scripts\bootstrap-agent.ps1") -PathType Leaf)
    )
}

function Invoke-Git {
    param([string[]]$ArgumentList)
    $Git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $Git) {
        throw "Git was not found on PATH. Install Git first with: winget install --id Git.Git --exact --source winget"
    }
    & $Git.Source @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "git $($ArgumentList -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Resolve-ConnectorRepoRoot {
    if ($PSScriptRoot -and (Test-ConnectorRepoRoot $PSScriptRoot)) {
        return (Resolve-Path -LiteralPath $PSScriptRoot).Path
    }

    $CurrentDir = (Get-Location).Path
    if (Test-ConnectorRepoRoot $CurrentDir) {
        return (Resolve-Path -LiteralPath $CurrentDir).Path
    }

    $Target = $InstallDir
    if (-not $Target -and $env:VW_MCP_REPO) {
        $Target = $env:VW_MCP_REPO
    }
    if (-not $Target) {
        $Target = $DefaultInstallDir
    }
    $Target = [System.IO.Path]::GetFullPath($Target)

    if (Test-ConnectorRepoRoot $Target) {
        if (Test-Path -LiteralPath (Join-Path $Target ".git") -PathType Container) {
            Invoke-Git @("-C", $Target, "pull", "--ff-only")
        }
        return (Resolve-Path -LiteralPath $Target).Path
    }

    if (Test-Path -LiteralPath $Target) {
        $HasChildren = @(Get-ChildItem -LiteralPath $Target -Force -ErrorAction SilentlyContinue | Select-Object -First 1).Count -gt 0
        if ($HasChildren) {
            throw "InstallDir exists but is not a vectorworks-mcp checkout: $Target"
        }
    } else {
        $Parent = Split-Path -Parent $Target
        if ($Parent) {
            New-Item -ItemType Directory -Force -Path $Parent | Out-Null
        }
    }

    Invoke-Git @("clone", $RepoUrl, $Target)
    if (-not (Test-ConnectorRepoRoot $Target)) {
        throw "Cloned repo did not contain the expected Vectorworks MCP files: $Target"
    }
    return (Resolve-Path -LiteralPath $Target).Path
}

function New-InstallPayload {
    param(
        [bool]$Ok,
        [string]$RepoRoot,
        [string]$Message,
        [string]$ErrorMessage = ""
    )
    $LoaderPath = if ($RepoRoot) { Join-Path $RepoRoot "vw_load_listener_2024.py" } else { "" }
    $LauncherPath = if ($RepoRoot) { Join-Path $RepoRoot "vw_start_listener_2024.py" } else { "" }
    $McpConfigPath = if ($RepoRoot) { Join-Path $RepoRoot ".mcp.json" } else { "" }
    return [ordered]@{
        ok = $Ok
        setup_complete = $Ok
        install_complete = $Ok
        usable_now = $Ok
        requires_action = -not $Ok
        client = $Client
        repo_root = $RepoRoot
        mcp_config = $McpConfigPath
        vectorworks_loader = $LoaderPath
        vectorworks_launcher = $LauncherPath
        user_message = $Message
        next_action = if ($Ok) {
            "Trust or add the repo .mcp.json in your MCP client, run vw_load_listener_2024.py in Vectorworks, then call vw_ping."
        } else {
            "Fix the reported installer error, then rerun install.ps1."
        }
        native_note = "The native SDK bridge is an optional non-modal upgrade; missing SDK or C++ tools do not make this Python dialog fallback install fail."
        raw_install_url = $RawInstallUrl
        error = $ErrorMessage
    }
}

function Write-InstallPayload {
    param([System.Collections.IDictionary]$Payload)
    if ($Json) {
        $Payload | ConvertTo-Json -Depth 8
        return
    }

    if ($Payload.ok) {
        Write-Host $Payload.user_message
        Write-Host "Repo: $($Payload.repo_root)"
        Write-Host "MCP config: $($Payload.mcp_config)"
        Write-Host "Vectorworks loader: $($Payload.vectorworks_loader)"
        Write-Host "Next: $($Payload.next_action)"
    } else {
        Write-Error $Payload.user_message
        if ($Payload.error) {
            Write-Error $Payload.error
        }
    }
}

try {
    $RepoRoot = Resolve-ConnectorRepoRoot
    $BootstrapPath = Join-Path $RepoRoot "scripts\bootstrap-agent.ps1"
    $BootstrapArgs = @("-Client", $Client)
    if (-not $NoVerify) { $BootstrapArgs += "-Verify" }
    if ($SkipClipboard) { $BootstrapArgs += "-SkipClipboard" }

    Push-Location $RepoRoot
    try {
        if ($Json) {
            $BootstrapOutput = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $BootstrapPath @BootstrapArgs 2>&1
            if ($LASTEXITCODE -ne 0) {
                throw "bootstrap-agent.ps1 failed with exit code $LASTEXITCODE. Output: $($BootstrapOutput -join [Environment]::NewLine)"
            }
        } else {
            & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $BootstrapPath @BootstrapArgs
            if ($LASTEXITCODE -ne 0) {
                throw "bootstrap-agent.ps1 failed with exit code $LASTEXITCODE"
            }
        }
    } finally {
        Pop-Location
    }

    $Payload = New-InstallPayload -Ok $true -RepoRoot $RepoRoot -Message "Vectorworks MCP installed and usable now with the Python dialog fallback."
    Write-InstallPayload $Payload
    exit 0
} catch {
    $Payload = New-InstallPayload -Ok $false -RepoRoot $RepoRoot -Message "Vectorworks MCP install failed." -ErrorMessage $_.Exception.Message
    Write-InstallPayload $Payload
    exit 1
}
