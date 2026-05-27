[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [string]$VectorworksVersion = "2024",
    [string]$BuiltArtifact = "",
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
$WorktreeRoot = Join-Path $RepoRoot "native_bridge\worktree\SDKExamples"
$BridgeSourceDir = Join-Path $WorktreeRoot "Examples$VectorworksVersion\VectorworksMCPBridge"
$ScaffoldDestinationDir = Join-Path $BridgeSourceDir "Source\VectorworksMCPBridge"

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

if (-not (Test-Path -LiteralPath $PrereqPath)) {
    throw "Native prerequisite checker not found at $PrereqPath"
}

$PrereqRaw = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $PrereqPath -VectorworksVersion $VectorworksVersion -Advisory -Json | Out-String
$Prereqs = $PrereqRaw | ConvertFrom-Json
$SourcePrepared = Test-Path -LiteralPath $BridgeSourceDir -PathType Container
$SolutionPath = Get-FirstFile -Root $BridgeSourceDir -Patterns @("*$VectorworksVersion.sln")
$ScaffoldCopied = Test-Path -LiteralPath (Join-Path $ScaffoldDestinationDir "BridgeProtocol.hpp") -PathType Leaf

if ($BuiltArtifact) {
    if (-not (Test-Path -LiteralPath $BuiltArtifact -PathType Leaf)) {
        throw "Built artifact was not found at $BuiltArtifact"
    }
    $BuiltArtifact = (Resolve-Path -LiteralPath $BuiltArtifact).Path
} else {
    $BuiltArtifact = Get-FirstFile -Root $BridgeSourceDir -Patterns @("*.vwlibrary", "*.vsm", "*.vst", "*.vso", "*.dll")
}

$InstalledPath = ""
if ($Install) {
    if (-not $BuiltArtifact) {
        throw "Pass -BuiltArtifact before using -Install, or build the native bridge first."
    }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $Destination = Join-Path $InstallDir (Split-Path -Leaf $BuiltArtifact)
    if ($PSCmdlet.ShouldProcess($Destination, "Install native Vectorworks bridge artifact")) {
        Copy-Item -LiteralPath $BuiltArtifact -Destination $Destination -Force
        $InstalledPath = (Resolve-Path -LiteralPath $Destination).Path
    } else {
        $InstalledPath = $Destination
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
if ($SourcePrepared -and $SolutionPath -and -not $BuiltArtifact) {
    Add-NextAction $NextActions "Run scripts\build-native-bridge.ps1 -VectorworksVersion $VectorworksVersion"
}
if ($SourcePrepared -and $SolutionPath -and -not $ScaffoldCopied) {
    Add-NextAction $NextActions "After the unmodified SDK example builds, run scripts\copy-native-bridge-scaffold.ps1 -VectorworksVersion $VectorworksVersion"
}
if ($BuiltArtifact -and -not $Install) {
    Add-NextAction $NextActions "Dry-run install: scripts\doctor-native-bridge.ps1 -BuiltArtifact `"$BuiltArtifact`" -Install -WhatIf"
    Add-NextAction $NextActions "Install when ready: scripts\doctor-native-bridge.ps1 -BuiltArtifact `"$BuiltArtifact`" -Install"
}
if ($InstalledPath) {
    Add-NextAction $NextActions "Restart Vectorworks $VectorworksVersion, enable/load the native bridge plug-in, then run scripts\smoke-native-bridge.ps1 -Json"
}
if ($NextActions.Count -eq 0) {
    Add-NextAction $NextActions "Complete native bridge source, build it, then rerun this doctor with -BuiltArtifact."
}

$Report = [pscustomobject]@{
    vectorworksVersion = $VectorworksVersion
    repoRoot = $RepoRoot
    prereqsReady = [bool]$Prereqs.ready
    prereqs = $Prereqs
    worktreeRoot = $WorktreeRoot
    bridgeSourceDir = $BridgeSourceDir
    scaffoldDestinationDir = $ScaffoldDestinationDir
    scaffoldCopied = [bool]$ScaffoldCopied
    sourcePrepared = [bool]$SourcePrepared
    solutionPath = $SolutionPath
    builtArtifact = $BuiltArtifact
    installDir = $InstallDir
    installRequested = [bool]$Install
    installedPath = $InstalledPath
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
    Write-Host "Solution: $(if ($SolutionPath) { $SolutionPath } else { 'not found' })"
    Write-Host "Built artifact: $(if ($BuiltArtifact) { $BuiltArtifact } else { 'not found' })"
    Write-Host "Install dir: $InstallDir"
    if ($InstalledPath) {
        Write-Host "Installed path: $InstalledPath"
    }
    Write-Host ""
    Write-Host "Next action:"
    Write-Host ("- {0}" -f $NextActions[0])
}
