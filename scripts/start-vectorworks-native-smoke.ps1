[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$VectorworksExe = "",
    [string]$HostName = "",
    [ValidateRange(1, 65535)]
    [int]$Port = 0,
    [ValidateRange(5, 600)]
    [int]$StartupTimeoutSeconds = 90,
    [ValidateRange(100, 10000)]
    [int]$ProbeIntervalMilliseconds = 1000,
    [switch]$RestartIfRunning,
    [switch]$ForceKillIfCloseFails,
    [switch]$NoStart,
    [switch]$RunPhase2,
    [switch]$AllowWriteFixture,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SmokePath = Join-Path $PSScriptRoot "smoke-native-bridge.ps1"
if (-not (Test-Path -LiteralPath $SmokePath -PathType Leaf)) {
    throw "Native bridge smoke script was not found at $SmokePath"
}

if (-not $HostName) {
    $HostName = if ($env:VW_MCP_HOST) { $env:VW_MCP_HOST } else { "127.0.0.1" }
}
if ($Port -eq 0) {
    $Port = if ($env:VW_MCP_PORT) { [int]$env:VW_MCP_PORT } else { 9877 }
}
if (-not $ForceKillIfCloseFails -and $env:VW_MCP_FORCE_VECTORWORKS_RESTART -in @("1", "true", "TRUE", "yes", "YES")) {
    $ForceKillIfCloseFails = $true
}

$Failures = [System.Collections.Generic.List[string]]::new()
$Actions = [System.Collections.Generic.List[string]]::new()
$RunningBefore = @()
$RunningAfter = @()
$CloseRequested = $false
$ForceKilled = $false
$Started = $false
$StartedProcessId = $null
$PortOpened = $false
$SmokeAttempted = $false
$SmokeExitCode = $null
$SmokePayload = $null
$SmokeRaw = ""

function Add-Failure {
    param([string]$Message)
    if ($Message -and -not $Failures.Contains($Message)) {
        $Failures.Add($Message) | Out-Null
    }
}

function Add-Action {
    param([string]$Message)
    if ($Message -and -not $Actions.Contains($Message)) {
        $Actions.Add($Message) | Out-Null
    }
}

function Get-FirstExistingFile {
    param([string[]]$Paths)
    foreach ($Path in $Paths) {
        if ($Path -and (Test-Path -LiteralPath $Path -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $Path).Path
        }
    }
    return ""
}

function Resolve-VectorworksExe {
    if ($VectorworksExe) {
        if (Test-Path -LiteralPath $VectorworksExe -PathType Leaf) {
            return (Resolve-Path -LiteralPath $VectorworksExe).Path
        }
        return $VectorworksExe
    }
    if ($env:VW_MCP_VECTORWORKS_EXE) {
        $Configured = [string]$env:VW_MCP_VECTORWORKS_EXE
        if (Test-Path -LiteralPath $Configured -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Configured).Path
        }
    }

    $Candidates = @()
    if ($env:ProgramFiles) {
        $Candidates += Join-Path $env:ProgramFiles "Vectorworks $VectorworksVersion\Vectorworks$VectorworksVersion.exe"
        $Candidates += Join-Path $env:ProgramFiles "Vectorworks $VectorworksVersion\Vectorworks.exe"
    }
    if (${env:ProgramFiles(x86)}) {
        $Candidates += Join-Path ${env:ProgramFiles(x86)} "Vectorworks $VectorworksVersion\Vectorworks$VectorworksVersion.exe"
        $Candidates += Join-Path ${env:ProgramFiles(x86)} "Vectorworks $VectorworksVersion\Vectorworks.exe"
    }
    $Found = Get-FirstExistingFile -Paths $Candidates
    if ($Found) { return $Found }

    foreach ($Base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $Base -or -not (Test-Path -LiteralPath $Base -PathType Container)) {
            continue
        }
        $InstallDir = Get-ChildItem -LiteralPath $Base -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "Vectorworks $VectorworksVersion*" } |
            Sort-Object FullName |
            Select-Object -First 1
        if ($InstallDir) {
            $Exe = Get-ChildItem -LiteralPath $InstallDir.FullName -File -Filter "Vectorworks*.exe" -ErrorAction SilentlyContinue |
                Sort-Object FullName |
                Select-Object -First 1
            if ($Exe) { return $Exe.FullName }
        }
    }
    return ""
}

