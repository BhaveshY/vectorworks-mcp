[CmdletBinding()]
param(
    [string]$HostName = "",
    [ValidateRange(1, 65535)]
    [int]$Port = 0,
    [ValidateRange(1, 120)]
    [int]$TimeoutSeconds = 5,
    [ValidateRange(1, 100)]
    [int]$PingCount = 10,
    [ValidateRange(1, 100)]
    [int]$ReadCount = 10,
    [ValidateRange(0, 600000)]
    [double]$MaxPingMilliseconds = 0,
    [ValidateRange(0, 600000)]
    [double]$MaxReadMilliseconds = 0,
    [ValidateRange(0, 1)]
    [int]$Phase = 1,
    [switch]$AllowNonNative,
    [switch]$IncludeObjects,
    [switch]$AllowWriteFixture,
    [switch]$Stop,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$SmokePath = Join-Path $RepoRoot "native_bridge\smoke.py"

if (-not (Test-Path -LiteralPath $SmokePath)) {
    throw "Native bridge smoke harness was not found at $SmokePath"
}

if (-not $HostName) {
    $HostName = if ($env:VW_MCP_HOST) { $env:VW_MCP_HOST } else { "127.0.0.1" }
}
if ($Port -eq 0) {
    $Port = if ($env:VW_MCP_PORT) { [int]$env:VW_MCP_PORT } else { 9877 }
}

if (Test-Path -LiteralPath $VenvPython) {
    $PythonCommand = $VenvPython
    $PythonArgs = @()
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonCommand = "py"
    $PythonArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonCommand = "python"
    $PythonArgs = @()
} else {
    throw "Python was not found. Run scripts\bootstrap-agent.ps1 first or install Python 3."
}

$Args = @(
    $SmokePath,
    "--host", $HostName,
    "--port", $Port,
    "--timeout", $TimeoutSeconds,
    "--ping-count", $PingCount,
    "--read-count", $ReadCount,
    "--phase", $Phase
)
if ($MaxPingMilliseconds -gt 0) { $Args += @("--max-ping-ms", $MaxPingMilliseconds) }
if ($MaxReadMilliseconds -gt 0) { $Args += @("--max-read-ms", $MaxReadMilliseconds) }
if ($AllowNonNative) { $Args += "--allow-non-native" }
if ($IncludeObjects) { $Args += "--include-objects" }
if ($AllowWriteFixture) { $Args += "--allow-write-fixture" }
if ($Stop) { $Args += "--stop" }
if ($Json) { $Args += "--json" }

& $PythonCommand @PythonArgs @Args
exit $LASTEXITCODE
