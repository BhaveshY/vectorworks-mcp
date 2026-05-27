[CmdletBinding(SupportsShouldProcess=$true)]
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
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PrereqPath = Join-Path $PSScriptRoot "check-native-bridge-prereqs.ps1"
$PreparePath = Join-Path $PSScriptRoot "prepare-native-bridge-source.ps1"
$BuildPath = Join-Path $PSScriptRoot "build-native-bridge.ps1"
$SmokePath = Join-Path $PSScriptRoot "smoke-native-bridge.ps1"
$WorktreeRootWasExplicit = -not [string]::IsNullOrWhiteSpace($WorktreeRoot)
if (-not $WorktreeRoot) {
    $WorktreeRoot = Join-Path $RepoRoot "native_bridge\worktree\SDKExamples"
}
$BridgeSourceDir = Join-Path $WorktreeRoot "Examples$VectorworksVersion\VectorworksMCPBridge"
$ScaffoldDestinationDir = Join-Path $BridgeSourceDir "Source\VectorworksMCPBridge"
$RequiredScaffoldFiles = @(
    "BridgeProtocol.hpp",
    "BridgeProtocol.cpp",
    "BridgeDispatcher.hpp",
    "CadRequestQueue.hpp",
    "VectorworksMCPBridge.cpp"
)
$BuiltArtifactWasExplicit = -not [string]::IsNullOrWhiteSpace($BuiltArtifact)
$SdkDirWasExplicit = -not [string]::IsNullOrWhiteSpace($SdkDir)
$SdkExamplesDirWasExplicit = -not [string]::IsNullOrWhiteSpace($SdkExamplesDir)
$InstallDirWasExplicit = -not [string]::IsNullOrWhiteSpace($InstallDir)
$ConfigurationWasExplicit = $PSBoundParameters.ContainsKey("Configuration")

if (-not $InstallDir) {
    if (-not $env:APPDATA) {
        throw "APPDATA is not set. Pass -InstallDir to the Vectorworks user Plug-ins folder."
    }
    $InstallDir = Join-Path $env:APPDATA "Nemetschek\Vectorworks\$VectorworksVersion\Plug-ins"
}

function Get-FirstFile {
    param(
        [string]$Root,
        [string[]]$Patterns
    )
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return ""
    }
    foreach ($Pattern in $Patterns) {
        $Match = Get-ChildItem -LiteralPath $Root -Recurse -File -Filter $Pattern -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch '\\(Source|include|SDKLib|ThirdPartySource)\\' } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($Match) { return $Match.FullName }
    }
    return ""
}

function Add-NextAction {
    param(
        [System.Collections.Generic.List[string]]$Actions,
        [string]$Action
    )
    if ($Action -and -not $Actions.Contains($Action)) {
        $Actions.Add($Action)
    }
}

function Quote-PowerShellArgument {
    param([string]$Value)
    return "'$($Value -replace "'", "''")'"
}

function Format-PowerShellArgument {
    param([string]$Value)
    if ($Value -match '^[A-Za-z0-9_./:\\-]+$') {
        return $Value
    }
    return (Quote-PowerShellArgument $Value)
}

function New-RepoScriptCommand {
    param(
        [string]$ScriptName,
        [string[]]$Arguments = @()
    )
    $ScriptPath = Join-Path $PSScriptRoot $ScriptName
    $Parts = [System.Collections.Generic.List[string]]::new()
    foreach ($Part in @("powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ScriptPath)) {
        $Parts.Add((Format-PowerShellArgument $Part))
    }
    foreach ($Argument in $Arguments) {
        $Parts.Add((Format-PowerShellArgument $Argument))
    }
    return ($Parts -join " ")
}

function New-RepoScriptCommandSpec {
    param(
        [string]$ScriptName,
        [string[]]$Arguments = @(),
        [string]$Stage,
        [bool]$RequiresNetwork = $false,
        [bool]$MayInstallSoftware = $false,
        [bool]$MayDownloadLargeFiles = $false,
        [bool]$MayModifyVectorworksUserPlugins = $false,
        [bool]$RequiresVectorworksRestartBeforeRun = $false,
        [bool]$MayRequireReboot = $false,
        [bool]$IsDryRun = $false,
        [bool]$RerunDoctorAfter = $false
    )
    $ScriptPath = Join-Path $PSScriptRoot $ScriptName
    $CommandArguments = @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ScriptPath) + @($Arguments)
    return [pscustomobject]@{
        stage = $Stage
        executable = "powershell.exe"
        arguments = @($CommandArguments)
        workingDirectory = $RepoRoot
        scriptPath = $ScriptPath
        command = New-RepoScriptCommand -ScriptName $ScriptName -Arguments $Arguments
        requiresNetwork = [bool]$RequiresNetwork
        mayInstallSoftware = [bool]$MayInstallSoftware
        mayDownloadLargeFiles = [bool]$MayDownloadLargeFiles
        mayModifyVectorworksUserPlugins = [bool]$MayModifyVectorworksUserPlugins
        requiresVectorworksRestartBeforeRun = [bool]$RequiresVectorworksRestartBeforeRun
        mayRequireReboot = [bool]$MayRequireReboot
        isDryRun = [bool]$IsDryRun
        rerunDoctorAfter = [bool]$RerunDoctorAfter
    }
}

