<#
.SYNOPSIS
  Development helper: restart Vectorworks for live bridge testing.
.DESCRIPTION
  1. If Vectorworks runs, asks the bridge for the open documents and refuses to
     continue if any document is saved to disk or has a path (your real work),
     unless -Force. Untitled test documents are closed WITHOUT saving.
  2. Quits Vectorworks, relaunches it through explorer.exe (outside the calling
     process tree, which avoids licensing failures when started from an agent).
  3. If the network-licence "Login Settings" dialog appears, presses its Login
     button (no credentials are typed; the dialog only selects the licence server).
  4. Waits for the bridge discovery file and prints it.
  Never use this on a PC where someone is working in Vectorworks.
#>
[CmdletBinding()]
param(
    [string]$Exe = "C:\Program Files\Vectorworks 2024\Vectorworks2024.exe",
    [int]$TimeoutSec = 180,
    [switch]$Force,
    [switch]$NoLaunch
)
$ErrorActionPreference = "Stop"
$State = Join-Path $env:USERPROFILE ".vectorworks-mcp"
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @"
using System;using System.Runtime.InteropServices;
public static class VwDevWin { [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l); }
"@

function Invoke-Bridge([string]$Action, [hashtable]$Params = @{}) {
    $token = (Get-Content -Raw (Join-Path $State "auth-token")).Trim()
    $body = [Text.Encoding]::UTF8.GetBytes((@{ id = "dev"; action = $Action; auth_token = $token; params = $Params } | ConvertTo-Json -Compress))
    $client = New-Object Net.Sockets.TcpClient("127.0.0.1", 9877)
    try {
        $stream = $client.GetStream(); $stream.ReadTimeout = 60000
        $len = [BitConverter]::GetBytes([uint32]$body.Length); [Array]::Reverse($len)
        $stream.Write($len, 0, 4); $stream.Write($body, 0, $body.Length)
        $hdr = New-Object byte[] 4; $got = 0; while ($got -lt 4) { $got += $stream.Read($hdr, $got, 4 - $got) }
        [Array]::Reverse($hdr); $n = [BitConverter]::ToUInt32($hdr, 0)
        $buf = New-Object byte[] $n; $got = 0; while ($got -lt $n) { $got += $stream.Read($buf, $got, $n - $got) }
        return ([Text.Encoding]::UTF8.GetString($buf) | ConvertFrom-Json)
    } finally { $client.Close() }
}

$running = Get-Process Vectorworks2024 -ErrorAction SilentlyContinue | Sort-Object WorkingSet64 -Descending | Select-Object -First 1
if ($running) {
    $docs = @()
    try { $docs = @((Invoke-Bridge "list_documents").result.documents) } catch { if (-not $Force) { throw "Bridge not reachable; cannot verify open documents. Use -Force only if no real work is open." } }
    $real = @($docs | Where-Object { $_.path })
    if ($real.Count -and -not $Force) { throw "Refusing: saved documents are open: $($real.name -join ', '). Close them yourself or pass -Force." }
    foreach ($d in $docs) {
        try { Invoke-Bridge "close_document" @{ fingerprint = $d.fingerprint; discard_changes = "discard" } | Out-Null } catch { }
    }
    $null = $running.CloseMainWindow()
    if (-not $running.WaitForExit(60000)) { throw "Vectorworks did not exit (a dialog may be open)." }
    Write-Host "Vectorworks closed."
}
if ($NoLaunch) { return }

Start-Process explorer.exe -ArgumentList ('"' + $Exe + '"')
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$loginPressed = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $p = Get-Process Vectorworks2024 -ErrorAction SilentlyContinue | Sort-Object WorkingSet64 -Descending | Select-Object -First 1
    if (-not $p) { continue }
    if (-not $loginPressed) {
        $root = [System.Windows.Automation.AutomationElement]::RootElement
        $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $p.Id)
        $login = $root.FindAll("Children", $cond) | Where-Object { $_.Current.Name -eq "Login Settings" } | Select-Object -First 1
        if ($login) {
            [VwDevWin]::PostMessage([IntPtr]$login.Current.NativeWindowHandle, 0x0111, [IntPtr]1, [IntPtr]::Zero) | Out-Null
            $loginPressed = $true
            Write-Host "Pressed Login on the licence dialog."
        }
    }
    # The bridge loads before the licence dialog appears, so "discovery file
    # exists" is not enough: wait until the UI is past licensing (idle, no
    # document, or a Home/update dialog the bridge dismisses by itself).
    $file = Join-Path $State ("instances\" + $p.Id + ".json")
    if (Test-Path $file) {
        try {
            $ping = (Invoke-Bridge "ping").result
            $kind = if ($ping.modal_dialog) { $ping.modal_dialog.kind } else { "" }
            if ($ping.ui_state -in @("idle", "no_document") -or $kind -in @("home", "update_available")) {
                Get-Content -Raw $file
                Write-Host "ui_state=$($ping.ui_state) dialog=$kind"
                return
            }
        } catch { }
    }
}
throw "Bridge did not come up within $TimeoutSec s."