function Get-VectorworksProcesses {
    $Processes = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -like "Vectorworks*"
    })
    return @($Processes)
}

function Test-PortOpen {
    param([int]$TimeoutMilliseconds = 500)
    $Client = [System.Net.Sockets.TcpClient]::new()
    try {
        $Async = $Client.BeginConnect($HostName, $Port, $null, $null)
        $Connected = $Async.AsyncWaitHandle.WaitOne($TimeoutMilliseconds)
        if (-not $Connected) { return $false }
        $Client.EndConnect($Async)
        return $Client.Connected
    } catch {
        return $false
    } finally {
        $Client.Close()
    }
}

function Wait-PortOpen {
    $Deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while ((Get-Date) -lt $Deadline) {
        if (Test-PortOpen -TimeoutMilliseconds 500) {
            return $true
        }
        Start-Sleep -Milliseconds $ProbeIntervalMilliseconds
    }
    return $false
}

function Stop-RunningVectorworksForRestart {
    param([object[]]$Processes)
    if ($Processes.Count -eq 0) { return $true }

    $script:CloseRequested = $true
    foreach ($Process in $Processes) {
        try {
            if ($Process.MainWindowHandle -ne 0) {
                [void]$Process.CloseMainWindow()
            }
        } catch {
            Add-Failure "Could not request Vectorworks process $($Process.Id) to close: $($_.Exception.Message)"
        }
    }

    $Deadline = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $Deadline) {
        $StillRunning = @(Get-VectorworksProcesses | Where-Object { $_.Id -in @($Processes.Id) })
        if ($StillRunning.Count -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }

    $StillRunning = @(Get-VectorworksProcesses | Where-Object { $_.Id -in @($Processes.Id) })
    if ($StillRunning.Count -eq 0) {
        return $true
    }

    if ($ForceKillIfCloseFails) {
        foreach ($Process in $StillRunning) {
            try {
                Stop-Process -Id $Process.Id -Force -ErrorAction Stop
                $script:ForceKilled = $true
            } catch {
                Add-Failure "Could not force-stop Vectorworks process $($Process.Id): $($_.Exception.Message)"
            }
        }
        Start-Sleep -Seconds 2
        return @(Get-VectorworksProcesses | Where-Object { $_.Id -in @($Processes.Id) }).Count -eq 0
    }

    Add-Failure "Vectorworks is running and did not close after a graceful restart request. Save/close any prompts, then rerun. Agents may set VW_MCP_FORCE_VECTORWORKS_RESTART=1 only when the user confirms no CAD work is open."
    Add-Action "Close Vectorworks prompts or save open files, then rerun install.ps1 -FullNative."
    return $false
}

function New-Report {
    param([bool]$Ok)
    [ordered]@{
        ok = [bool]$Ok
        vectorworksVersion = $VectorworksVersion
        vectorworksExe = $ResolvedVectorworksExe
        host = $HostName
        port = $Port
        startupTimeoutSeconds = $StartupTimeoutSeconds
        restartIfRunning = [bool]$RestartIfRunning
        forceKillIfCloseFails = [bool]$ForceKillIfCloseFails
        noStart = [bool]$NoStart
        runningBefore = @($RunningBefore | ForEach-Object { [ordered]@{ id = $_.Id; processName = $_.ProcessName } })
        runningAfter = @($RunningAfter | ForEach-Object { [ordered]@{ id = $_.Id; processName = $_.ProcessName } })
        closeRequested = [bool]$CloseRequested
        forceKilled = [bool]$ForceKilled
        started = [bool]$Started
        startedProcessId = $StartedProcessId
        portOpened = [bool]$PortOpened
        smokeAttempted = [bool]$SmokeAttempted
        smokeExitCode = $SmokeExitCode
        smoke = $SmokePayload
        smokeRaw = $SmokeRaw
        failures = @($Failures)
        nextActions = @($Actions)
        nextAction = if ($Actions.Count -gt 0) { [string]$Actions[0] } else { "" }
    }
}

