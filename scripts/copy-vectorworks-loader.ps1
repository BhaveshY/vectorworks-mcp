[CmdletBinding()]
param(
    [string]$LauncherPath = "",
    [string]$LoaderPath = "",
    [switch]$Regenerate,
    [switch]$Print,
    [switch]$BestEffort
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RegisterScript = Join-Path $RepoRoot "scripts\register-claude-code.ps1"
if (-not $LauncherPath) {
    $LauncherPath = Join-Path $RepoRoot "vw_start_listener_2024.py"
}
if (-not $LoaderPath) {
    $LoaderPath = Join-Path $RepoRoot "vw_load_listener_2024.py"
}
$LauncherPath = [System.IO.Path]::GetFullPath($LauncherPath)
$LoaderPath = [System.IO.Path]::GetFullPath($LoaderPath)

function ConvertTo-PythonRawStringLiteralText {
    param([string]$Value)
    return $Value.Replace("\", "\\").Replace('"', '\"')
}

$ShouldRegenerate = $Regenerate.IsPresent -or -not (Test-Path -LiteralPath $LauncherPath) -or -not (Test-Path -LiteralPath $LoaderPath)
if ($ShouldRegenerate) {
    if (-not (Test-Path -LiteralPath $RegisterScript)) {
        throw "register-claude-code.ps1 was not found at $RegisterScript"
    }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $RegisterScript -SkipInstall -NoClaudeConfig -LauncherPath $LauncherPath -LoaderPath $LoaderPath
    if ($LASTEXITCODE -ne 0) {
        throw "Could not regenerate Vectorworks loader. register-claude-code.ps1 exited with code $LASTEXITCODE."
    }
}

if (-not (Test-Path -LiteralPath $LoaderPath)) {
    throw "Vectorworks loader was not found at $LoaderPath"
}

$LoaderText = Get-Content -Raw -LiteralPath $LoaderPath
$ExpectedLauncherLiteral = ConvertTo-PythonRawStringLiteralText $LauncherPath
if ($LoaderText -notmatch "runpy\.run_path") {
    throw "Vectorworks loader at $LoaderPath does not call runpy.run_path."
}
if (-not $LoaderText.Contains($ExpectedLauncherLiteral)) {
    throw "Vectorworks loader at $LoaderPath does not point to launcher $LauncherPath. Run with -Regenerate."
}

Write-Host "Vectorworks launcher: $LauncherPath"
Write-Host "Vectorworks loader: $LoaderPath"

try {
    Set-Clipboard -Value $LoaderText
    Write-Host "Copied Vectorworks loader script to clipboard."
} catch {
    $SetClipboardError = $_.Exception.Message
    $Clip = Get-Command clip.exe -ErrorAction SilentlyContinue
    if ($Clip) {
        try {
            $LoaderText | & $Clip.Source
            Write-Host "Copied Vectorworks loader script to clipboard with clip.exe."
        } catch {
            $Message = "Could not copy Vectorworks loader script to clipboard: Set-Clipboard failed with '$SetClipboardError'; clip.exe failed with '$($_.Exception.Message)'"
            if ($BestEffort) {
                Write-Warning $Message
            } else {
                throw $Message
            }
        }
    } else {
        $Message = "Could not copy Vectorworks loader script to clipboard: $SetClipboardError"
        if ($BestEffort) {
            Write-Warning $Message
        } else {
            throw $Message
        }
    }
}

Write-Host "Paste the clipboard contents into the Vectorworks Resource Manager script or Plug-in Manager menu command."
if ($Print) {
    Write-Output $LoaderText
}
