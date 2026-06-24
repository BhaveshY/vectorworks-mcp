[CmdletBinding()]
param(
    [ValidateSet("ClaudeCode", "HostOnly")]
    [string]$Client = "ClaudeCode",
    [switch]$Verify,
    [switch]$SkipClipboard
)

$ErrorActionPreference = "Stop"

switch ($Client) {
    "ClaudeCode" {
        $RegisterArgs = @()
        if ($Verify) { $RegisterArgs += "-Verify" }
        if (-not $SkipClipboard) { $RegisterArgs += @("-CopyLoaderToClipboard", "-BestEffortClipboard") }
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "register-claude-code.ps1") @RegisterArgs
        exit $LASTEXITCODE
    }
    "HostOnly" {
        $RegisterArgs = @("-NoClaudeConfig")
        if ($Verify) { $RegisterArgs += "-Verify" }
        if (-not $SkipClipboard) { $RegisterArgs += @("-CopyLoaderToClipboard", "-BestEffortClipboard") }
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "register-claude-code.ps1") @RegisterArgs
        exit $LASTEXITCODE
    }
}
