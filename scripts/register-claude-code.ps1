[CmdletBinding()]
param(
    [string]$Name = "vectorworks",
    [string]$ListenHost = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$Port = 9877,
    [ValidateRange(1, 3600)]
    [int]$TimeoutSeconds = 60,
    [string]$LauncherPath = "",
    [string]$LoaderPath = "",
    [switch]$SkipInstall,
    [switch]$NoClaudeConfig,
    [switch]$Verify
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ServerPath = Join-Path $RepoRoot "server.py"
$ListenerPath = Join-Path $RepoRoot "vw_listener.py"
$RunnerPath = Join-Path $RepoRoot "scripts\run-mcp-server.ps1"
$VerifierPath = Join-Path $RepoRoot "scripts\verify-no-vectorworks.ps1"

if (-not $LauncherPath) {
    $LauncherPath = Join-Path $RepoRoot "vw_start_listener_2024.py"
}
if (-not $LoaderPath) {
    $LoaderPath = Join-Path $RepoRoot "vw_load_listener_2024.py"
}

if (-not (Test-Path $ServerPath)) { throw "server.py was not found at $ServerPath" }
if (-not (Test-Path $ListenerPath)) { throw "vw_listener.py was not found at $ListenerPath" }
if (-not (Test-Path $RunnerPath)) { throw "run-mcp-server.ps1 was not found at $RunnerPath" }

function ConvertTo-PythonRawStringLiteral {
    param([string]$Value)
    $Escaped = $Value.Replace("\", "\\").Replace('"', '\"')
    return '"' + $Escaped + '"'
}

function Write-AtomicText {
    param(
        [string]$Path,
        [string]$Text,
        [string]$Encoding = "UTF8"
    )
    $Directory = Split-Path -Parent $Path
    if ($Directory) {
        New-Item -ItemType Directory -Force -Path $Directory | Out-Null
    }
    $TempPath = "$Path.tmp.$PID"
    Set-Content -Path $TempPath -Value $Text -Encoding $Encoding
    Move-Item -Force -Path $TempPath -Destination $Path
}

function New-VectorworksLauncher {
    param(
        [string]$Path,
        [string]$HostName,
        [int]$ListenPort
    )
    $StopDir = Join-Path $env:USERPROFILE ".vectorworks-mcp"
    $ListenerLiteral = ConvertTo-PythonRawStringLiteral $ListenerPath
    $StopDirLiteral = ConvertTo-PythonRawStringLiteral $StopDir
    $Text = @"
import os
import runpy

os.environ["VW_MCP_HOST"] = "$HostName"
os.environ["VW_MCP_PORT"] = "$ListenPort"
os.environ["VW_MCP_STOP_DIR"] = $StopDirLiteral
os.environ["VW_MCP_MODE"] = "dialog"
os.environ["VW_MCP_DIALOG_TIMER_MS"] = "50"

runpy.run_path($ListenerLiteral, run_name="__main__")
"@
    Write-AtomicText -Path $Path -Text $Text
    return $Path
}

function New-VectorworksLoader {
    param(
        [string]$Path,
        [string]$TargetLauncherPath
    )
    $LauncherLiteral = ConvertTo-PythonRawStringLiteral $TargetLauncherPath
    $Text = @"
import runpy

runpy.run_path($LauncherLiteral, run_name="__main__")
"@
    Write-AtomicText -Path $Path -Text $Text
    return $Path
}

function New-ClaudeServerConfig {
    param(
        [string]$HostName,
        [int]$ListenPort,
        [int]$ToolTimeoutSeconds
    )
    return [ordered]@{
        type = "stdio"
        command = "powershell.exe"
        args = @(
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            $RunnerPath
        )
        env = [ordered]@{
            VW_MCP_HOST = $HostName
            VW_MCP_PORT = "$ListenPort"
            VW_MCP_TIMEOUT = "$ToolTimeoutSeconds"
            VW_MCP_PREFLIGHT_CACHE_MS = "750"
            VW_MCP_STOP_DIR = (Join-Path $env:USERPROFILE ".vectorworks-mcp")
        }
        timeout = ($ToolTimeoutSeconds * 1000)
    }
}

function Set-ClaudeJsonMcpServer {
    param(
        [string]$ServerName,
        [System.Collections.IDictionary]$ServerConfig
    )

    $ClaudeJsonPath = Join-Path $env:USERPROFILE ".claude.json"
    if (Test-Path $ClaudeJsonPath) {
        $BackupPath = "$ClaudeJsonPath.bak.$(Get-Date -Format 'yyyyMMddHHmmss')"
        Copy-Item -LiteralPath $ClaudeJsonPath -Destination $BackupPath -Force
        try {
            $Root = Get-Content -Raw $ClaudeJsonPath | ConvertFrom-Json
        } catch {
            throw "Could not parse $ClaudeJsonPath. Backup written to $BackupPath. Error: $($_.Exception.Message)"
        }
    } else {
        $Root = [pscustomobject]@{}
    }

    if (-not ($Root.PSObject.Properties.Name -contains "mcpServers")) {
        $Root | Add-Member -MemberType NoteProperty -Name "mcpServers" -Value ([pscustomobject]@{})
    }

    if ($null -eq $Root.mcpServers -or $Root.mcpServers -isnot [pscustomobject]) {
        throw "$ClaudeJsonPath has an mcpServers property, but it is not a JSON object."
    }

    $ServerObject = [pscustomobject]$ServerConfig
    if ($Root.mcpServers.PSObject.Properties.Name -contains $ServerName) {
        $Root.mcpServers.$ServerName = $ServerObject
    } else {
        $Root.mcpServers | Add-Member -MemberType NoteProperty -Name $ServerName -Value $ServerObject
    }

    $Json = $Root | ConvertTo-Json -Depth 100
    Write-AtomicText -Path $ClaudeJsonPath -Text $Json
    return $ClaudeJsonPath
}

if (-not $SkipInstall) {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $RunnerPath -SetupOnly
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency bootstrap failed with exit code $LASTEXITCODE"
    }
}

$GeneratedLauncherPath = New-VectorworksLauncher -Path $LauncherPath -HostName $ListenHost -ListenPort $Port
$GeneratedLoaderPath = New-VectorworksLoader -Path $LoaderPath -TargetLauncherPath $GeneratedLauncherPath
$Config = New-ClaudeServerConfig -HostName $ListenHost -ListenPort $Port -ToolTimeoutSeconds $TimeoutSeconds
$Json = $Config | ConvertTo-Json -Depth 10 -Compress

if (-not $NoClaudeConfig) {
    $Claude = Get-Command claude -ErrorAction SilentlyContinue
    if ($Claude) {
        & $Claude.Source mcp add-json $Name $Json --scope user
        $FirstExitCode = $LASTEXITCODE
        if ($FirstExitCode -ne 0) {
            & $Claude.Source mcp add-json --scope user $Name $Json
            $SecondExitCode = $LASTEXITCODE
        } else {
            $SecondExitCode = 0
        }
        if ($FirstExitCode -ne 0 -and $SecondExitCode -ne 0) {
            throw "claude mcp add-json failed with exit codes $FirstExitCode and $SecondExitCode"
        }
        Write-Host "Registered Claude Code MCP server '$Name' with claude CLI."
    } else {
        $ClaudeJsonPath = Set-ClaudeJsonMcpServer -ServerName $Name -ServerConfig $Config
        Write-Warning "Claude Code CLI was not found on PATH; updated $ClaudeJsonPath directly instead."
        Write-Host "Restart Claude Code so it reloads the MCP server list."
    }
}

Write-Host "Vectorworks MCP setup complete."
Write-Host "Repo: $RepoRoot"
Write-Host "Listener address: $ListenHost`:$Port"
Write-Host "Vectorworks launcher: $GeneratedLauncherPath"
Write-Host "Vectorworks loader to paste/install: $GeneratedLoaderPath"
Write-Host "MCP runner: $RunnerPath"

if ($Verify) {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $VerifierPath -Name $Name -LauncherPath $GeneratedLauncherPath -LoaderPath $GeneratedLoaderPath
    exit $LASTEXITCODE
}