function Set-NextCommandPlan {
    param(
        [string]$ScriptName,
        [string[]]$Arguments = @(),
        [string]$Stage,
        [string]$Reason,
        [bool]$RequiresNetwork = $false,
        [bool]$MayInstallSoftware = $false,
        [bool]$MayDownloadLargeFiles = $false,
        [bool]$MayModifyVectorworksUserPlugins = $false,
        [bool]$RequiresVectorworksRestartBeforeRun = $false,
        [bool]$MayRequireReboot = $false,
        [bool]$IsDryRun = $false,
        [bool]$RerunDoctorAfter = $false
    )
    $script:NextCommandSpec = New-RepoScriptCommandSpec `
        -ScriptName $ScriptName `
        -Arguments $Arguments `
        -Stage $Stage `
        -RequiresNetwork $RequiresNetwork `
        -MayInstallSoftware $MayInstallSoftware `
        -MayDownloadLargeFiles $MayDownloadLargeFiles `
        -MayModifyVectorworksUserPlugins $MayModifyVectorworksUserPlugins `
        -RequiresVectorworksRestartBeforeRun $RequiresVectorworksRestartBeforeRun `
        -MayRequireReboot $MayRequireReboot `
        -IsDryRun $IsDryRun `
        -RerunDoctorAfter $RerunDoctorAfter
    $script:NextCommand = $script:NextCommandSpec.command
    $script:NextCommandReason = $Reason
}

function Add-NamedCommandArgument {
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

function Add-SwitchCommandArgument {
    param(
        [System.Collections.Generic.List[string]]$Arguments,
        [string]$Name,
        [bool]$Present
    )
    if ($Present) {
        $Arguments.Add("-$Name")
    }
}

if (-not (Test-Path -LiteralPath $PrereqPath)) {
    throw "Native prerequisite checker not found at $PrereqPath"
}

$PrereqArgs = @("-VectorworksVersion", $VectorworksVersion, "-Advisory", "-Json")
if ($SdkDirWasExplicit) { $PrereqArgs += @("-SdkDir", $SdkDir) }
$PrereqRaw = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $PrereqPath @PrereqArgs | Out-String
$Prereqs = $PrereqRaw | ConvertFrom-Json
$SourcePrepared = Test-Path -LiteralPath $BridgeSourceDir -PathType Container
$SolutionPath = Get-FirstFile -Root $BridgeSourceDir -Patterns @("*$VectorworksVersion.sln")
$MissingScaffoldFiles = @($RequiredScaffoldFiles | Where-Object {
    -not (Test-Path -LiteralPath (Join-Path $ScaffoldDestinationDir $_) -PathType Leaf)
})
$ScaffoldCopied = $MissingScaffoldFiles.Count -eq 0

if ($BuiltArtifactWasExplicit) {
    if (-not (Test-Path -LiteralPath $BuiltArtifact -PathType Leaf)) {
        throw "Built artifact was not found at $BuiltArtifact"
    }
    $BuiltArtifact = (Resolve-Path -LiteralPath $BuiltArtifact).Path
} else {
    $BuiltArtifact = ""
}
$BuiltArtifactCandidate = Get-FirstFile -Root $BridgeSourceDir -Patterns @("*.vwlibrary", "*.vsm", "*.vst", "*.vso", "*.dll")
$InstallArtifact = if ($BuiltArtifactWasExplicit) { $BuiltArtifact } else { "" }

$InstallDestination = ""
$InstallPerformed = $false
$InstallWhatIf = [bool]$WhatIfPreference
$InstalledPath = ""
if ($Install) {
    if (-not $BuiltArtifactWasExplicit) {
        $CandidateHint = if ($BuiltArtifactCandidate) { " Candidate found: $BuiltArtifactCandidate" } else { "" }
        throw "Pass an explicit -BuiltArtifact before using -Install; auto-discovered artifacts are reported as candidates only and are not installed implicitly.$CandidateHint"
    }
    $InstallDestination = Join-Path $InstallDir (Split-Path -Leaf $InstallArtifact)
    if (-not $WhatIfPreference -and $PSCmdlet.ShouldProcess($InstallDestination, "Install native Vectorworks bridge artifact")) {
        New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
        Copy-Item -LiteralPath $InstallArtifact -Destination $InstallDestination -Force
        $InstalledPath = (Resolve-Path -LiteralPath $InstallDestination).Path
        $InstallPerformed = $true
    }
}

$NextActions = [System.Collections.Generic.List[string]]::new()
if (-not $Prereqs.ready) {
    Add-NextAction $NextActions "Run scripts\bootstrap-native-bridge.ps1 -InstallVisualStudioBuildTools -DownloadSdk -CloneSdkExamples -PrepareSource"
}
if (-not $SourcePrepared) {
    Add-NextAction $NextActions "Run scripts\prepare-native-bridge-source.ps1 -CloneSdkExamples"
}
if ($SourcePrepared -and -not $SolutionPath) {
    Add-NextAction $NextActions "Recreate native_bridge\worktree with scripts\prepare-native-bridge-source.ps1 -CloneSdkExamples -Force"
}
if ($SourcePrepared -and $SolutionPath -and -not $BuiltArtifact -and -not $BuiltArtifactCandidate) {
    Add-NextAction $NextActions "Run scripts\build-native-bridge.ps1 -VectorworksVersion $VectorworksVersion"
}
if ($SourcePrepared -and $SolutionPath -and -not $ScaffoldCopied) {
    $ForceHint = if ($MissingScaffoldFiles.Count -lt $RequiredScaffoldFiles.Count) { " -Force" } else { "" }
    Add-NextAction $NextActions "After the unmodified SDK example builds, run scripts\copy-native-bridge-scaffold.ps1 -VectorworksVersion $VectorworksVersion$ForceHint"
}
$InstallCandidate = if ($BuiltArtifact) { $BuiltArtifact } else { $BuiltArtifactCandidate }
if ($InstallCandidate -and -not $Install) {
    Add-NextAction $NextActions "Dry-run install: scripts\doctor-native-bridge.ps1 -BuiltArtifact `"$InstallCandidate`" -Install -WhatIf"
    Add-NextAction $NextActions "Install when ready: scripts\doctor-native-bridge.ps1 -BuiltArtifact `"$InstallCandidate`" -Install"
}
if ($Install -and $InstallArtifact -and -not $InstallPerformed) {
    Add-NextAction $NextActions "Dry-run only: rerun scripts\doctor-native-bridge.ps1 -BuiltArtifact `"$InstallArtifact`" -Install without -WhatIf to copy the bridge artifact."
}
if ($InstallPerformed) {
    Add-NextAction $NextActions "Restart Vectorworks $VectorworksVersion, enable/load the native bridge plug-in, then run scripts\smoke-native-bridge.ps1 -Phase 0 -Stop -Json first."
}
if ($NextActions.Count -eq 0) {
    Add-NextAction $NextActions "Complete native bridge source, build it, then rerun this doctor with -BuiltArtifact."
}

