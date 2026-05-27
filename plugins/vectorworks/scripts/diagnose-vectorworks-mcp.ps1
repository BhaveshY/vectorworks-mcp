[CmdletBinding()]
param(
    [string]$HostName = "",
    [ValidateRange(1, 65535)]
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"

if (-not $HostName) {
    $HostName = if ($env:VW_MCP_HOST) { $env:VW_MCP_HOST } else { "127.0.0.1" }
}
if ($Port -eq 0) {
    $Port = if ($env:VW_MCP_PORT) { [int]$env:VW_MCP_PORT } else { 9877 }
}

function Test-TcpPort {
    param(
        [string]$ComputerName,
        [int]$PortNumber,
        [int]$TimeoutMilliseconds = 1000
    )
    $Client = [System.Net.Sockets.TcpClient]::new()
    try {
        $Async = $Client.BeginConnect($ComputerName, $PortNumber, $null, $null)
        if (-not $Async.AsyncWaitHandle.WaitOne($TimeoutMilliseconds, $false)) {
            return $false
        }
        $Client.EndConnect($Async)
        return $true
    } catch {
        return $false
    } finally {
        $Client.Close()
    }
}

function Write-PortDiagnostics {
    param(
        [string]$Address,
        [int]$PortNumber
    )
    $Connections = @(Get-NetTCPConnection -LocalPort $PortNumber -ErrorAction SilentlyContinue)
    if (-not $Connections) {
        Write-Host "Port owner: none found for $Address`:$PortNumber"
        return
    }

    Write-Host "TCP state for local port ${PortNumber}:"
    $Connections |
        Select-Object LocalAddress,LocalPort,RemoteAddress,RemotePort,State,OwningProcess |
        Format-Table -AutoSize | Out-String | Write-Host

    Write-Host "Owning process(es):"
    $Connections |
        Select-Object -ExpandProperty OwningProcess -Unique |
        ForEach-Object {
            Get-Process -Id $_ -ErrorAction SilentlyContinue |
                Select-Object Id,ProcessName,Path |
                Format-List | Out-String | Write-Host
        }
}

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$RepoRoot = $null
try {
    $ResolverArgs = @()
    if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
    $RepoRoot = (& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Resolver @ResolverArgs | Select-Object -Last 1).Trim()
} catch {
    Write-Host "Repo: NOT FOUND"
    Write-Host $_.Exception.Message
}

if ($RepoRoot) {
    $Launcher = Join-Path $RepoRoot "vw_start_listener_2024.py"
    $LauncherOk = (Test-Path -LiteralPath $Launcher) -and ((Get-Content -Raw -Path $Launcher) -match 'os\.environ\["VW_MCP_MODE"\]\s*=\s*["'']win_timer["'']')
    Write-Host "Repo: $RepoRoot"
    Write-Host "Generated launcher: $Launcher"
    Write-Host "Launcher Windows timer mode: $LauncherOk"
}

$Claude = Get-Command claude -ErrorAction SilentlyContinue
Write-Host "claude on PATH: $([bool]$Claude)"
if ($Claude) { Write-Host "claude path: $($Claude.Source)" }
$TcpReachable = Test-TcpPort -ComputerName $HostName -PortNumber $Port
Write-Host "Listener TCP $HostName`:$Port reachable: $TcpReachable"
Write-PortDiagnostics -Address $HostName -PortNumber $Port

if ($RepoRoot -and $TcpReachable) {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "test-vectorworks-listener.ps1") -HostName $HostName -Port $Port
}
