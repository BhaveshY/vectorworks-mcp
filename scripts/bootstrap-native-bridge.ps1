[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$SdkDir = "",
    [switch]$DownloadSdk,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$CheckerPath = Join-Path $PSScriptRoot "check-native-bridge-prereqs.ps1"
$SdkRequirementsPath = Join-Path $RepoRoot "native_bridge\SDK_REQUIREMENTS.json"
if (-not (Test-Path -LiteralPath $SdkRequirementsPath)) {
    throw "Native bridge SDK requirements file was not found at $SdkRequirementsPath"
}
$SdkRequirements = Get-Content -Raw -LiteralPath $SdkRequirementsPath | ConvertFrom-Json
$OfficialSdkPage = [string]$SdkRequirements.officialSdkPage
$VersionRequirements = $SdkRequirements.versions.$VectorworksVersion
if (-not $VersionRequirements) {
    $SupportedVersions = ($SdkRequirements.versions.PSObject.Properties.Name | Sort-Object) -join ", "
    throw "SDK requirements do not contain Vectorworks $VectorworksVersion. Supported versions: $SupportedVersions"
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
    $DownloadUrl = [string]$VersionRequirements.winSdkDownload

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