$NextCommand = ""
$NextCommandReason = ""
$NextCommandSpec = $null
$ScaffoldAbsent = $MissingScaffoldFiles.Count -eq $RequiredScaffoldFiles.Count
$ScaffoldPartiallyCopied = $MissingScaffoldFiles.Count -gt 0 -and $MissingScaffoldFiles.Count -lt $RequiredScaffoldFiles.Count

if ($InstallPerformed) {
    $SmokeArgs = [System.Collections.Generic.List[string]]::new()
    Add-NamedCommandArgument $SmokeArgs "Phase" "0"
    Add-SwitchCommandArgument $SmokeArgs "Stop" $true
    Add-SwitchCommandArgument $SmokeArgs "Json" $true
    Set-NextCommandPlan -ScriptName "smoke-native-bridge.ps1" -Arguments $SmokeArgs -Stage "smoke-phase-0" -Reason "The native bridge artifact was installed. Restart Vectorworks, load the plug-in, then run the phase-0 transport smoke." -RequiresVectorworksRestartBeforeRun $true
} elseif ($Install -and $InstallArtifact -and -not $InstallPerformed) {
    $DoctorArgs = [System.Collections.Generic.List[string]]::new()
    Add-NamedCommandArgument $DoctorArgs "VectorworksVersion" $VectorworksVersion
    Add-NamedCommandArgument $DoctorArgs "BuiltArtifact" $InstallArtifact
    if ($InstallDirWasExplicit) { Add-NamedCommandArgument $DoctorArgs "InstallDir" $InstallDir }
    Add-SwitchCommandArgument $DoctorArgs "Install" $true
    Set-NextCommandPlan -ScriptName "doctor-native-bridge.ps1" -Arguments $DoctorArgs -Stage "install-native-artifact" -Reason "The install was only simulated. Rerun without -WhatIf to copy the bridge artifact." -MayModifyVectorworksUserPlugins $true -RerunDoctorAfter $true
} elseif ($InstallCandidate -and -not $Install) {
    $DoctorArgs = [System.Collections.Generic.List[string]]::new()
    Add-NamedCommandArgument $DoctorArgs "VectorworksVersion" $VectorworksVersion
    Add-NamedCommandArgument $DoctorArgs "BuiltArtifact" $InstallCandidate
    if ($InstallDirWasExplicit) { Add-NamedCommandArgument $DoctorArgs "InstallDir" $InstallDir }
    Add-SwitchCommandArgument $DoctorArgs "Install" $true
    Add-SwitchCommandArgument $DoctorArgs "WhatIf" $true
    Set-NextCommandPlan -ScriptName "doctor-native-bridge.ps1" -Arguments $DoctorArgs -Stage "dry-run-install-native-artifact" -Reason "A native artifact is available. Dry-run the install before copying it into the Vectorworks user Plug-ins folder." -IsDryRun $true -RerunDoctorAfter $true
} elseif ($ScaffoldPartiallyCopied) {
    $CopyArgs = [System.Collections.Generic.List[string]]::new()
    Add-NamedCommandArgument $CopyArgs "VectorworksVersion" $VectorworksVersion
    Add-SwitchCommandArgument $CopyArgs "Force" $true
    if ($WorktreeRootWasExplicit) { Add-NamedCommandArgument $CopyArgs "WorktreeRoot" $WorktreeRoot }
    Set-NextCommandPlan -ScriptName "copy-native-bridge-scaffold.ps1" -Arguments $CopyArgs -Stage "copy-native-scaffold" -Reason "The reviewed bridge scaffold is partially copied; force-copy it to restore a consistent source tree before building." -RerunDoctorAfter $true
} elseif (-not $Prereqs.ready) {
    $BootstrapArgs = [System.Collections.Generic.List[string]]::new()
    Add-NamedCommandArgument $BootstrapArgs "VectorworksVersion" $VectorworksVersion
    if ($SdkDirWasExplicit) { Add-NamedCommandArgument $BootstrapArgs "SdkDir" $SdkDir }
    if ($SdkExamplesDirWasExplicit) { Add-NamedCommandArgument $BootstrapArgs "SdkExamplesDir" $SdkExamplesDir }
    if ($WorktreeRootWasExplicit) { Add-NamedCommandArgument $BootstrapArgs "WorktreeRoot" $WorktreeRoot }
    if ($ConfigurationWasExplicit) { Add-NamedCommandArgument $BootstrapArgs "Configuration" $Configuration }
    Add-SwitchCommandArgument $BootstrapArgs "InstallVisualStudioBuildTools" $true
    Add-SwitchCommandArgument $BootstrapArgs "DownloadSdk" $true
    Add-SwitchCommandArgument $BootstrapArgs "CloneSdkExamples" $true
    Add-SwitchCommandArgument $BootstrapArgs "PrepareSource" $true
    Set-NextCommandPlan -ScriptName "bootstrap-native-bridge.ps1" -Arguments $BootstrapArgs -Stage "bootstrap-native-prereqs" -Reason "Native prerequisites are missing. Run the opt-in bootstrap, then rerun doctor-native-bridge.ps1 -Json after installer completion or reboot." -RequiresNetwork $true -MayInstallSoftware $true -MayDownloadLargeFiles $true -MayRequireReboot $true -RerunDoctorAfter $true
} elseif (-not $SourcePrepared) {
    $PrepareArgs = [System.Collections.Generic.List[string]]::new()
    Add-NamedCommandArgument $PrepareArgs "VectorworksVersion" $VectorworksVersion
    if ($SdkDirWasExplicit) { Add-NamedCommandArgument $PrepareArgs "SdkDir" $SdkDir }
    if ($SdkExamplesDirWasExplicit) { Add-NamedCommandArgument $PrepareArgs "SdkExamplesDir" $SdkExamplesDir }
    if ($WorktreeRootWasExplicit) { Add-NamedCommandArgument $PrepareArgs "WorktreeRoot" $WorktreeRoot }
    Add-SwitchCommandArgument $PrepareArgs "CloneSdkExamples" $true
    Set-NextCommandPlan -ScriptName "prepare-native-bridge-source.ps1" -Arguments $PrepareArgs -Stage "prepare-native-source" -Reason "The SDK example worktree is not prepared yet." -RequiresNetwork $true -RerunDoctorAfter $true
} elseif (-not $SolutionPath) {
    $PrepareArgs = [System.Collections.Generic.List[string]]::new()
    Add-NamedCommandArgument $PrepareArgs "VectorworksVersion" $VectorworksVersion
    if ($SdkDirWasExplicit) { Add-NamedCommandArgument $PrepareArgs "SdkDir" $SdkDir }
    if ($SdkExamplesDirWasExplicit) { Add-NamedCommandArgument $PrepareArgs "SdkExamplesDir" $SdkExamplesDir }
    if ($WorktreeRootWasExplicit) { Add-NamedCommandArgument $PrepareArgs "WorktreeRoot" $WorktreeRoot }
    Add-SwitchCommandArgument $PrepareArgs "CloneSdkExamples" $true
    Add-SwitchCommandArgument $PrepareArgs "Force" $true
    Set-NextCommandPlan -ScriptName "prepare-native-bridge-source.ps1" -Arguments $PrepareArgs -Stage "repair-native-source" -Reason "The native bridge source folder exists, but the expected Vectorworks solution was not found." -RequiresNetwork $true -RerunDoctorAfter $true
} elseif ($ScaffoldAbsent -and -not $BuiltArtifact -and -not $BuiltArtifactCandidate) {
    $BuildArgs = [System.Collections.Generic.List[string]]::new()
    Add-NamedCommandArgument $BuildArgs "VectorworksVersion" $VectorworksVersion
    if ($SdkDirWasExplicit) { Add-NamedCommandArgument $BuildArgs "SdkDir" $SdkDir }
    if ($WorktreeRootWasExplicit) { Add-NamedCommandArgument $BuildArgs "SourceDir" $WorktreeRoot }
    if ($ConfigurationWasExplicit) { Add-NamedCommandArgument $BuildArgs "Configuration" $Configuration }
    Set-NextCommandPlan -ScriptName "build-native-bridge.ps1" -Arguments $BuildArgs -Stage "build-unmodified-sdk-example" -Reason "The unmodified SDK example should build once before copying the reviewed bridge scaffold." -RerunDoctorAfter $true
} elseif (-not $ScaffoldCopied) {
    $CopyArgs = [System.Collections.Generic.List[string]]::new()
    Add-NamedCommandArgument $CopyArgs "VectorworksVersion" $VectorworksVersion
    if ($WorktreeRootWasExplicit) { Add-NamedCommandArgument $CopyArgs "WorktreeRoot" $WorktreeRoot }
    Set-NextCommandPlan -ScriptName "copy-native-bridge-scaffold.ps1" -Arguments $CopyArgs -Stage "copy-native-scaffold" -Reason "A native artifact candidate exists, so the next step is copying the reviewed bridge scaffold into the SDK example." -RerunDoctorAfter $true
} elseif (-not $BuiltArtifact -and -not $BuiltArtifactCandidate) {
    $BuildArgs = [System.Collections.Generic.List[string]]::new()
    Add-NamedCommandArgument $BuildArgs "VectorworksVersion" $VectorworksVersion
    if ($SdkDirWasExplicit) { Add-NamedCommandArgument $BuildArgs "SdkDir" $SdkDir }
    if ($WorktreeRootWasExplicit) { Add-NamedCommandArgument $BuildArgs "SourceDir" $WorktreeRoot }
    if ($ConfigurationWasExplicit) { Add-NamedCommandArgument $BuildArgs "Configuration" $Configuration }
    Set-NextCommandPlan -ScriptName "build-native-bridge.ps1" -Arguments $BuildArgs -Stage "build-native-bridge" -Reason "The bridge scaffold is copied; build the native bridge artifact next." -RerunDoctorAfter $true
} else {
    $DoctorArgs = [System.Collections.Generic.List[string]]::new()
    Add-NamedCommandArgument $DoctorArgs "VectorworksVersion" $VectorworksVersion
    if ($SdkDirWasExplicit) { Add-NamedCommandArgument $DoctorArgs "SdkDir" $SdkDir }
    if ($SdkExamplesDirWasExplicit) { Add-NamedCommandArgument $DoctorArgs "SdkExamplesDir" $SdkExamplesDir }
    if ($WorktreeRootWasExplicit) { Add-NamedCommandArgument $DoctorArgs "WorktreeRoot" $WorktreeRoot }
    if ($InstallDirWasExplicit) { Add-NamedCommandArgument $DoctorArgs "InstallDir" $InstallDir }
    if ($ConfigurationWasExplicit) { Add-NamedCommandArgument $DoctorArgs "Configuration" $Configuration }
    Add-SwitchCommandArgument $DoctorArgs "Json" $true
    Set-NextCommandPlan -ScriptName "doctor-native-bridge.ps1" -Arguments $DoctorArgs -Stage "rerun-native-doctor" -Reason "The current state needs another doctor pass after completing the source or build step."
}

