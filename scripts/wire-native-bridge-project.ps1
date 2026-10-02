[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [string]$VectorworksVersion = "2024",
    [string]$WorktreeRoot = "",
    [string]$ProjectPath = "",
    [string]$SourceDir = "",
    [string]$FiltersPath = "",
    [switch]$CheckOnly,
    [switch]$Json
)

# 1.0: the native bridge build project is version controlled at
# native_bridge\project\VectorworksMCPBridge.vcxproj and compiles the sources in
# native_bridge\src directly. There is nothing to wire into an SDK sample copy
# any more; this shim only reports the project state for older callers.

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Project = Join-Path $RepoRoot "native_bridge\project\VectorworksMCPBridge.vcxproj"
$Exists = Test-Path -LiteralPath $Project
$Report = [ordered]@{
    vectorworksVersion = $VectorworksVersion
    projectPath = $Project
    sourceDir = (Join-Path $RepoRoot "native_bridge\src")
    projectWired = $Exists
    missingProjectItems = @()
    checkOnly = [bool]$CheckOnly
    projectChanged = $false
    note = "native_bridge\project is version controlled; build with scripts\build-native-bridge.ps1"
}
if ($Json) {
    $Report | ConvertTo-Json -Depth 4
} else {
    Write-Host "Native bridge project: $Project ($(if ($Exists) { 'present' } else { 'MISSING' }))"
    Write-Host "Build with: scripts\build-native-bridge.ps1 -VectorworksVersion $VectorworksVersion"
}
if (-not $Exists) { exit 2 }
exit 0
