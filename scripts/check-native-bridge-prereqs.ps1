[CmdletBinding()]
param(
    [ValidateSet("2024", "2025", "2026")]
    [string]$VectorworksVersion = "2024",
    [string]$SdkDir = "",
    [switch]$Advisory,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OfficialSdkPage = "https://www.vectorworks.net/en-US/support/custom/sdk/sdkdown"
$OfficialSdkExamples = "https://github.com/VectorworksDeveloper/SDKExamples"
$SdkDownloadUrls = @{
    "2024" = "https://release.vectorworks.net/latest/Vectorworks/2024-NNA-eng-win-SDK"
    "2025" = "https://release.vectorworks.net/latest/Vectorworks/2025-NNA-eng-win-SDK.zip"
    "2026" = "https://release.vectorworks.net/latest/Vectorworks/2026-NNA-eng-win-SDK.zip"
}
$VisualStudioRequirements = @{
    "2024" = @{ MinimumVersion = "17.6.3"; Toolset = "v143" }
    "2025" = @{ MinimumVersion = "17.8"; Toolset = "v143" }
    "2026" = @{ MinimumVersion = "17.12"; Toolset = "v143" }
}

function New-CheckResult {
    param(
        [string]$Name,
        [bool]$Required,
        [bool]$Ok,
        [string]$Detail,
        [string]$Fix
    )
    [pscustomobject]@{
        name = $Name
        required = $Required
        ok = $Ok
        detail = $Detail
        fix = $Fix
    }
}

function Get-FirstExistingPath {
    param([string[]]$Paths)
    foreach ($Path in $Paths) {
        if ($Path -and (Test-Path -LiteralPath $Path)) {
            return (Resolve-Path -LiteralPath $Path).Path
        }
    }
    return ""
}

function Test-VersionAtLeast {
    param(
        [string]$Actual,
        [string]$Minimum
    )
    if (-not $Actual -or -not $Minimum) {
        return $false
    }
    try {
        return ([version]$Actual -ge [version]$Minimum)
    } catch {
        return $false
    }
}

function Find-VectorworksInstall {
    param([string]$Version)

    $Candidates = @()
    if ($env:ProgramFiles) {
        $Candidates += Join-Path $env:ProgramFiles "Vectorworks $Version\Vectorworks$Version.exe"
        $Candidates += Join-Path $env:ProgramFiles "Vectorworks $Version\Vectorworks.exe"
    }
    if (${env:ProgramFiles(x86)}) {
        $Candidates += Join-Path ${env:ProgramFiles(x86)} "Vectorworks $Version\Vectorworks$Version.exe"
        $Candidates += Join-Path ${env:ProgramFiles(x86)} "Vectorworks $Version\Vectorworks.exe"
    }

    $Found = Get-FirstExistingPath -Paths $Candidates
    if ($Found) { return $Found }

    if ($env:ProgramFiles -and (Test-Path -LiteralPath $env:ProgramFiles)) {
        $InstallDir = Get-ChildItem -LiteralPath $env:ProgramFiles -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "Vectorworks $Version*" } |
            Select-Object -First 1
        if ($InstallDir) {
            $Exe = Get-ChildItem -LiteralPath $InstallDir.FullName -File -Filter "Vectorworks*.exe" -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($Exe) { return $Exe.FullName }
            return $InstallDir.FullName
        }
    }

    return ""
}

function Test-SdkLayout {
    param([string]$Path)

    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Container)) {
        return $false
    }

    $DirectMarkers = @(
        "SDKLib",
        "VWFC",
        "Samples",
        "SDKExamples",
        "VectorworksSDK"
    )
    foreach ($Marker in $DirectMarkers) {
        if (Test-Path -LiteralPath (Join-Path $Path $Marker)) {
            return $true
        }
    }

    $HeaderNames = @(
        "MiniCadCallBacks.h",
        "VectorworksSDK.h",
        "VWPluginLibrary.h"
    )
    try {
        $Header = Get-ChildItem -LiteralPath $Path -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $HeaderNames -contains $_.Name } |
            Select-Object -First 1
        return [bool]$Header
    } catch {
        return $false
    }
}