$ResolvedVectorworksExe = Resolve-VectorworksExe
$RunningBefore = @(Get-VectorworksProcesses)
$PortAlreadyOpen = Test-PortOpen -TimeoutMilliseconds 300

if ($RestartIfRunning -and $RunningBefore.Count -gt 0) {
    [void](Stop-RunningVectorworksForRestart -Processes $RunningBefore)
}

if (-not (Test-PortOpen -TimeoutMilliseconds 300)) {
    if ($NoStart) {
        Add-Failure "Native bridge port $HostName`:$Port is not open and -NoStart was supplied."
        Add-Action "Start Vectorworks $VectorworksVersion or rerun without -NoStart so the agent can launch it."
    } elseif (-not $ResolvedVectorworksExe -or -not (Test-Path -LiteralPath $ResolvedVectorworksExe -PathType Leaf)) {
        Add-Failure "Vectorworks $VectorworksVersion executable was not found. Set VW_MCP_VECTORWORKS_EXE or pass -VectorworksExe."
        Add-Action "Install Vectorworks $VectorworksVersion or set VW_MCP_VECTORWORKS_EXE to the full Vectorworks.exe path, then rerun install.ps1 -FullNative."
    } elseif ($Failures.Count -eq 0) {
        try {
            $Process = Start-Process -FilePath $ResolvedVectorworksExe -PassThru
            $Started = $true
            $StartedProcessId = $Process.Id
        } catch {
            Add-Failure "Could not start Vectorworks $VectorworksVersion from $ResolvedVectorworksExe`: $($_.Exception.Message)"
            Add-Action "Start Vectorworks $VectorworksVersion manually, then rerun install.ps1 -FullNative."
        }
    }
}

if ($Failures.Count -eq 0) {
    $PortOpened = if ($PortAlreadyOpen -and -not $RestartIfRunning) { $true } else { Wait-PortOpen }
    if (-not $PortOpened) {
        Add-Failure "Vectorworks started or was already running, but the native bridge did not open $HostName`:$Port within $StartupTimeoutSeconds seconds."
        Add-Action "Confirm Vectorworks $VectorworksVersion opens without license/startup prompts and that the installed native plug-in is enabled, then rerun install.ps1 -FullNative."
    }
}

if ($Failures.Count -eq 0 -and $PortOpened) {
    $SmokeAttempted = $true
    $SmokeArgs = @(
        "-HostName", $HostName,
        "-Port", [string]$Port,
        "-TimeoutSeconds", "8",
        "-PingCount", $(if ($RunPhase2) { "3" } else { "10" }),
        "-ReadCount", $(if ($RunPhase2) { "2" } else { "1" }),
        "-Phase", $(if ($RunPhase2) { "2" } else { "0" }),
        "-Json"
    )
    if ($RunPhase2) {
        $SmokeArgs += "-IncludeObjects"
        if ($AllowWriteFixture) { $SmokeArgs += "-AllowWriteFixture" }
    } else {
        $SmokeArgs += "-Stop"
    }

    $SmokeRaw = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $SmokePath @SmokeArgs 2>&1 | Out-String
    $SmokeExitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
    try {
        $SmokePayload = $SmokeRaw | ConvertFrom-Json
    } catch {
        $SmokePayload = $null
    }
    if ($SmokeExitCode -ne 0) {
        Add-Failure "Native bridge smoke failed with exit code $SmokeExitCode."
        Add-Action "Open Vectorworks $VectorworksVersion, confirm the native plug-in is loaded, then run the command in native_summary.next_command."
    }
}

$RunningAfter = @(Get-VectorworksProcesses)
$Ok = $Failures.Count -eq 0
$Report = New-Report -Ok $Ok

if ($Json) {
    $Report | ConvertTo-Json -Depth 12
} elseif ($Ok) {
    Write-Host "Vectorworks native bridge smoke automation passed."
    Write-Host "Vectorworks: $ResolvedVectorworksExe"
    Write-Host "Target: $HostName`:$Port"
} else {
    Write-Host "Vectorworks native bridge smoke automation did not complete."
    foreach ($Failure in $Failures) {
        Write-Host "ERROR: $Failure"
    }
    foreach ($Action in $Actions) {
        Write-Host "Next: $Action"
    }
}

exit $(if ($Ok) { 0 } else { 2 })
