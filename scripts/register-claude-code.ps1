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
    [switch]$CopyLoaderToClipboard,
    [switch]$BestEffortClipboard,
    [switch]$Verify
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ServerPath = Join-Path $RepoRoot "server.py"
$ListenerPath = Join-Path $RepoRoot "vw_listener.py"
$RunnerPath = Join-Path $RepoRoot "scripts\run-mcp-server.ps1"
$VerifierPath = Join-Path $RepoRoot "scripts\verify-no-vectorworks.ps1"
$CopyLoaderPath = Join-Path $RepoRoot "scripts\copy-vectorworks-loader.ps1"
$DefaultStateDir = Join-Path $env:USERPROFILE ".vectorworks-mcp"
$AuthTokenPath = if ($env:VW_MCP_AUTH_TOKEN_FILE) {
    [System.IO.Path]::GetFullPath($env:VW_MCP_AUTH_TOKEN_FILE)
} else {
    Join-Path $DefaultStateDir "auth-token"
}

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

function Ensure-AuthToken {
    if ($env:VW_MCP_INSECURE_NO_AUTH) {
        return ""
    }
    if ($env:VW_MCP_AUTH_TOKEN) {
        $Token = $env:VW_MCP_AUTH_TOKEN.Trim()
    } else {
        $AuthDir = Split-Path -Parent $AuthTokenPath
        if ($AuthDir) {
            New-Item -ItemType Directory -Force -Path $AuthDir | Out-Null
        }
        if (-not (Test-Path -LiteralPath $AuthTokenPath -PathType Leaf)) {
            $Token = ([Guid]::NewGuid().ToString("N") + [Guid]::NewGuid().ToString("N"))
            Set-Content -LiteralPath $AuthTokenPath -Value $Token -Encoding ASCII -NoNewline
        } else {
            $Token = (Get-Content -Raw -LiteralPath $AuthTokenPath).Trim()
        }
    }
    if (-not $Token) {
        throw "Generated Vectorworks MCP auth token was empty."
    }
    $env:VW_MCP_AUTH_TOKEN_FILE = $AuthTokenPath
    $env:VW_MCP_AUTH_TOKEN = $Token
    return $Token
}

function New-VectorworksLauncher {
    param(
        [string]$Path,
        [string]$HostName,
        [int]$ListenPort,
        [string]$AuthToken
    )
    $StopDir = $DefaultStateDir
    $ListenerLiteral = ConvertTo-PythonRawStringLiteral $ListenerPath
    $StopDirLiteral = ConvertTo-PythonRawStringLiteral $StopDir
    $AuthTokenLiteral = ConvertTo-PythonRawStringLiteral $AuthToken
    $AuthTokenPathLiteral = ConvertTo-PythonRawStringLiteral $AuthTokenPath
    $AuthLines = if ($AuthToken) {
@"
os.environ["VW_MCP_AUTH_TOKEN_FILE"] = $AuthTokenPathLiteral
os.environ["VW_MCP_AUTH_TOKEN"] = $AuthTokenLiteral
"@
    } else {
@"
os.environ["VW_MCP_INSECURE_NO_AUTH"] = "1"
"@
    }
    $Text = @"
import os
import runpy

os.environ["VW_MCP_HOST"] = "$HostName"
os.environ["VW_MCP_PORT"] = "$ListenPort"
os.environ["VW_MCP_STOP_DIR"] = $StopDirLiteral
os.environ["VW_MCP_MODE"] = "dialog"
os.environ["VW_MCP_DIALOG_TIMER_MS"] = "50"
$AuthLines

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
    $RepoRootLiteral = ConvertTo-PythonRawStringLiteral $RepoRoot
    $ContractMarker = Join-Path $RepoRoot ".vectorworks-mcp-contract.json"
    $ContractVersion = 0
    $FeatureLiteral = ""
    if (Test-Path -LiteralPath $ContractMarker) {
        $Contract = Get-Content -Raw -LiteralPath $ContractMarker | ConvertFrom-Json
        $ContractVersion = [int]$Contract.contractVersion
        $FeatureLiteral = (@($Contract.requiredFeatures) | ForEach-Object {
            ConvertTo-PythonRawStringLiteral ([string]$_)
        }) -join ", "
    }
    $GeneratedAtUtc = [DateTime]::UtcNow.ToString("o")
    $Text = @"
import runpy

VW_MCP_LOADER_METADATA = {
    "repoRoot": $RepoRootLiteral,
    "launcherPath": $LauncherLiteral,
    "contractVersion": $ContractVersion,
    "requiredFeatures": [$FeatureLiteral],
    "generatedAtUtc": "$GeneratedAtUtc",
}

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
    $ServerEnv = [ordered]@{
        VW_MCP_HOST = $HostName
        VW_MCP_PORT = "$ListenPort"
        VW_MCP_TIMEOUT = "$ToolTimeoutSeconds"
        VW_MCP_PREFLIGHT_CACHE_MS = "750"
        VW_MCP_STOP_DIR = $DefaultStateDir
    }
    if ($env:VW_MCP_AUTH_TOKEN) {
        $ServerEnv["VW_MCP_AUTH_TOKEN_FILE"] = $AuthTokenPath
        $ServerEnv["VW_MCP_AUTH_TOKEN"] = $env:VW_MCP_AUTH_TOKEN
    } elseif ($env:VW_MCP_INSECURE_NO_AUTH) {
        $ServerEnv["VW_MCP_INSECURE_NO_AUTH"] = "1"
    }
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
        env = $ServerEnv
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

$AuthToken = Ensure-AuthToken
$GeneratedLauncherPath = New-VectorworksLauncher -Path $LauncherPath -HostName $ListenHost -ListenPort $Port -AuthToken $AuthToken
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

if ($CopyLoaderToClipboard) {
    $CopyArgs = @("-LauncherPath", $GeneratedLauncherPath, "-LoaderPath", $GeneratedLoaderPath)
    if ($BestEffortClipboard) { $CopyArgs += "-BestEffort" }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $CopyLoaderPath @CopyArgs
    if ($LASTEXITCODE -ne 0) {
        throw "copy-vectorworks-loader.ps1 failed with exit code $LASTEXITCODE"
    }
}

if ($Verify) {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $VerifierPath -Name $Name -LauncherPath $GeneratedLauncherPath -LoaderPath $GeneratedLoaderPath
    exit $LASTEXITCODE
}
