[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [string]$VectorworksVersion = "2024",
    [string]$BuiltArtifact = "",
    [string]$WorktreeRoot = "",
    [string]$InstallDir = "",
    [switch]$Install,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PrereqPath = Join-Path $PSScriptRoot "check-native-bridge-prereqs.ps1"
$PreparePath = Join-Path $PSScriptRoot "prepare-native-bridge-source.ps1"
$BuildPath = Join-Path $PSScriptRoot "build-native-bridge.ps1"
$SmokePath = Join-Path $PSScriptRoot "smoke-native-bridge.ps1"
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

if (-not (Test-Path -LiteralPath $PrereqPath)) {
    throw "Native prerequisite checker not found at $PrereqPath"
}

$PrereqRaw = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $PrereqPath -VectorworksVersion $VectorworksVersion -Advisory -Json | Out-String
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
$ScaffoldAbsent = $MissingScaffoldFiles.Count -eq $RequiredScaffoldFiles.Count
$ScaffoldPartiallyCopied = $MissingScaffoldFiles.Count -gt 0 -and $MissingScaffoldFiles.Count -lt $RequiredScaffoldFiles.Count

if ($InstallPerformed) {
    $NextCommand = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke-native-bridge.ps1 -Phase 0 -Stop -Json"
    $NextCommandReason = "The native bridge artifact was installed. Restart Vectorworks, load the plug-in, then run the phase-0 transport smoke."
} elseif ($Install -and $InstallArtifact -and -not $InstallPerformed) {
    $NextCommand = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -BuiltArtifact $(Quote-PowerShellArgument $InstallArtifact) -Install"
    $NextCommandReason = "The install was only simulated. Rerun without -WhatIf to copy the bridge artifact."
} elseif ($InstallCandidate -and -not $Install) {
    $NextCommand = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -BuiltArtifact $(Quote-PowerShellArgument $InstallCandidate) -Install -WhatIf"
    $NextCommandReason = "A native artifact is available. Dry-run the install before copying it into the Vectorworks user Plug-ins folder."
} elseif ($ScaffoldPartiallyCopied) {
    $NextCommand = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\copy-native-bridge-scaffold.ps1 -VectorworksVersion $VectorworksVersion -Force"
    $NextCommandReason = "The reviewed bridge scaffold is partially copied; force-copy it to restore a consistent source tree before building."
} elseif (-not $Prereqs.ready) {
    $NextCommand = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-native-bridge.ps1 -VectorworksVersion $VectorworksVersion -InstallVisualStudioBuildTools -DownloadSdk -CloneSdkExamples -PrepareSource"
    $NextCommandReason = "Native prerequisites are missing. Run the opt-in bootstrap, then rerun doctor-native-bridge.ps1 -Json after installer completion or reboot."
} elseif (-not $SourcePrepared) {
    $NextCommand = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\prepare-native-bridge-source.ps1 -VectorworksVersion $VectorworksVersion -CloneSdkExamples"
    $NextCommandReason = "The SDK example worktree is not prepared yet."
} elseif (-not $SolutionPath) {
    $NextCommand = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\prepare-native-bridge-source.ps1 -VectorworksVersion $VectorworksVersion -CloneSdkExamples -Force"
    $NextCommandReason = "The native bridge source folder exists, but the expected Vectorworks solution was not found."
} elseif ($ScaffoldAbsent -and -not $BuiltArtifact -and -not $BuiltArtifactCandidate) {
    $NextCommand = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-native-bridge.ps1 -VectorworksVersion $VectorworksVersion"
    $NextCommandReason = "The unmodified SDK example should build once before copying the reviewed bridge scaffold."
} elseif (-not $ScaffoldCopied) {
    $NextCommand = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\copy-native-bridge-scaffold.ps1 -VectorworksVersion $VectorworksVersion"
    $NextCommandReason = "A native artifact candidate exists, so the next step is copying the reviewed bridge scaffold into the SDK example."
} elseif (-not $BuiltArtifact -and -not $BuiltArtifactCandidate) {
    $NextCommand = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-native-bridge.ps1 -VectorworksVersion $VectorworksVersion"
    $NextCommandReason = "The bridge scaffold is copied; build the native bridge artifact next."
} else {
    $NextCommand = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\doctor-native-bridge.ps1 -Json"
    $NextCommandReason = "The current state needs another doctor pass after completing the source or build step."
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
