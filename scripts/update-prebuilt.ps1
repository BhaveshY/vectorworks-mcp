<#
.SYNOPSIS
  Copies the Release builds of the native bridge into native\<version>\ (tracked in git)
  so a plain clone installs the plug-in without Visual Studio or the SDK.

.DESCRIPTION
  Run after building with scripts\build-native-bridge.ps1 and live-testing the build.
  Refuses when native_bridge\src has uncommitted changes (the prebuilt must match a commit).
  Writes native\PREBUILT.json: per version the hashes, build id and the git tree id of
  native_bridge\src it was built from (install.ps1 / preflight.ps1 warn when it is older).

  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\update-prebuilt.ps1 [-Versions 2024,2025] [-Tested 2024]
#>
param(
    [string[]]$Versions = @("2024", "2025"),
    # Versions that passed the live acceptance with this build (recorded, shown by preflight).
    [string[]]$Tested = @()
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "install-common.ps1")

$dirty = (& git -C $Repo status --porcelain -- native_bridge/src native_bridge/project) -join "`n"
if ($dirty.Trim()) { throw "native_bridge has uncommitted changes; commit them and rebuild first:`n$dirty" }
$tree = (& git -C $Repo rev-parse HEAD:native_bridge/src).Trim()

$manifestPath = Join-Path $Repo "native\PREBUILT.json"
$manifest = [ordered]@{}
if (Test-Path -LiteralPath $manifestPath) {
    $old = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    foreach ($p in $old.PSObject.Properties) { $manifest[$p.Name] = $p.Value }
}
foreach ($ver in $Versions) {
    $out = Join-Path $Repo "native_bridge\project\out\$ver\Release"
    $info = Get-Content -LiteralPath (Join-Path $out "build-info.json") -Raw | ConvertFrom-Json
    if ($info.build_id -match '-dirty-') { throw "Vectorworks $ver build $($info.build_id) was built from a dirty tree; rebuild it." }
    $commit = ($info.build_id -split '-')[0]
    $builtTree = (& git -C $Repo rev-parse "${commit}:native_bridge/src" 2>$null)
    if ("$builtTree".Trim() -ne $tree) { throw "Vectorworks $ver build $($info.build_id) does not match native_bridge\src at HEAD; rebuild it: scripts\build-native-bridge.ps1 -VectorworksVersion $ver -Configuration Release" }
    $dst = Join-Path $Repo "native\$ver"
    New-Item -ItemType Directory -Path $dst -Force | Out-Null
    foreach ($n in @("VectorworksMCPBridge.vlb", "VectorworksMCPBridge.vwr")) { Copy-Item -LiteralPath (Join-Path $out $n) -Destination (Join-Path $dst $n) -Force }
    $manifest[$ver] = [ordered]@{
        build_id = $info.build_id; built_at = $info.built_at; native_src_tree = $tree
        vlb_sha256 = Get-Sha256 (Join-Path $dst "VectorworksMCPBridge.vlb")
        vwr_sha256 = Get-Sha256 (Join-Path $dst "VectorworksMCPBridge.vwr")
        live_tested = [bool]($Tested -contains $ver)
    }
    Write-Host "native\$ver <- $($info.build_id)$(if ($Tested -contains $ver) { ' (live-tested)' })"
}
Write-JsonFile $manifestPath $manifest
Write-Host "wrote $manifestPath"
