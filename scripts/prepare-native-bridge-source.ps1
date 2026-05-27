[CmdletBinding()]
param(
    [string]$VectorworksVersion = "2024",
    [string]$SdkExamplesDir = "",
    [switch]$CloneSdkExamples,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SdkRequirementsPath = Join-Path $RepoRoot "native_bridge\SDK_REQUIREMENTS.json"
$WorkRoot = Join-Path $RepoRoot "native_bridge\worktree"
$WorktreeRoot = Join-Path $WorkRoot "SDKExamples"
$TargetDir = Join-Path $WorktreeRoot "Examples$VectorworksVersion\VectorworksMCPBridge"

if (-not (Test-Path -LiteralPath $SdkRequirementsPath)) {
    throw "Native bridge SDK requirements file was not found at $SdkRequirementsPath"
}

$SdkRequirements = Get-Content -Raw -LiteralPath $SdkRequirementsPath | ConvertFrom-Json
$VersionRequirements = $SdkRequirements.versions.$VectorworksVersion
if (-not $VersionRequirements) {
    $SupportedVersions = ($SdkRequirements.versions.PSObject.Properties.Name | Sort-Object) -join ", "
    throw "SDK requirements do not contain Vectorworks $VectorworksVersion. Supported versions: $SupportedVersions"
}

function Test-SdkExamplesLayout {
    param(
        [string]$Path,
        [string]$Version
    )
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Container)) {
        return $false
    }

    $ExampleDir = Join-Path $Path "Examples$Version\ObjectExample"
    $SdkLibDir = Join-Path $Path "VectorworksSDK\SDK$Version\SDKLib"
    return ((Test-Path -LiteralPath $ExampleDir -PathType Container) -and
        (Test-Path -LiteralPath $SdkLibDir -PathType Container))
}

