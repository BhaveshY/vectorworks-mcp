[CmdletBinding()]
param(
    [string]$HostName = "",
    [ValidateRange(1, 65535)]
    [int]$Port = 0,
    [ValidateRange(100, 30000)]
    [int]$TimeoutMilliseconds = 1200,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LauncherPath = Join-Path $RepoRoot "vw_start_listener_2024.py"
$NativeCheckerPath = Join-Path $RepoRoot "scripts\check-native-bridge-prereqs.ps1"
$ProjectMcpPath = Join-Path $RepoRoot ".mcp.json"
$ServerPath = Join-Path $RepoRoot "server.py"
$ListenerPath = Join-Path $RepoRoot "vw_listener.py"

if (-not $HostName) {
    $HostName = if ($env:VW_MCP_HOST) { $env:VW_MCP_HOST } else { "127.0.0.1" }
}
if ($Port -eq 0) {
    $Port = if ($env:VW_MCP_PORT) { [int]$env:VW_MCP_PORT } else { 9877 }
}

function New-Finding {
    param(
        [string]$Name,
        [string]$Status,
        [string]$Detail
    )
    [pscustomobject]@{
        name = $Name
        status = $Status
        detail = $Detail
    }
}

function Read-Exact {
    param(
        [System.IO.Stream]$Stream,
        [int]$Size
    )
    $Buffer = New-Object byte[] $Size
    $Offset = 0
    while ($Offset -lt $Size) {
        $Read = $Stream.Read($Buffer, $Offset, $Size - $Offset)
        if ($Read -le 0) {
            throw "listener closed before sending $Size bytes"
        }
        $Offset += $Read
    }
    return $Buffer
}

function Invoke-RawListenerPing {
    param(
        [string]$Address,
        [int]$PortNumber,
        [int]$TimeoutMs
    )
    $Client = [System.Net.Sockets.TcpClient]::new()
    try {
        $Async = $Client.BeginConnect($Address, $PortNumber, $null, $null)
        if (-not $Async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
            throw "timed out connecting to $Address`:$PortNumber"
        }
        $Client.EndConnect($Async)
        $Client.ReceiveTimeout = $TimeoutMs
        $Client.SendTimeout = $TimeoutMs

        $Request = @{ id = "doctor-ping"; action = "ping"; params = @{} } | ConvertTo-Json -Compress
        $Payload = [System.Text.Encoding]::UTF8.GetBytes($Request)
        $Header = [System.BitConverter]::GetBytes([uint32]$Payload.Length)
        if ([System.BitConverter]::IsLittleEndian) {
            [array]::Reverse($Header)
        }

        $Stream = $Client.GetStream()
        $Stream.Write($Header, 0, $Header.Length)
        $Stream.Write($Payload, 0, $Payload.Length)

        $ResponseHeader = Read-Exact -Stream $Stream -Size 4
        if ([System.BitConverter]::IsLittleEndian) {
            [array]::Reverse($ResponseHeader)
        }
        $Size = [System.BitConverter]::ToUInt32($ResponseHeader, 0)
        if ($Size -lt 1 -or $Size -gt 16777216) {
            throw "invalid response frame length $Size"
        }
        $ResponsePayload = Read-Exact -Stream $Stream -Size ([int]$Size)
        return ([System.Text.Encoding]::UTF8.GetString($ResponsePayload) | ConvertFrom-Json)
    } finally {
        $Client.Close()
    }
}

function Get-PortOwners {
    param([int]$PortNumber)
    $Connections = @(Get-NetTCPConnection -LocalPort $PortNumber -ErrorAction SilentlyContinue)
    foreach ($OwnerProcessId in ($Connections | Select-Object -ExpandProperty OwningProcess -Unique)) {
        $Process = Get-Process -Id $OwnerProcessId -ErrorAction SilentlyContinue
        if ($Process) {
            [pscustomobject]@{
                id = $Process.Id
                processName = $Process.ProcessName
                responding = if ($Process.PSObject.Properties.Name -contains "Responding") { [bool]$Process.Responding } else { $null }
                path = $Process.Path
            }
        } else {
            [pscustomobject]@{
                id = $OwnerProcessId
                processName = ""
                responding = $null
                path = ""
            }
        }
    }
}

$Findings = @()
$NextActions = New-Object System.Collections.Generic.List[string]

$Findings += New-Finding -Name "repo" -Status "ok" -Detail $RepoRoot

$RequiredFiles = @($ServerPath, $ListenerPath, $ProjectMcpPath)
$MissingFiles = @($RequiredFiles | Where-Object { -not (Test-Path -LiteralPath $_) })
if ($MissingFiles.Count -eq 0) {
    $Findings += New-Finding -Name "required files" -Status "ok" -Detail "server.py, vw_listener.py, and .mcp.json exist"
} else {
    $Findings += New-Finding -Name "required files" -Status "missing" -Detail ($MissingFiles -join "; ")
    $NextActions.Add("Restore missing connector files before setup.")
}

$LauncherStatus = "missing"
$LauncherDetail = $LauncherPath
if (Test-Path -LiteralPath $LauncherPath) {
    $LauncherText = Get-Content -Raw -LiteralPath $LauncherPath
    if ($LauncherText -match 'os\.environ\["VW_MCP_MODE"\]\s*=\s*["'']dialog["'']' -and
        $LauncherText -match 'os\.environ\["VW_MCP_DIALOG_TIMER_MS"\]\s*=\s*["'']50["'']') {
        $LauncherStatus = "ok"
        $LauncherDetail = "dialog launcher: $LauncherPath"
    } else {
        $LauncherStatus = "unsafe"
        $LauncherDetail = "launcher exists but is not the dialog agent-session launcher"
        $NextActions.Add("Run scripts\bootstrap-claude-code.ps1 -Verify, then replace the old Vectorworks script with vw_start_listener_2024.py.")
    }
} else {
    $NextActions.Add("Run scripts\bootstrap-claude-code.ps1 -Verify to generate vw_start_listener_2024.py.")
}
$Findings += New-Finding -Name "launcher" -Status $LauncherStatus -Detail $LauncherDetail

$Ping = $null
$PingError = ""
try {
    $Ping = Invoke-RawListenerPing -Address $HostName -PortNumber $Port -TimeoutMs $TimeoutMilliseconds
    if ($Ping.success) {
        $Result = $Ping.result
        $Findings += New-Finding -Name "listener ping" -Status "ok" -Detail ("bridge={0}; mode={1}; cad_api_safe={2}; transport_only={3}" -f $Result.bridge_kind, $Result.dispatch_mode, $Result.cad_api_safe, $Result.transport_only)
        if ($Result.transport_only -or $Result.cad_api_safe -eq $false) {
            $NextActions.Add("Do not call CAD handlers. Regenerate/run the dialog launcher, or build the native SDK bridge.")
        } else {
            $NextActions.Add("Listener is CAD-safe. Use vw_get_document_info next before CAD work.")
        }
    } else {
        $Findings += New-Finding -Name "listener ping" -Status "error" -Detail $Ping.error
        $NextActions.Add("Listener answered with an error; rerun the dialog launcher or inspect Vectorworks alerts.")
    }
} catch {
    $PingError = $_.Exception.Message
    $Findings += New-Finding -Name "listener ping" -Status "unreachable" -Detail $PingError
}

$PortOwners = @(Get-PortOwners -PortNumber $Port)
if ($PortOwners.Count -eq 0) {
    $Findings += New-Finding -Name "port owner" -Status "none" -Detail "nothing owns $HostName`:$Port"
    if (-not $Ping) {
        $NextActions.Add("Open Vectorworks 2024 and run the generated dialog launcher.")
    }
} else {
    $OwnerSummary = ($PortOwners | ForEach-Object { "{0}({1}) responding={2}" -f $_.processName, $_.id, $_.responding }) -join "; "
    $Findings += New-Finding -Name "port owner" -Status "present" -Detail $OwnerSummary
    if (-not $Ping) {
        $UnresponsiveVectorworks = @($PortOwners | Where-Object { $_.processName -like "Vectorworks*" -and $_.responding -eq $false })
        if ($UnresponsiveVectorworks.Count -gt 0) {
            $NextActions.Add("Vectorworks owns the port but is not responding. Save if possible, then restart Vectorworks before running the dialog launcher.")
        } else {
            $NextActions.Add("Port $Port is owned but ping failed. Create ~/.vectorworks-mcp/STOP; if it stays stuck, restart Vectorworks.")
        }
    }
}

$Native = $null
if (Test-Path -LiteralPath $NativeCheckerPath) {
    try {
        $NativeRaw = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $NativeCheckerPath -Advisory -Json | Out-String
        $Native = $NativeRaw | ConvertFrom-Json
        $NativeStatus = if ($Native.ready) { "ready" } else { "missing-prereqs" }
        $MissingNative = @($Native.checks | Where-Object { $_.required -and -not $_.ok } | ForEach-Object { $_.name })
        $NativeDetail = if ($Native.ready) { "native SDK bridge prerequisites are ready" } else { "missing: " + ($MissingNative -join ", ") }
        $Findings += New-Finding -Name "native bridge prereqs" -Status $NativeStatus -Detail $NativeDetail
    } catch {
        $Findings += New-Finding -Name "native bridge prereqs" -Status "error" -Detail $_.Exception.Message
    }
}

if ($NextActions.Count -eq 0) {
    $NextActions.Add("Run scripts\bootstrap-claude-code.ps1 -Verify, then run this doctor again.")
}

$Overall = "needs-attention"
if ($Ping -and $Ping.success -and $Ping.result.cad_api_safe -eq $true -and $LauncherStatus -eq "ok") {
    $Overall = "cad-ready"
} elseif ($Ping -and $Ping.success -and ($Ping.result.transport_only -or $Ping.result.cad_api_safe -eq $false)) {
    $Overall = "transport-only"
} elseif (-not $Ping -and $PortOwners.Count -gt 0) {
    $Overall = "stale-or-blocked-listener"
} elseif (-not $Ping) {
    $Overall = "listener-not-running"
}

$Report = [pscustomobject]@{
    overall = $Overall
    host = $HostName
    port = $Port
    findings = $Findings
    portOwners = $PortOwners
    ping = $Ping
    nativeBridge = $Native
    nextActions = @($NextActions)
}

if ($Json) {
    $Report | ConvertTo-Json -Depth 10
} else {
    Write-Host "Vectorworks MCP doctor"
    Write-Host "Overall: $Overall"
    Write-Host ""
    foreach ($Finding in $Findings) {
        Write-Host ("[{0}] {1}: {2}" -f $Finding.status, $Finding.name, $Finding.detail)
    }
    Write-Host ""
    Write-Host "Next action:"
    Write-Host ("- {0}" -f $NextActions[0])
}