$Report = [pscustomobject]@{
    vectorworksVersion = $VectorworksVersion
    repoRoot = $RepoRoot
    prereqsReady = [bool]$Prereqs.ready
    prereqs = $Prereqs
    worktreeRoot = $WorktreeRoot
    bridgeSourceDir = $BridgeSourceDir
    scaffoldDestinationDir = $ScaffoldDestinationDir
    scaffoldFiles = @($RequiredScaffoldFiles)
    missingScaffoldFiles = @($MissingScaffoldFiles)
    scaffoldCopied = [bool]$ScaffoldCopied
    sourcePrepared = [bool]$SourcePrepared
    solutionPath = $SolutionPath
    sdkDir = $SdkDir
    sdkExamplesDir = $SdkExamplesDir
    configuration = $Configuration
    builtArtifact = $BuiltArtifact
    builtArtifactWasExplicit = [bool]$BuiltArtifactWasExplicit
    builtArtifactCandidate = $BuiltArtifactCandidate
    installDir = $InstallDir
    installRequested = [bool]$Install
    installDestination = $InstallDestination
    installPerformed = [bool]$InstallPerformed
    installWhatIf = [bool]$InstallWhatIf
    installedPath = $InstalledPath
    nextCommand = $NextCommand
    nextCommandReason = $NextCommandReason
    nextCommandSpec = $NextCommandSpec
    helperScripts = [pscustomobject]@{
        prereq = $PrereqPath
        prepare = $PreparePath
        build = $BuildPath
        smoke = $SmokePath
    }
    nextActions = @($NextActions)
}

