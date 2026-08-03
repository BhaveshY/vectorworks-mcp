[CmdletBinding()]
param(
    [switch]$Verify,
    [switch]$SkipInstall,
    [switch]$SkipClipboard,
    [switch]$EnablePythonDialogFallback
)

$ErrorActionPreference = "Stop"
$RegisterScript = Join-Path $PSScriptRoot "register-claude-code.ps1"
$RegisterArgs = @()
if ($Verify) { $RegisterArgs += "-Verify" }
if ($SkipInstall) { $RegisterArgs += "-SkipInstall" }
if ($EnablePythonDialogFallback) {
    $RegisterArgs += "-EnablePythonDialogFallback"
    if (-not $SkipClipboard) { $RegisterArgs += @("-CopyLoaderToClipboard", "-BestEffortClipboard") }
}

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $RegisterScript @RegisterArgs
exit $LASTEXITCODE
