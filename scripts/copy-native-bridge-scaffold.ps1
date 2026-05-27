[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$WorktreeRoot = "",
    [string]$DestinationDir = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ScaffoldRoot = Join-Path $RepoRoot "native_bridge\src"
if (-not $WorktreeRoot) {
    $WorktreeRoot = Join-Path $RepoRoot "native_bridge\worktree\SDKExamples"
}
if (-not $DestinationDir) {
    $DestinationDir = Join-Path $WorktreeRoot "Examples$VectorworksVersion\VectorworksMCPBridge\Source\VectorworksMCPBridge"
}

if (-not (Test-Path -LiteralPath $ScaffoldRoot -PathType Container)) {
    throw "Native bridge scaffold folder was not found at $ScaffoldRoot"
}

$SourceParent = Split-Path -Parent $DestinationDir
if (-not (Test-Path -LiteralPath $SourceParent -PathType Container)) {
    throw "Native bridge worktree Source folder was not found at $SourceParent. Run scripts\prepare-native-bridge-source.ps1 first."
}

$Files = @(
    "BridgeProtocol.hpp",
    "BridgeProtocol.cpp",
    "BridgeDispatcher.hpp",
    "CadRequestQueue.hpp",
    "VectorworksMCPBridge.cpp"
)

New-Item -ItemType Directory -Force -Path $DestinationDir | Out-Null
foreach ($FileName in $Files) {
    $Source = Join-Path $ScaffoldRoot $FileName
    $Destination = Join-Path $DestinationDir $FileName
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Native bridge scaffold file was not found at $Source"
    }
    if ((Test-Path -LiteralPath $Destination) -and -not $Force) {
        throw "Refusing to overwrite $Destination. Pass -Force to refresh scaffold files."
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force:$Force
}

Write-Host "Copied native bridge scaffold to:"
Write-Host $DestinationDir
Write-Host ""
Write-Host "Next: add these files to the SDK project, wire SDK entry points, build, then run scripts\smoke-native-bridge.ps1 -Phase 0 -Stop -Json first."