function Find-SdkInstall {
    param(
        [string]$Version,
        [string]$RequestedPath
    )

    $Candidates = @()
    if ($RequestedPath) { $Candidates += $RequestedPath }
    if ($env:VECTORWORKS_SDK_DIR) { $Candidates += $env:VECTORWORKS_SDK_DIR }
    $Candidates += Join-Path $RepoRoot "third_party\VectorworksSDK\$Version"
    $Candidates += Join-Path $RepoRoot "third_party\VectorworksSDK"
    if ($env:USERPROFILE) {
        $Candidates += Join-Path $env:USERPROFILE "Downloads\Vectorworks SDK $Version"
        $Candidates += Join-Path $env:USERPROFILE "Downloads\$Version-NNA-eng-win-SDK"
    }

    foreach ($Candidate in ($Candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (Test-SdkLayout -Path $Candidate) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }

    return ""
}

function Find-VisualStudioCpp {
    $VsWhereCandidates = @()
    if (${env:ProgramFiles(x86)}) {
        $VsWhereCandidates += Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    }
    if ($env:ProgramFiles) {
        $VsWhereCandidates += Join-Path $env:ProgramFiles "Microsoft Visual Studio\Installer\vswhere.exe"
    }

    $VsWhere = Get-FirstExistingPath -Paths $VsWhereCandidates
    if ($VsWhere) {
        $Json = (& $VsWhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -format json 2>$null)
        if ($Json) {
            try {
                $Install = $Json | ConvertFrom-Json | Select-Object -First 1
                if ($Install) {
                    return [pscustomobject]@{
                        path = [string]$Install.installationPath
                        version = [string]$Install.installationVersion
                        source = $VsWhere
                        detail = ("{0} ({1})" -f $Install.installationPath, $Install.installationVersion)
                    }
                }
            } catch {
                # Fall through to cl.exe detection.
            }
        }
    }

    $Cl = Get-Command cl.exe -ErrorAction SilentlyContinue
    if ($Cl) {
        return [pscustomobject]@{
            path = $Cl.Source
            version = ""
            source = "PATH"
            detail = "$($Cl.Source) (Visual Studio version not verifiable without vswhere)"
        }
    }

    return [pscustomobject]@{
        path = ""
        version = ""
        source = ""
        detail = "not found via vswhere or cl.exe"
    }
}

function Find-MSBuild {
    param([string]$VisualStudioPath)

    $Command = Get-Command msbuild.exe -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }

    if ($VisualStudioPath -and (Test-Path -LiteralPath $VisualStudioPath -PathType Container)) {
        $Candidate = Join-Path $VisualStudioPath "MSBuild\Current\Bin\MSBuild.exe"
        if (Test-Path -LiteralPath $Candidate) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }

    return ""
}

$VectorworksPath = Find-VectorworksInstall -Version $VectorworksVersion
$SdkPath = Find-SdkInstall -Version $VectorworksVersion -RequestedPath $SdkDir
$VisualStudio = Find-VisualStudioCpp
$VisualStudioPath = $VisualStudio.path
$RequiredVs = $VisualStudioRequirements[$VectorworksVersion]
$VisualStudioVersionOk = Test-VersionAtLeast -Actual $VisualStudio.version -Minimum $RequiredVs.MinimumVersion
$VisualStudioOk = [bool]$VisualStudioPath -and $VisualStudioVersionOk
$MSBuildPath = Find-MSBuild -VisualStudioPath $VisualStudioPath
$CMake = Get-Command cmake.exe -ErrorAction SilentlyContinue

