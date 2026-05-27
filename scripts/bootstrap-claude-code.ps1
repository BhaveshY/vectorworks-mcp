[CmdletBinding()]
param(
    [switch]$Verify,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$RegisterScript = Join-Path $PSScriptRoot "register-claude-code.ps1"
$RegisterArgs = @()
if ($Verify) { $RegisterArgs += "-Verify" }
if ($SkipInstall) { $RegisterArgs += "-SkipInstall" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $RegisterScript @RegisterArgs
exit $LASTEXITCODE
