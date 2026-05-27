[CmdletBinding()]
param(
    [ValidateSet("2024", "2025", "2026")]
    [string]$VectorworksVersion = "2024",
    [string]$SdkDir = "",
    [switch]$DownloadSdk,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$CheckerPath = Join-Path $PSScriptRoot "check-native-bridge-prereqs.ps1"
$OfficialSdkPage = "https://www.vectorworks.net/en-US/support/custom/sdk/sdkdown"
$SdkDownloadUrls = @{
    "2024" = "https://release.vectorworks.net/latest/Vectorworks/2024-NNA-eng-win-SDK"
    "2025" = "https://release.vectorworks.net/latest/Vectorworks/2025-NNA-eng-win-SDK.zip"
    "2026" = "https://release.vectorworks.net/latest/Vectorworks/2026-NNA-eng-win-SDK.zip"
}

if (-not $SdkDir) {
    $SdkDir = Join-Path $RepoRoot "third_party\VectorworksSDK\$VectorworksVersion"
}

if (-not (Test-Path -LiteralPath $CheckerPath)) {
    throw "Prerequisite checker not found at $CheckerPath"
}

Write-Host "Vectorworks native bridge bootstrap ($VectorworksVersion)"
Write-Host "Repo: $RepoRoot"
Write-Host "SDK directory: $SdkDir"

if ($DownloadSdk) {
    $CacheDir = Join-Path $RepoRoot ".cache\vectorworks-sdk"
    $ArchivePath = Join-Path $CacheDir "VectorworksSDK-$VectorworksVersion-win64.zip"
    $DownloadUrl = $SdkDownloadUrls[$VectorworksVersion]

    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
    New-Item -ItemType Directory -Force -Path $SdkDir | Out-Null

    if ((Test-Path -LiteralPath $ArchivePath) -and -not $Force) {
        Write-Host "Using cached SDK archive: $ArchivePath"
    } else {
        Write-Host "Downloading SDK from official Vectorworks URL:"
        Write-Host $DownloadUrl
        Write-Host "Official SDK page: $OfficialSdkPage"
        Invoke-WebRequest -Uri $DownloadUrl -OutFile $ArchivePath
    }

    Write-Host "Extracting SDK archive to $SdkDir"
    Expand-Archive -Path $ArchivePath -DestinationPath $SdkDir -Force
} else {
    Write-Host "SDK download skipped. Pass -DownloadSdk to fetch the official Windows SDK archive."
    Write-Host "Official SDK page: $OfficialSdkPage"
}

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $CheckerPath -VectorworksVersion $VectorworksVersion -SdkDir $SdkDir
exit $LASTEXITCODE