if ($Json) {
    $Report | ConvertTo-Json -Depth 12
} else {
    Write-Host "Vectorworks native bridge doctor ($VectorworksVersion)"
    Write-Host "Prerequisites ready: $($Report.prereqsReady)"
    Write-Host "Source prepared: $($Report.sourcePrepared)"
    Write-Host "Scaffold copied: $($Report.scaffoldCopied)"
    if ($MissingScaffoldFiles.Count -gt 0) {
        Write-Host "Missing scaffold files: $($MissingScaffoldFiles -join ', ')"
    }
    Write-Host "Solution: $(if ($SolutionPath) { $SolutionPath } else { 'not found' })"
    Write-Host "Built artifact: $(if ($BuiltArtifact) { $BuiltArtifact } else { 'not provided explicitly' })"
    if ($BuiltArtifactCandidate) {
        Write-Host "Auto-discovered candidate: $BuiltArtifactCandidate"
    }
    Write-Host "Install dir: $InstallDir"
    if ($InstallDestination) {
        Write-Host "Install destination: $InstallDestination"
        Write-Host "Install performed: $InstallPerformed"
        Write-Host "Install WhatIf: $InstallWhatIf"
    }
    if ($InstalledPath) {
        Write-Host "Installed path: $InstalledPath"
    }
    Write-Host ""
    Write-Host "Next command:"
    Write-Host ("- {0}" -f $Report.nextCommand)
    Write-Host "Reason: $($Report.nextCommandReason)"
    Write-Host ""
    Write-Host "Next action:"
    Write-Host ("- {0}" -f $NextActions[0])
}
