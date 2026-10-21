<#
.SYNOPSIS
  Runs install.ps1 OUTSIDE the calling app's MSIX package and returns its JSON result.

.DESCRIPTION
  Shells of packaged apps (e.g. the Codex desktop app) get AppData writes virtualised, so
  install.ps1 refuses to write the Vectorworks plug-in from them. This helper writes a small
  .cmd under %USERPROFILE%\.vectorworks-mcp\relay\ (a real, non-virtualised folder), starts it
  through explorer.exe (the user's desktop shell, outside the package; a console window opens),
  waits for it and prints install.ps1's JSON. Exit code = the installer's exit code
  (124 = timed out, the installer may still be running - check the window / relay folder).

  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-outside-msix.ps1 [-TimeoutSec 900] [-InstallArgs "-WaitForVectorworksExit -WaitTimeoutSec 300"]
  Default installer arguments: -WaitForVectorworksExit -WaitTimeoutSec 300
#>
param(
    [int]$TimeoutSec = 900,
    # install.ps1 arguments as ONE string, e.g. "-DryRun -SkipNative"; -Json is always added.
    [string]$InstallArgs = "-WaitForVectorworksExit -WaitTimeoutSec 300"
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "install-common.ps1")

$argList = @($InstallArgs -split '\s+' | Where-Object { $_ })
if ($argList -notcontains "-Json") { $argList += "-Json" }
foreach ($a in $argList) { if ($a -match '[&|<>^%"]') { throw "unsupported character in installer argument: $a" } }

$relay = Join-Path (Get-DefaultRoot) ("relay\" + (Get-Date).ToString("yyyyMMdd-HHmmss"))
Assert-RealFilesystemPath $relay "Relay folder"
New-Item -ItemType Directory -Path $relay -Force | Out-Null
$out = Join-Path $relay "out.json"; $err = Join-Path $relay "err.txt"; $code = Join-Path $relay "exit.txt"
$ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$cmd = Join-Path $relay "run.cmd"
$text = "@echo off`r`n" +
    "title Vectorworks MCP installer - do not close`r`n" +
    "echo Installing Vectorworks MCP (started by an agent). This window closes by itself.`r`n" +
    "`"$ps`" -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $Repo 'install.ps1')`" $($argList -join ' ') > `"$out`" 2> `"$err`"`r`n" +
    "> `"$code`" echo %ERRORLEVEL%`r`n"
[System.IO.File]::WriteAllText($cmd, $text, (New-Object System.Text.ASCIIEncoding))

Start-Process -FilePath (Join-Path $env:SystemRoot "explorer.exe") -ArgumentList ('"' + $cmd + '"')
$deadline = (Get-Date).AddSeconds($TimeoutSec)
while (-not (Test-Path -LiteralPath $code) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
if (-not (Test-Path -LiteralPath $code)) {
    [ordered]@{ ok = $false; error = "installer did not finish within $TimeoutSec s"; relay = $relay } | ConvertTo-Json
    exit 124
}
Start-Sleep -Milliseconds 200
$rc = [int]((Get-Content -LiteralPath $code -TotalCount 1).Trim())
$json = if (Test-Path -LiteralPath $out) { Get-Content -LiteralPath $out -Raw } else { "" }
if ($json.Trim()) { $json.Trim() } else { [ordered]@{ ok = $false; exit_code = $rc; stderr = (Get-Content -LiteralPath $err -Raw -ErrorAction SilentlyContinue); relay = $relay } | ConvertTo-Json }
exit $rc
