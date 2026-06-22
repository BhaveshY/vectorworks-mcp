[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$BuiltArtifact = "",
    [string]$SdkDir = "",
    [string]$SdkArchivePath = "",
    [string]$SdkExamplesDir = "",
    [string]$WorktreeRoot = "",
    [string]$InstallDir = "",
    [string]$DoctorPath = "",
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Debug",
    [switch]$Install,
    [ValidateRange(1, 20)]
    [int]$MaxSteps = 1,
    [switch]$AllowNetwork,
    [switch]$AllowInstallSoftware,
    [switch]$AllowSoftwareInstall,
    [switch]$AllowDownloadLargeFiles,
    [switch]$AllowLargeDownloads,
    [switch]$AllowModifyVectorworksUserPlugins,
    [switch]$AllowVectorworksPluginModify,
    [switch]$AllowVectorworksRestartStep,
    [switch]$AllowRebootRisk,
    [switch]$PlanOnly,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $DoctorPath) {
    $DoctorPath = Join-Path $PSScriptRoot "doctor-native-bridge.ps1"
}
if (-not (Test-Path -LiteralPath $DoctorPath -PathType Leaf)) {
    throw "Native bridge doctor was not found at $DoctorPath"
}
$DoctorPath = (Resolve-Path -LiteralPath $DoctorPath).Path
$AllowInstallSoftwareEffective = [bool]($AllowInstallSoftware -or $AllowSoftwareInstall)
$AllowDownloadLargeFilesEffective = [bool]($AllowDownloadLargeFiles -or $AllowLargeDownloads)
$AllowModifyVectorworksUserPluginsEffective = [bool]($AllowModifyVectorworksUserPlugins -or $AllowVectorworksPluginModify)

$KnownNativeDoctorStages = @(
    "bootstrap-native-prereqs",
    "prepare-native-source",
    "repair-native-source",
    "build-unmodified-sdk-example",
    "copy-native-scaffold",
    "wire-native-project",
    "build-native-bridge",
    "dry-run-install-native-artifact",
    "install-native-artifact",
    "smoke-phase-0",
    "rerun-native-doctor"
)

$BooleanCommandSpecFields = @(
    "requiresNetwork",
    "mayInstallSoftware",
    "mayDownloadLargeFiles",
    "mayModifyVectorworksUserPlugins",
    "requiresVectorworksRestartBeforeRun",
    "mayRequireReboot",
    "isDryRun",
    "rerunDoctorAfter"
)

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
    Add-NamedArgument $DoctorArgs "SdkArchivePath" $SdkArchivePath
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

