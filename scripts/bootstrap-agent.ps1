[CmdletBinding()]
param(
    [ValidateSet("ClaudeCode", "HostOnly")]
    [string]$Client = "ClaudeCode",
    [switch]$Verify
)

$ErrorActionPreference = "Stop"

switch ($Client) {
    "ClaudeCode" {
        $RegisterArgs = @()
        if ($Verify) { $RegisterArgs += "-Verify" }
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "register-claude-code.ps1") @RegisterArgs
        exit $LASTEXITCODE
    }
    "HostOnly" {
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "run-mcp-server.ps1") -SetupOnly
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "verify-no-vectorworks.ps1")
        exit $LASTEXITCODE
    }
}
