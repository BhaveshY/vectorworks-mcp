[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$SdkDir = "",
    [string]$SourceDir = "",
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release",
    [switch]$SkipPrereqCheck
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$CheckerPath = Join-Path $PSScriptRoot "check-native-bridge-prereqs.ps1"
$PreparePath = Join-Path $PSScriptRoot "prepare-native-bridge-source.ps1"
$WirePath = Join-Path $PSScriptRoot "wire-native-bridge-project.ps1"
if (-not $SourceDir) {
    $SourceDir = Join-Path $RepoRoot "native_bridge\worktree\SDKExamples"
}

function Get-MSBuildFromPath {
    $Command = Get-Command msbuild.exe -ErrorAction SilentlyContinue
    if ($Command) {
        return $Command.Source
    }
    return ""
}

function Get-CheckByName {
    param(
        [object]$Report,
        [string]$Name
    )
    return $Report.checks | Where-Object { $_.name -eq $Name } | Select-Object -First 1
}

$MSBuildPath = ""
if (-not $SkipPrereqCheck) {
    if (-not (Test-Path -LiteralPath $CheckerPath)) {
        throw "Native bridge prerequisite checker was not found at $CheckerPath"
    }

    $CheckerArgs = @("-VectorworksVersion", $VectorworksVersion, "-Advisory", "-Json")
    if ($SdkDir) { $CheckerArgs += @("-SdkDir", $SdkDir) }

    $Json = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $CheckerPath @CheckerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Native prerequisite checker failed with exit code $LASTEXITCODE"
    }
    $Report = $Json | ConvertFrom-Json

    if (-not $Report.ready) {
        $Missing = @($Report.checks | Where-Object { $_.required -and -not $_.ok } | ForEach-Object { $_.name })
        Write-Error ("Native bridge prerequisites are missing: {0}" -f ($Missing -join ", "))
        exit 2
    }

    $MSBuildCheck = Get-CheckByName -Report $Report -Name "MSBuild"
    if ($MSBuildCheck -and $MSBuildCheck.ok) {
        $MSBuildPath = [string]$MSBuildCheck.detail
    }
}

if (-not $MSBuildPath) {
    $MSBuildPath = Get-MSBuildFromPath
}
if (-not $MSBuildPath) {
    throw "MSBuild was not found. Install Visual Studio Build Tools or rerun without -SkipPrereqCheck for a fuller report."
}

if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
    throw "Native source worktree was not found at $SourceDir. Run $PreparePath first."
}

$Solution = Get-ChildItem -LiteralPath $SourceDir -Recurse -File -Filter "*$VectorworksVersion.sln" |
    Select-Object -First 1
if (-not $Solution) {
    throw "No Vectorworks $VectorworksVersion solution (*.sln) was found under $SourceDir."
}

$ScaffoldSourceDir = Join-Path $Solution.DirectoryName "Source\VectorworksMCPBridge"
$RequiredScaffoldFiles = @(
    "BridgeProtocol.hpp",
    "BridgeProtocol.cpp",
    "NativeTransport.hpp",
    "NativeTransport.cpp",
    "BridgeDispatcher.hpp",
    "CadRequestQueue.hpp",
    "VectorworksMCPBridge.cpp"
)
$ScaffoldPresent = @($RequiredScaffoldFiles | Where-Object {
    Test-Path -LiteralPath (Join-Path $ScaffoldSourceDir $_) -PathType Leaf
}).Count -eq $RequiredScaffoldFiles.Count
if ($ScaffoldPresent) {
    if (-not (Test-Path -LiteralPath $WirePath -PathType Leaf)) {
        throw "Native project wiring helper was not found at $WirePath"
    }
    $ProjectFile = Get-ChildItem -LiteralPath $Solution.DirectoryName -Recurse -File -Filter "*.vcxproj" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\(Source|include|SDKLib|ThirdPartySource)\\' } |
        Select-Object -First 1
    if (-not $ProjectFile) {
        throw "Native bridge scaffold is present, but no SDK Visual C++ project (*.vcxproj) was found next to the solution."
    }
    $WireJson = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $WirePath -VectorworksVersion $VectorworksVersion -ProjectPath $ProjectFile.FullName -SourceDir $ScaffoldSourceDir -CheckOnly -Json | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "Native bridge project wiring check failed with exit code $LASTEXITCODE"
    }
    $WireReport = $WireJson | ConvertFrom-Json
    if (-not $WireReport.projectWired) {
        $MissingItems = @($WireReport.missingProjectItems | ForEach-Object { [string]$_ }) -join ", "
        throw "Native bridge scaffold is present but not wired into the SDK project. Run scripts\wire-native-bridge-project.ps1 first. Missing: $MissingItems"
    }
}

Write-Host "Building native bridge worktree"
Write-Host "Solution: $($Solution.FullName)"
Write-Host "Configuration: $Configuration|x64"
Write-Host "MSBuild: $MSBuildPath"

& $MSBuildPath $Solution.FullName /m /p:Configuration=$Configuration /p:Platform=x64 /p:LanguageStandard=stdcpp17
exit $LASTEXITCODE
