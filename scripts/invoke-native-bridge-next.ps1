[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$BuiltArtifact = "",
    [string]$SdkDir = "",
    [string]$SdkExamplesDir = "",
    [string]$WorktreeRoot = "",
    [string]$InstallDir = "",
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Debug",
    [switch]$Install,
    [ValidateRange(1, 20)]
    [int]$MaxSteps = 1,
    [switch]$AllowNetwork,
    [switch]$AllowInstallSoftware,
    [switch]$AllowDownloadLargeFiles,
    [switch]$AllowModifyVectorworksUserPlugins,
    [switch]$AllowVectorworksRestartStep,
    [switch]$AllowRebootRisk,
    [switch]$PlanOnly,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DoctorPath = Join-Path $PSScriptRoot "doctor-native-bridge.ps1"
if (-not (Test-Path -LiteralPath $DoctorPath -PathType Leaf)) {
    throw "Native bridge doctor was not found at $DoctorPath"
}

function Add-NamedArgument {
    param(
        [System.Collections.Generic.List[string]]$Arguments,
        [string]$Name,
        [string]$Value
    )
    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        $Arguments.Add("-$Name")
        $Arguments.Add($Value)
    }
}

function Invoke-NativeBridgeDoctor {
    $DoctorArgs = [System.Collections.Generic.List[string]]::new()
    Add-NamedArgument $DoctorArgs "VectorworksVersion" $VectorworksVersion
    Add-NamedArgument $DoctorArgs "BuiltArtifact" $BuiltArtifact
    Add-NamedArgument $DoctorArgs "SdkDir" $SdkDir
    Add-NamedArgument $DoctorArgs "SdkExamplesDir" $SdkExamplesDir
    Add-NamedArgument $DoctorArgs "WorktreeRoot" $WorktreeRoot
    Add-NamedArgument $DoctorArgs "InstallDir" $InstallDir
    if ($PSBoundParameters.ContainsKey("Configuration")) {
        Add-NamedArgument $DoctorArgs "Configuration" $Configuration
    }
    if ($Install) {
        $DoctorArgs.Add("-Install")
    }
    $DoctorArgs.Add("-Json")

    $Raw = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $DoctorPath @DoctorArgs | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "Native bridge doctor failed with exit code $LASTEXITCODE. Output: $Raw"
    }
    try {
        return $Raw | ConvertFrom-Json
    } catch {
        throw "Native bridge doctor did not emit valid JSON: $($_.Exception.Message)"
    }
}

function Get-SafetyBlockReasons {
    param([object]$Spec)

    $Reasons = [System.Collections.Generic.List[string]]::new()
    if ([bool]$Spec.requiresNetwork -and -not $AllowNetwork) {
        $Reasons.Add("requiresNetwork=true; rerun with -AllowNetwork")
    }
    if ([bool]$Spec.mayInstallSoftware -and -not $AllowInstallSoftware) {
        $Reasons.Add("mayInstallSoftware=true; rerun with -AllowInstallSoftware")
    }
    if ([bool]$Spec.mayDownloadLargeFiles -and -not $AllowDownloadLargeFiles) {
        $Reasons.Add("mayDownloadLargeFiles=true; rerun with -AllowDownloadLargeFiles")
    }
    if ([bool]$Spec.mayModifyVectorworksUserPlugins -and -not $AllowModifyVectorworksUserPlugins) {
        $Reasons.Add("mayModifyVectorworksUserPlugins=true; rerun with -AllowModifyVectorworksUserPlugins")
    }
    if ([bool]$Spec.requiresVectorworksRestartBeforeRun -and -not $AllowVectorworksRestartStep) {
        $Reasons.Add("requiresVectorworksRestartBeforeRun=true; restart/load Vectorworks as instructed, then rerun with -AllowVectorworksRestartStep")
    }
    if ([bool]$Spec.mayRequireReboot -and -not $AllowRebootRisk) {
        $Reasons.Add("mayRequireReboot=true; rerun with -AllowRebootRisk")
    }
    return @($Reasons)
}

function New-StepRecord {
    param(
        [int]$Index,
        [object]$DoctorReport,
        [object]$Spec
    )
    return [ordered]@{
        index = $Index
        stage = [string]$Spec.stage
        nextCommand = [string]$DoctorReport.nextCommand
        nextCommandReason = [string]$DoctorReport.nextCommandReason
        executable = [string]$Spec.executable
        arguments = @($Spec.arguments | ForEach-Object { [string]$_ })
        workingDirectory = [string]$Spec.workingDirectory
        safety = [ordered]@{
            requiresNetwork = [bool]$Spec.requiresNetwork
            mayInstallSoftware = [bool]$Spec.mayInstallSoftware
            mayDownloadLargeFiles = [bool]$Spec.mayDownloadLargeFiles
            mayModifyVectorworksUserPlugins = [bool]$Spec.mayModifyVectorworksUserPlugins
            requiresVectorworksRestartBeforeRun = [bool]$Spec.requiresVectorworksRestartBeforeRun
            mayRequireReboot = [bool]$Spec.mayRequireReboot
            isDryRun = [bool]$Spec.isDryRun
            rerunDoctorAfter = [bool]$Spec.rerunDoctorAfter
        }
        blockedReasons = @()
        plannedOnly = $false
        executed = $false
        exitCode = $null
        output = ""
        stopReason = ""
    }
}

