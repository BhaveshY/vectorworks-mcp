[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$SdkDir = "",
    [string]$SdkExamplesDir = "",
    [switch]$DownloadSdk,
    [switch]$InstallVisualStudioBuildTools,
    [switch]$CloneSdkExamples,
    [switch]$PrepareSource,
    [switch]$Build,
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Debug",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$CheckerPath = Join-Path $PSScriptRoot "check-native-bridge-prereqs.ps1"
$PreparePath = Join-Path $PSScriptRoot "prepare-native-bridge-source.ps1"
$BuildPath = Join-Path $PSScriptRoot "build-native-bridge.ps1"
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

if ($InstallVisualStudioBuildTools) {
    $Winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $Winget) {
        throw "winget.exe was not found. Install App Installer or install Visual Studio 2022 Build Tools manually."
    }

    Write-Host "Installing Visual Studio 2022 Build Tools C++ workload with winget."
    Write-Host "Package: Microsoft.VisualStudio.2022.BuildTools"
    & $Winget.Source install `
        --id Microsoft.VisualStudio.2022.BuildTools `
        --exact `
        --source winget `
        --accept-package-agreements `
        --accept-source-agreements `
        --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
    if ($LASTEXITCODE -ne 0) {
        throw "winget Visual Studio Build Tools install failed with exit code $LASTEXITCODE"
    }
    Write-Host "Visual Studio Build Tools installer finished. A reboot may still be required before MSBuild is visible."
} else {
    Write-Host "Visual Studio Build Tools install skipped. Pass -InstallVisualStudioBuildTools to install the C++ workload with winget."
}

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

$PrepareRequested = $PrepareSource -or $CloneSdkExamples -or $Build
if ($PrepareRequested) {
    if (-not (Test-Path -LiteralPath $PreparePath)) {
        throw "Native source preparation script not found at $PreparePath"
    }

    $PrepareArgs = @("-VectorworksVersion", $VectorworksVersion)
    if ($SdkDir) { $PrepareArgs += @("-SdkDir", $SdkDir) }
    if ($SdkExamplesDir) { $PrepareArgs += @("-SdkExamplesDir", $SdkExamplesDir) }
    if ($CloneSdkExamples) { $PrepareArgs += "-CloneSdkExamples" }
    if ($Force) { $PrepareArgs += "-Force" }

    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $PreparePath @PrepareArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $CheckerPath -VectorworksVersion $VectorworksVersion -SdkDir $SdkDir
$CheckExitCode = $LASTEXITCODE
if ($CheckExitCode -ne 0) {
    exit $CheckExitCode
}

if ($Build) {
    if (-not (Test-Path -LiteralPath $BuildPath)) {
        throw "Native build script not found at $BuildPath"
    }
    $BuildArgs = @("-VectorworksVersion", $VectorworksVersion, "-Configuration", $Configuration)
    if ($SdkDir) { $BuildArgs += @("-SdkDir", $SdkDir) }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $BuildPath @BuildArgs
    exit $LASTEXITCODE
}

exit 0