$Checks = @()
$Checks += New-CheckResult `
    -Name "Vectorworks $VectorworksVersion install" `
    -Required $true `
    -Ok ([bool]$VectorworksPath) `
    -Detail $(if ($VectorworksPath) { $VectorworksPath } else { "not found in Program Files" }) `
    -Fix "Install Vectorworks $VectorworksVersion or adjust this script if it is installed in a custom location."

$Checks += New-CheckResult `
    -Name "Vectorworks $VectorworksVersion SDK" `
    -Required $true `
    -Ok ([bool]$SdkPath) `
    -Detail $(if ($SdkPath) { $SdkPath } else { "not found. Checked VECTORWORKS_SDK_DIR, third_party\VectorworksSDK\$VectorworksVersion, and Downloads." }) `
    -Fix "Download the SDK from $OfficialSdkPage, extract it, then rerun with -SdkDir or set VECTORWORKS_SDK_DIR."

$Checks += New-CheckResult `
    -Name "Visual Studio C++ tools for Vectorworks $VectorworksVersion" `
    -Required $true `
    -Ok $VisualStudioOk `
    -Detail $(if ($VisualStudioPath) { "$($VisualStudio.detail); required >= $($RequiredVs.MinimumVersion) ($($RequiredVs.Toolset))" } else { $VisualStudio.detail }) `
    -Fix "Install Visual Studio 2022 Build Tools with Desktop development with C++; Vectorworks $VectorworksVersion SDK examples require VS >= $($RequiredVs.MinimumVersion) and toolset $($RequiredVs.Toolset)."

$Checks += New-CheckResult `
    -Name "MSBuild" `
    -Required $true `
    -Ok ([bool]$MSBuildPath) `
    -Detail $(if ($MSBuildPath) { $MSBuildPath } else { "not found on PATH or under Visual Studio" }) `
    -Fix "Install Visual Studio 2022 Build Tools, then rerun from a Developer PowerShell if needed."

$Checks += New-CheckResult `
    -Name "CMake" `
    -Required $false `
    -Ok ([bool]$CMake) `
    -Detail $(if ($CMake) { $CMake.Source } else { "not found; optional unless the native bridge project chooses CMake" }) `
    -Fix "Optional: install CMake or use the Vectorworks SDK Visual Studio project template."

$RequiredFailures = @($Checks | Where-Object { $_.required -and -not $_.ok })

if ($Json) {
    [pscustomobject]@{
        vectorworksVersion = $VectorworksVersion
        officialSdkPage = $OfficialSdkPage
        officialSdkExamples = $OfficialSdkExamples
        officialWinSdkDownload = $SdkDownloadUrls[$VectorworksVersion]
        requiredVisualStudioVersion = $RequiredVs.MinimumVersion
        requiredToolset = $RequiredVs.Toolset
        repoRoot = $RepoRoot
        checks = $Checks
        ready = ($RequiredFailures.Count -eq 0)
    } | ConvertTo-Json -Depth 8
} else {
    Write-Host "Vectorworks native bridge prerequisite check ($VectorworksVersion)"
    Write-Host "SDK page: $OfficialSdkPage"
    Write-Host "SDK examples/build requirements: $OfficialSdkExamples"
    Write-Host "Win SDK:  $($SdkDownloadUrls[$VectorworksVersion])"
    Write-Host "VS tools: >= $($RequiredVs.MinimumVersion) ($($RequiredVs.Toolset))"
    Write-Host ""
    foreach ($Check in $Checks) {
        $Status = if ($Check.ok) { "OK" } elseif ($Check.required) { "MISSING" } else { "OPTIONAL" }
        Write-Host ("[{0}] {1}: {2}" -f $Status, $Check.name, $Check.detail)
        if (-not $Check.ok) {
            Write-Host ("      Fix: {0}" -f $Check.fix)
        }
    }

    if ($RequiredFailures.Count -eq 0) {
        Write-Host ""
        Write-Host "OK: native bridge prerequisites appear ready."
    } else {
        Write-Host ""
        Write-Warning "Native bridge prerequisites are not complete. The pure-Python dialog listener remains the safe fallback agent-session mode."
    }
}

if ($RequiredFailures.Count -gt 0 -and -not $Advisory) {
    exit 2
}

exit 0