function Test-PathUnderDirectory {
    param(
        [string]$Path,
        [string]$Directory
    )
    if ([string]::IsNullOrWhiteSpace($Path) -or [string]::IsNullOrWhiteSpace($Directory)) {
        return $false
    }
    try {
        $ResolvedPath = [System.IO.Path]::GetFullPath($Path)
        $ResolvedDirectory = [System.IO.Path]::GetFullPath($Directory)
    } catch {
        return $false
    }
    $TrimChars = @([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $DirectoryPrefix = $ResolvedDirectory.TrimEnd($TrimChars) + [System.IO.Path]::DirectorySeparatorChar
    return (
        $ResolvedPath.Equals($ResolvedDirectory, [System.StringComparison]::OrdinalIgnoreCase) -or
        $ResolvedPath.StartsWith($DirectoryPrefix, [System.StringComparison]::OrdinalIgnoreCase)
    )
}

function Test-SameFullPath {
    param(
        [string]$Left,
        [string]$Right
    )
    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) {
        return $false
    }
    try {
        $ResolvedLeft = [System.IO.Path]::GetFullPath($Left)
        $ResolvedRight = [System.IO.Path]::GetFullPath($Right)
    } catch {
        return $false
    }
    return $ResolvedLeft.Equals($ResolvedRight, [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-NativeCommandSpec {
    param(
        [object]$DoctorReport,
        [object]$Spec
    )

    $Errors = [System.Collections.Generic.List[string]]::new()
    if (-not $Spec) {
        $Errors.Add("nextCommandSpec is missing.")
        return @($Errors)
    }

    $SpecPropertyNames = @($Spec.PSObject.Properties.Name)
    foreach ($RequiredField in @("stage", "executable", "arguments", "workingDirectory", "scriptPath", "command")) {
        if ($RequiredField -notin $SpecPropertyNames) {
            $Errors.Add("nextCommandSpec.$RequiredField is missing.")
        }
    }
    if ($Errors.Count -gt 0) {
        return @($Errors)
    }

    if ([string]::IsNullOrWhiteSpace([string]$Spec.stage) -or [string]$Spec.stage -notin $KnownNativeDoctorStages) {
        $Errors.Add("nextCommandSpec.stage is not recognized: $($Spec.stage)")
    }
    if ([string]$Spec.executable -ne "powershell.exe") {
        $Errors.Add("nextCommandSpec.executable must be powershell.exe.")
    }
    if ([string]$Spec.command -ne [string]$DoctorReport.nextCommand) {
        $Errors.Add("nextCommandSpec.command must exactly match nextCommand.")
    }

    $Arguments = @($Spec.arguments | ForEach-Object { [string]$_ })
    if ($Arguments.Count -lt 6) {
        $Errors.Add("nextCommandSpec.arguments must contain the full PowerShell argument array.")
    }

    if (-not (Test-SameFullPath -Left ([string]$Spec.workingDirectory) -Right $RepoRoot)) {
        $Errors.Add("nextCommandSpec.workingDirectory must be the companion repo root.")
    }

    $ResolvedScriptPath = ""
    if ([string]::IsNullOrWhiteSpace([string]$Spec.scriptPath)) {
        $Errors.Add("nextCommandSpec.scriptPath is required.")
    } elseif (-not [System.IO.Path]::IsPathRooted([string]$Spec.scriptPath)) {
        $Errors.Add("nextCommandSpec.scriptPath must be absolute.")
    } else {
        try {
            $ResolvedScriptPath = [System.IO.Path]::GetFullPath([string]$Spec.scriptPath)
        } catch {
            $Errors.Add("nextCommandSpec.scriptPath could not be resolved: $($_.Exception.Message)")
        }
    }

    $ScriptsRoot = Join-Path $RepoRoot "scripts"
    if ($ResolvedScriptPath) {
        if (-not (Test-PathUnderDirectory -Path $ResolvedScriptPath -Directory $ScriptsRoot)) {
            $Errors.Add("nextCommandSpec.scriptPath must be under the companion scripts folder.")
        } elseif (-not (Test-Path -LiteralPath $ResolvedScriptPath -PathType Leaf)) {
            $Errors.Add("nextCommandSpec.scriptPath must point to an existing script.")
        }
    }

    $FileIndex = [array]::IndexOf($Arguments, "-File")
    if ($FileIndex -lt 0 -or $FileIndex -ge ($Arguments.Count - 1)) {
        $Errors.Add("nextCommandSpec.arguments must include -File followed by scriptPath.")
    } elseif ($ResolvedScriptPath -and -not (Test-SameFullPath -Left $Arguments[$FileIndex + 1] -Right $ResolvedScriptPath)) {
        $Errors.Add("nextCommandSpec.arguments -File target must match scriptPath.")
    }

    foreach ($BooleanCommandSpecField in $BooleanCommandSpecFields) {
        if ($BooleanCommandSpecField -notin $SpecPropertyNames) {
            $Errors.Add("nextCommandSpec.$BooleanCommandSpecField is missing.")
        } elseif (-not ($Spec.$BooleanCommandSpecField -is [bool])) {
            $Errors.Add("nextCommandSpec.$BooleanCommandSpecField must be boolean.")
        }
    }

    return @($Errors)
}

function Get-SafetyBlocks {
    param([object]$Spec)

    $Blocks = [System.Collections.Generic.List[object]]::new()
    if ([bool]$Spec.requiresNetwork -and -not $AllowNetwork) {
        $Blocks.Add([pscustomobject]@{
            field = "requiresNetwork"
            allowSwitch = "-AllowNetwork"
            reason = "requiresNetwork=true; rerun with -AllowNetwork"
        })
    }
    if ([bool]$Spec.mayInstallSoftware -and -not $AllowInstallSoftwareEffective) {
        $Blocks.Add([pscustomobject]@{
            field = "mayInstallSoftware"
            allowSwitch = "-AllowInstallSoftware"
            reason = "mayInstallSoftware=true; rerun with -AllowInstallSoftware"
        })
    }
    if ([bool]$Spec.mayDownloadLargeFiles -and -not $AllowDownloadLargeFilesEffective) {
        $Blocks.Add([pscustomobject]@{
            field = "mayDownloadLargeFiles"
            allowSwitch = "-AllowDownloadLargeFiles"
            reason = "mayDownloadLargeFiles=true; rerun with -AllowDownloadLargeFiles"
        })
    }
    if ([bool]$Spec.mayModifyVectorworksUserPlugins -and -not $AllowModifyVectorworksUserPluginsEffective) {
        $Blocks.Add([pscustomobject]@{
            field = "mayModifyVectorworksUserPlugins"
            allowSwitch = "-AllowModifyVectorworksUserPlugins"
            reason = "mayModifyVectorworksUserPlugins=true; rerun with -AllowModifyVectorworksUserPlugins"
        })
    }
    if ([bool]$Spec.requiresVectorworksRestartBeforeRun -and -not $AllowVectorworksRestartStep) {
        $Blocks.Add([pscustomobject]@{
            field = "requiresVectorworksRestartBeforeRun"
            allowSwitch = "-AllowVectorworksRestartStep"
            reason = "requiresVectorworksRestartBeforeRun=true; restart/load Vectorworks as instructed, then rerun with -AllowVectorworksRestartStep"
        })
    }
    if ([bool]$Spec.mayRequireReboot -and -not $AllowRebootRisk) {
        $Blocks.Add([pscustomobject]@{
            field = "mayRequireReboot"
            allowSwitch = "-AllowRebootRisk"
            reason = "mayRequireReboot=true; rerun with -AllowRebootRisk"
        })
    }
    return @($Blocks)
}

function New-StepRecord {
    param(
        [int]$Index,
        [object]$DoctorReport,
        [object]$Spec
    )
    $Arguments = if ($Spec -and $Spec.PSObject.Properties.Name -contains "arguments") {
        @($Spec.arguments | ForEach-Object { [string]$_ })
    } else {
        @()
    }
    return [ordered]@{
        index = $Index
        stage = [string]$Spec.stage
        nextCommand = [string]$DoctorReport.nextCommand
        nextCommandReason = [string]$DoctorReport.nextCommandReason
        executable = [string]$Spec.executable
        arguments = $Arguments
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
        safetyBlocks = @()
        missingAllowFlags = @()
        validationErrors = @()
        blockedReasons = @()
        plannedOnly = $false
        executed = $false
        exitCode = $null
        output = ""
        stopReason = ""
    }
}

function Join-ProcessArguments {
    param([string[]]$ArgumentList)

    $Quoted = foreach ($Argument in $ArgumentList) {
        if ($null -eq $Argument) {
            '""'
            continue
        }
        if ($Argument -notmatch '[\s"]') {
            $Argument
            continue
        }

        $Builder = [System.Text.StringBuilder]::new()
        [void]$Builder.Append('"')
        $Backslashes = 0
        foreach ($Character in $Argument.ToCharArray()) {
            if ($Character -eq '\') {
                $Backslashes += 1
                continue
            }
            if ($Character -eq '"') {
                if ($Backslashes -gt 0) {
                    [void]$Builder.Append(('\' * ($Backslashes * 2 + 1)))
                    $Backslashes = 0
                } else {
                    [void]$Builder.Append('\')
                }
                [void]$Builder.Append('"')
                continue
            }
            if ($Backslashes -gt 0) {
                [void]$Builder.Append(('\' * $Backslashes))
                $Backslashes = 0
            }
            [void]$Builder.Append($Character)
        }
        if ($Backslashes -gt 0) {
            [void]$Builder.Append(('\' * ($Backslashes * 2)))
        }
        [void]$Builder.Append('"')
        $Builder.ToString()
    }

    return ($Quoted -join " ")
}

function Invoke-CommandSpec {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )

    $ProcessInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $ProcessInfo.FileName = $Executable
    $ProcessInfo.Arguments = Join-ProcessArguments -ArgumentList $Arguments
    $ProcessInfo.WorkingDirectory = $WorkingDirectory
    $ProcessInfo.UseShellExecute = $false
    $ProcessInfo.RedirectStandardOutput = $true
    $ProcessInfo.RedirectStandardError = $true

    $Process = [System.Diagnostics.Process]::new()
    $Process.StartInfo = $ProcessInfo
    [void]$Process.Start()
    $Stdout = $Process.StandardOutput.ReadToEnd()
    $Stderr = $Process.StandardError.ReadToEnd()
    $Process.WaitForExit()
    $CommandExitCode = $Process.ExitCode

    $OutputParts = @()
    if (-not [string]::IsNullOrEmpty($Stderr)) { $OutputParts += $Stderr.TrimEnd() }
    if (-not [string]::IsNullOrEmpty($Stdout)) { $OutputParts += $Stdout.TrimEnd() }
    $Output = if ($OutputParts.Count -gt 0) { $OutputParts -join [Environment]::NewLine } else { "" }

    return [pscustomobject]@{
        output = [string]$Output
        exitCode = [int]$CommandExitCode
    }
}

$Steps = [System.Collections.Generic.List[object]]::new()
$Blocked = $false
$Failed = $false
$ExitCode = 0
$StopReason = ""
$Status = "not_started"
$MissingAllowFlags = @()
$ValidationErrors = @()

for ($Index = 1; $Index -le $MaxSteps; $Index++) {
    $DoctorReport = Invoke-NativeBridgeDoctor
    $Spec = $DoctorReport.nextCommandSpec
    $Step = New-StepRecord -Index $Index -DoctorReport $DoctorReport -Spec $Spec
    $StepValidationErrors = @(Test-NativeCommandSpec -DoctorReport $DoctorReport -Spec $Spec)
    if ($StepValidationErrors.Count -gt 0) {
        $Step.validationErrors = @($StepValidationErrors)
        $Step.stopReason = "invalid nextCommandSpec"
        $Steps.Add([pscustomobject]$Step)
        $ValidationErrors = @($StepValidationErrors)
        $Failed = $true
        $ExitCode = 3
        $StopReason = $Step.stopReason
        $Status = "invalid_spec"
        break
    }

    $SafetyBlocks = @(Get-SafetyBlocks -Spec $Spec)
    $BlockReasons = @($SafetyBlocks | ForEach-Object { [string]$_.reason })
    $Step.safetyBlocks = @($SafetyBlocks)
    $Step.blockedReasons = @($BlockReasons)
    $Step.missingAllowFlags = @($SafetyBlocks | ForEach-Object { [string]$_.allowSwitch } | Sort-Object -Unique)
    $MissingAllowFlags = @($Step.missingAllowFlags)
    if ($PlanOnly) {
        $Step.plannedOnly = $true
        $Step.stopReason = "plan only"
        $Steps.Add([pscustomobject]$Step)
        $StopReason = $Step.stopReason
        $Status = "plan_only"
        break
    }

    if ($BlockReasons.Count -gt 0) {
        $Step.stopReason = "blocked by safety flags"
        $Steps.Add([pscustomobject]$Step)
        $Blocked = $true
        $ExitCode = 2
        $StopReason = $Step.stopReason
        $Status = "blocked_by_safety_flag"
        break
    }

    $CommandArguments = @($Spec.arguments | ForEach-Object { [string]$_ })
    $WorkingDirectory = [string]$Spec.workingDirectory

    $CommandResult = Invoke-CommandSpec -Executable ([string]$Spec.executable) -Arguments $CommandArguments -WorkingDirectory $WorkingDirectory
    $CommandOutput = $CommandResult.output
    $CommandExitCode = $CommandResult.exitCode

    $Step.executed = $true
    $Step.exitCode = $CommandExitCode
    $Step.output = $CommandOutput
    if ($CommandExitCode -ne 0) {
        $Step.stopReason = "child command failed"
        $Steps.Add([pscustomobject]$Step)
        $Failed = $true
        $ExitCode = if ($CommandExitCode) { $CommandExitCode } else { 1 }
        $StopReason = $Step.stopReason
        $Status = "child_failed"
        break
    }

    if ([bool]$Spec.isDryRun) {
        $Step.stopReason = "dry-run command executed; not escalating to a mutating command automatically"
        $Steps.Add([pscustomobject]$Step)
        $StopReason = $Step.stopReason
        $Status = "dry_run_executed"
        break
    }
    if (-not [bool]$Spec.rerunDoctorAfter) {
        $Step.stopReason = "command executed; doctor rerun not requested"
        $Steps.Add([pscustomobject]$Step)
        $StopReason = $Step.stopReason
        $Status = "completed"
        break
    }
    if ($Index -eq $MaxSteps) {
        $Step.stopReason = "max steps reached after executing command"
        $Steps.Add([pscustomobject]$Step)
        $StopReason = $Step.stopReason
        $Status = "max_steps_reached"
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
    status = $Status
    blocked = [bool]$Blocked
    failed = [bool]$Failed
    exitCode = $ExitCode
    stopReason = $StopReason
    missingAllowFlags = @($MissingAllowFlags)
    validationErrors = @($ValidationErrors)
    steps = @($Steps)
}

if ($Json) {
    $Result | ConvertTo-Json -Depth 12
} else {
    Write-Host "Vectorworks native bridge next-step runner"
    Write-Host "Status: $($Result.status)"
    Write-Host "Blocked: $($Result.blocked)"
    Write-Host "Failed: $($Result.failed)"
    Write-Host "Stop reason: $($Result.stopReason)"
    foreach ($Step in $Result.steps) {
        Write-Host ""
        Write-Host ("Step {0}: {1}" -f $Step.index, $Step.stage)
        Write-Host ("Reason: {0}" -f $Step.nextCommandReason)
        if ($Step.validationErrors.Count -gt 0) {
            Write-Host "Validation errors:"
            foreach ($ValidationError in $Step.validationErrors) {
                Write-Host "- $ValidationError"
            }
        } elseif ($Step.blockedReasons.Count -gt 0) {
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
