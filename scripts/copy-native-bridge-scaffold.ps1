[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$WorktreeRoot = "",
    [string]$DestinationDir = "",
    [switch]$Force
)

# 1.0: native_bridge\project\VectorworksMCPBridge.vcxproj compiles native_bridge\src
# in place, so sources are no longer copied into an SDK sample worktree.
# Kept as a no-op for older automation.

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Write-Host "Nothing to copy: the build project native_bridge\project compiles native_bridge\src directly."
Write-Host "Build with: $(Join-Path $RepoRoot 'scripts\build-native-bridge.ps1') -VectorworksVersion $VectorworksVersion"
exit 0