function Get-FirstSdkExamplesLayout {
    param([string]$Version)

    $Candidates = @()
    if ($env:VECTORWORKS_SDK_EXAMPLES_DIR) {
        $Candidates += $env:VECTORWORKS_SDK_EXAMPLES_DIR
    }
    $Candidates += Join-Path $RepoRoot "third_party\VectorworksSDKExamples"
    $Candidates += Join-Path $RepoRoot ".cache\VectorworksSDKExamples"
    $Candidates += Join-Path $RepoRoot "third_party\VectorworksSDK\$Version"
    $Candidates += Join-Path $RepoRoot "third_party\VectorworksSDK"
    if ($env:USERPROFILE) {
        $Candidates += Join-Path $env:USERPROFILE "Downloads\SDKExamples"
        $Candidates += Join-Path $env:USERPROFILE "Downloads\Vectorworks SDK Examples"
    }

    foreach ($Candidate in ($Candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (Test-SdkExamplesLayout -Path $Candidate -Version $Version) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }

    return ""
}

function New-DirectoryLinkOrCopy {
    param(
        [string]$LinkPath,
        [string]$TargetPath
    )

    if (-not (Test-Path -LiteralPath $TargetPath -PathType Container)) {
        throw "Required SDK examples dependency was not found at $TargetPath"
    }
    if (Test-Path -LiteralPath $LinkPath) {
        return
    }

    try {
        New-Item -ItemType Junction -Path $LinkPath -Target $TargetPath | Out-Null
    } catch {
        Write-Warning "Could not create junction $LinkPath -> $TargetPath; copying instead. This may take a while."
        Copy-Item -LiteralPath $TargetPath -Destination $LinkPath -Recurse
    }
}

if (-not $SdkExamplesDir) {
    $SdkExamplesDir = Get-FirstSdkExamplesLayout -Version $VectorworksVersion
}

if (-not $SdkExamplesDir -and $CloneSdkExamples) {
    $SdkExamplesDir = Join-Path $RepoRoot "third_party\VectorworksSDKExamples"
    $ExamplesUrl = [string]$SdkRequirements.officialSdkExamples
    if ((Test-Path -LiteralPath $SdkExamplesDir) -and -not $Force) {
        Write-Host "Using existing SDK examples clone: $SdkExamplesDir"
    } else {
        if (Test-Path -LiteralPath $SdkExamplesDir) {
            $ResolvedExamplesDir = (Resolve-Path -LiteralPath $SdkExamplesDir).Path
            $ResolvedThirdParty = (Resolve-Path -LiteralPath (Join-Path $RepoRoot "third_party")).Path
            if (-not $ResolvedExamplesDir.StartsWith($ResolvedThirdParty, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to remove SDK examples directory outside third_party: $ResolvedExamplesDir"
            }
            Remove-Item -LiteralPath $SdkExamplesDir -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SdkExamplesDir) | Out-Null
        Write-Host "Cloning official Vectorworks SDK examples:"
        Write-Host $ExamplesUrl
        & git clone --depth 1 $ExamplesUrl $SdkExamplesDir
        if ($LASTEXITCODE -ne 0) {
            throw "git clone failed with exit code $LASTEXITCODE"
        }
    }
}

if (-not (Test-SdkExamplesLayout -Path $SdkExamplesDir -Version $VectorworksVersion)) {
    throw @"
Vectorworks SDK examples were not found for $VectorworksVersion.

Fix one of these:
- Set VECTORWORKS_SDK_EXAMPLES_DIR to a clone of $($SdkRequirements.officialSdkExamples)
- Pass -SdkExamplesDir C:\path\to\SDKExamples
- Rerun with -CloneSdkExamples to clone the official examples into third_party\VectorworksSDKExamples
"@
}

$SdkExamplesDir = (Resolve-Path -LiteralPath $SdkExamplesDir).Path
$SourceExampleDir = Join-Path $SdkExamplesDir "Examples$VectorworksVersion\ObjectExample"

if ((Test-Path -LiteralPath $WorktreeRoot) -and -not $Force) {
    throw "Native bridge worktree already exists at $WorktreeRoot. Pass -Force to recreate it."
}

if (Test-Path -LiteralPath $WorktreeRoot) {
    $ResolvedWorktree = (Resolve-Path -LiteralPath $WorktreeRoot).Path
    if (-not $ResolvedWorktree.StartsWith($WorkRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove target outside native_bridge\worktree: $ResolvedWorktree"
    }
    Remove-Item -LiteralPath $WorktreeRoot -Recurse -Force
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $TargetDir) | Out-Null
Copy-Item -LiteralPath $SourceExampleDir -Destination $TargetDir -Recurse
New-DirectoryLinkOrCopy -LinkPath (Join-Path $WorktreeRoot "VectorworksSDK") -TargetPath (Join-Path $SdkExamplesDir "VectorworksSDK")
New-DirectoryLinkOrCopy -LinkPath (Join-Path $WorktreeRoot "ThirdPartySource") -TargetPath (Join-Path $SdkExamplesDir "ThirdPartySource")

$NotesPath = Join-Path $TargetDir "VECTORWORKS_MCP_BRIDGE_NOTES.md"
$Notes = @"
# Vectorworks MCP Native Bridge Worktree

Generated by ``scripts\prepare-native-bridge-source.ps1`` from the official
Vectorworks SDK example:

- SDK examples: ``$SdkExamplesDir``
- Source example: ``Examples$VectorworksVersion\ObjectExample``
- Worktree root: ``$WorktreeRoot``
- Target Vectorworks version: ``$VectorworksVersion``

This folder is intentionally ignored by git. Use it as the local SDK-backed
implementation workspace, then copy only reviewable source changes back into
``native_bridge/src`` when they are ready.

The copied project keeps the official example's relative layout. ``VectorworksSDK``
and ``ThirdPartySource`` are junctions to the official SDK examples clone when
possible, or copied folders if junction creation is unavailable.

Recommended order:

1. Build the unmodified copied example with ``scripts\build-native-bridge.ps1``.
2. Confirm Vectorworks can load the example plug-in.
3. Rename the module/project and replace the example extension code with the
   bridge transport and request queue.
4. Keep socket work off the Vectorworks API path; marshal CAD handlers to the
   Vectorworks main/plugin event context.
5. Implement phase 0 and phase 1 from ``native_bridge\HANDLER_MATRIX.md``.
"@
Set-Content -LiteralPath $NotesPath -Value $Notes -Encoding UTF8

Write-Host "Prepared native bridge source worktree:"
Write-Host $WorktreeRoot
Write-Host "Bridge project:"
Write-Host $TargetDir
Write-Host ""
Write-Host "Next build command:"
Write-Host "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-native-bridge.ps1 -VectorworksVersion $VectorworksVersion"