$Steps = [System.Collections.Generic.List[object]]::new()
$Blocked = $false
$Failed = $false
$ExitCode = 0
$StopReason = ""

for ($Index = 1; $Index -le $MaxSteps; $Index++) {
    $DoctorReport = Invoke-NativeBridgeDoctor
    $Spec = $DoctorReport.nextCommandSpec
    if (-not $Spec -or [string]::IsNullOrWhiteSpace([string]$Spec.executable) -or @($Spec.arguments).Count -eq 0) {
        throw "Native bridge doctor did not return an executable nextCommandSpec."
    }

    $Step = New-StepRecord -Index $Index -DoctorReport $DoctorReport -Spec $Spec
    $BlockReasons = @(Get-SafetyBlockReasons -Spec $Spec)
    if ($PlanOnly) {
        $Step.blockedReasons = @($BlockReasons)
        $Step.plannedOnly = $true
        $Step.stopReason = "plan only"
        $Steps.Add([pscustomobject]$Step)
        $StopReason = $Step.stopReason
        break
    }

    if ($BlockReasons.Count -gt 0) {
        $Step.blockedReasons = @($BlockReasons)
        $Step.stopReason = "blocked by safety flags"
        $Steps.Add([pscustomobject]$Step)
        $Blocked = $true
        $ExitCode = 2
        $StopReason = $Step.stopReason
        break
    }

    $CommandArguments = @($Spec.arguments | ForEach-Object { [string]$_ })
    $WorkingDirectory = if ([string]::IsNullOrWhiteSpace([string]$Spec.workingDirectory)) { $RepoRoot } else { [string]$Spec.workingDirectory }
    if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
        throw "nextCommandSpec.workingDirectory does not exist: $WorkingDirectory"
    }

    Push-Location $WorkingDirectory
    try {
        $CommandOutput = & ([string]$Spec.executable) @CommandArguments 2>&1 | Out-String
        $CommandExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    $Step.executed = $true
    $Step.exitCode = $CommandExitCode
    $Step.output = $CommandOutput
    if ($CommandExitCode -ne 0) {
        $Step.stopReason = "child command failed"
        $Steps.Add([pscustomobject]$Step)
        $Failed = $true
        $ExitCode = if ($CommandExitCode) { $CommandExitCode } else { 1 }
        $StopReason = $Step.stopReason
        break
    }

    if ([bool]$Spec.isDryRun) {
        $Step.stopReason = "dry-run command executed; not escalating to a mutating command automatically"
        $Steps.Add([pscustomobject]$Step)
        $StopReason = $Step.stopReason
        break
    }
    if (-not [bool]$Spec.rerunDoctorAfter) {
        $Step.stopReason = "command executed; doctor rerun not requested"
        $Steps.Add([pscustomobject]$Step)
        $StopReason = $Step.stopReason
        break
    }
    if ($Index -eq $MaxSteps) {
        $Step.stopReason = "max steps reached after executing command"
        $Steps.Add([pscustomobject]$Step)
        $StopReason = $Step.stopReason
        break
    }

    $Step.stopReason = "command executed; rerunning doctor"
    $Steps.Add([pscustomobject]$Step)
}

$Result = [pscustomobject]@{
    repoRoot = $RepoRoot
    vectorworksVersion = $VectorworksVersion
    maxSteps = $MaxSteps
    planOnly = [bool]$PlanOnly
    blocked = [bool]$Blocked
    failed = [bool]$Failed
    exitCode = $ExitCode
    stopReason = $StopReason
    steps = @($Steps)
}

if ($Json) {
    $Result | ConvertTo-Json -Depth 12
} else {
    Write-Host "Vectorworks native bridge next-step runner"
    Write-Host "Blocked: $($Result.blocked)"
    Write-Host "Failed: $($Result.failed)"
    Write-Host "Stop reason: $($Result.stopReason)"
    foreach ($Step in $Result.steps) {
        Write-Host ""
        Write-Host ("Step {0}: {1}" -f $Step.index, $Step.stage)
        Write-Host ("Reason: {0}" -f $Step.nextCommandReason)
        if ($Step.blockedReasons.Count -gt 0) {
            Write-Host "Blocked reasons:"
            foreach ($Reason in $Step.blockedReasons) {
                Write-Host "- $Reason"
            }
        } elseif ($Step.plannedOnly) {
            Write-Host ("Plan: {0}" -f $Step.nextCommand)
        } elseif ($Step.executed) {
            Write-Host ("Executed: {0}" -f $Step.nextCommand)
            Write-Host ("Exit code: {0}" -f $Step.exitCode)
        }
    }
}

exit $ExitCode
