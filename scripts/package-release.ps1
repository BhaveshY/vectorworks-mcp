<#
.SYNOPSIS
  Build a portable Vectorworks MCP release zip for installing on another PC.

.DESCRIPTION
  dist\vectorworks-mcp-<version>-win64.zip contains the runtime source, install.ps1,
  the shared scripts, the skills-only plugin, a prebuilt native plug-in under
  native\<vectorworks version>\ when one is available, RELEASE.json and, with
  -IncludeWheels, every locked dependency wheel (offline install).
  dist\vectorworks-plugin-<version>.zip is the skills-only plugin folder for
  uploading to a Claude organisation (Cowork) or another marketplace.

  On the target PC: extract the zip, then
    powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 [-FullNative]

.EXAMPLE
  .\scripts\package-release.ps1 -IncludeWheels
.EXAMPLE
  .\scripts\package-release.ps1 -NativeDir D:\builds\bridge -OutDir C:\temp\dist
#>
[CmdletBinding()]
param(
    [string]$OutDir = "",
    [string]$NativeDir = "",
    [switch]$IncludeWheels,
    [string]$Python = "",
    [string]$PythonVersion = "3.12"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "install-common.ps1")
Add-Type -AssemblyName System.IO.Compression.FileSystem

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutDir) { $OutDir = Join-Path $Repo "dist" }
$OutDir = Get-FullPath $OutDir
$version = Get-SourceVersion $Repo
$name = "vectorworks-mcp-$version-win64"
$stage = Join-Path $OutDir "stage\$name"
Remove-Tree (Join-Path $OutDir "stage")
New-Item -ItemType Directory -Path $stage -Force | Out-Null

Write-Step "Packaging Vectorworks MCP $version" "head"
$n = Copy-RuntimeTree $Repo $stage
Remove-Tree (Join-Path $stage "native")
Write-Step "copied $n source files" "ok"

# Prebuilt native plug-ins: explicit -NativeDir, then native\ in the repo, then the build output.
$native = [ordered]@{}
foreach ($ver in @("2024", "2025")) {
    $cands = @()
    if ($NativeDir) { $cands += @((Join-Path $NativeDir $ver), $NativeDir) }
    $cands += @((Join-Path $Repo "native\$ver"), (Join-Path $Repo "native_bridge\project\out\$ver\Release"))
    foreach ($c in $cands) {
        $vlb = Join-Path $c "VectorworksMCPBridge.vlb"; $vwr = Join-Path $c "VectorworksMCPBridge.vwr"
        if ((Test-Path -LiteralPath $vlb) -and (Test-Path -LiteralPath $vwr)) {
            $dst = Join-Path $stage "native\$ver"
            New-Item -ItemType Directory -Path $dst -Force | Out-Null
            Copy-Item -LiteralPath $vlb, $vwr -Destination $dst -Force
            $bi = Join-Path $c "build-info.json"
            if (Test-Path -LiteralPath $bi) { Copy-Item -LiteralPath $bi -Destination $dst -Force }
            $native[$ver] = [ordered]@{ source = (Get-FullPath $c); vlb_sha256 = (Get-Sha256 $vlb); vwr_sha256 = (Get-Sha256 $vwr) }
            Write-Step "native plug-in for Vectorworks ${ver}: $c" "ok"
            break
        }
    }
}
if ($native.Count -eq 0) { Write-Step "no prebuilt VectorworksMCPBridge.vlb found - the target PC needs -FullNative with Visual Studio + SDK" "warn" }

if ($IncludeWheels) {
    $py = if ($Python) { $Python } else { $f = Find-BasePython; if (-not $f) { throw "Python 3.11+ is required to download wheels." }; $f.executable }
    $wheels = Join-Path $stage "wheels"
    $r = Invoke-Native -FilePath $py -Arguments @("-I", "-m", "pip", "download", "--disable-pip-version-check", "--require-hashes",
        "-r", (Join-Path $Repo "requirements.lock"), "--only-binary=:all:", "--platform", "win_amd64",
        "--python-version", $PythonVersion, "--implementation", "cp", "-d", $wheels) -TimeoutSec 1200
    if ($r.ExitCode -ne 0) { throw "pip download failed: $(($r.StdErr -split "`n" | Select-Object -Last 6) -join "`n")" }
    Write-Step "downloaded $(@(Get-ChildItem -LiteralPath $wheels -File).Count) wheels for CPython $PythonVersion win_amd64" "ok"
}

$commit = ""
try { $commit = (& git -C $Repo rev-parse --short HEAD 2>$null) } catch { }
$dirty = $false
try { $dirty = [bool](& git -C $Repo status --porcelain 2>$null) } catch { }
Write-JsonFile (Join-Path $stage "RELEASE.json") ([ordered]@{
    product = "vectorworks-mcp"; version = $version; built_at = (Get-Date).ToUniversalTime().ToString("o")
    git_commit = $commit; git_dirty = $dirty; native = $native; wheels = [bool]$IncludeWheels
    install = "powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1"
})

$zip = Join-Path $OutDir "$name.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
[System.IO.Compression.ZipFile]::CreateFromDirectory((Join-Path $OutDir "stage"), $zip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
Write-Step "release zip: $zip ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)" "ok"

$pzip = Join-Path $OutDir "vectorworks-plugin-$version.zip"
if (Test-Path -LiteralPath $pzip) { Remove-Item -LiteralPath $pzip -Force }
[System.IO.Compression.ZipFile]::CreateFromDirectory((Join-Path $Repo "plugins\vectorworks"), $pzip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
Write-Step "plugin zip (skills only): $pzip" "ok"
Remove-Tree (Join-Path $OutDir "stage")
