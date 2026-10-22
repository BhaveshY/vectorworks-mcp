<#
.SYNOPSIS
  Read-only requirement check before installing Vectorworks MCP (for people and AI agents).

.DESCRIPTION
  Changes nothing. Prints one line per check, or with -Json one object:
    { ready: bool, blockers: [ids], checks: [{id, status: pass|warn|fail, detail, fix}], install_command }
  "fail" blocks the install; "warn" means the install works but something needs attention.
  Agents: follow AGENT_INSTALL.md - fix every "fail" with its "fix", rerun, then install.

  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1 [-Json] [-VectorworksVersion 2024]
#>
param(
    [string]$VectorworksVersion = "",
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "install-common.ps1")
$script:VwQuiet = $true

$Supported = @("2024", "2025")
$checks = New-Object System.Collections.Generic.List[object]
function Add-Check([string]$Id, [string]$Status, [string]$Detail, [string]$Fix = "") {
    $checks.Add([ordered]@{ id = $Id; status = $Status; detail = $Detail; fix = $Fix })
}

# ------------------------------------------------------------------ system
$os = [Environment]::OSVersion.Version
if (-not [Environment]::Is64BitOperatingSystem) { Add-Check "os" "fail" "32-bit Windows is not supported" "Use 64-bit Windows 10/11." }
elseif ($os.Major -lt 10) { Add-Check "os" "fail" "Windows $os is too old" "Use Windows 10 or 11 (x64)." }
else { Add-Check "os" "pass" "Windows $($os.Major).$($os.Minor) build $($os.Build) x64" }

$pkg = Test-MsixProcess
if ($pkg) {
    Add-Check "msix" "warn" "this shell runs inside the MSIX package '$pkg' (e.g. the Codex desktop app): AppData writes are virtualised, so the installer refuses to write the Vectorworks plug-in from here" `
        "Run the installer through scripts\run-outside-msix.ps1 (it starts it via explorer.exe outside the package and returns its JSON), or ask the user to run it in a normal PowerShell window."
} else { Add-Check "msix" "pass" "normal (unpackaged) process" }

$gpo = @()
foreach ($scope in @("MachinePolicy", "UserPolicy")) {
    $p = [string](Get-ExecutionPolicy -Scope $scope)
    if ($p -and $p -ne "Undefined") { $gpo += "${scope}=$p" }
}
if ($gpo | Where-Object { $_ -match 'AllSigned|Restricted' }) {
    Add-Check "execution_policy" "fail" "Group Policy enforces $($gpo -join ', '); -ExecutionPolicy Bypass cannot override it" "Ask IT to allow running install.ps1 (the MCP launcher itself is a .cmd and needs no PowerShell)."
} else { Add-Check "execution_policy" "pass" "install.ps1 can run with -ExecutionPolicy Bypass$(if ($gpo) { " ($($gpo -join ', '))" })" }

$free = (Get-PSDrive -Name ($env:USERPROFILE.Substring(0, 1))).Free
if ($free -lt 500MB) { Add-Check "disk" "fail" ("{0:N0} MB free on the profile drive" -f ($free / 1MB)) "Free at least 500 MB." }
else { Add-Check "disk" "pass" ("{0:N1} GB free" -f ($free / 1GB)) }

# ------------------------------------------------------------------ python
$base = Find-BasePython
$wingetFix = "winget install --id Python.Python.3.12 -e --scope user --accept-source-agreements --accept-package-agreements   (then open a new shell and rerun preflight)"
if (-not $base) {
    Add-Check "python" "fail" "no python.org Python 3.11+ found (Codex's bundled Python and the Microsoft Store stub are never used)" $wingetFix
} elseif (-not $base.version.StartsWith("3.12")) {
    Add-Check "python" "warn" "Python $($base.version) at $($base.executable) will be used; 3.12 is the tested version" $wingetFix
} else { Add-Check "python" "pass" "Python $($base.version): $($base.executable)" }
if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue) -and -not $base) {
    Add-Check "winget" "warn" "winget is not available for the Python fix" "Download the Python 3.12 Windows installer (64-bit) from https://www.python.org/downloads/windows/ and install it for the current user."
}

# ------------------------------------------------------------- vectorworks
$userVers = @(Get-InstalledVectorworksVersions)
$progVers = @()
foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if (-not $root) { continue }
    $progVers += @(Get-ChildItem -LiteralPath $root -Directory -Filter "Vectorworks*" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '(20\d\d)' } | ForEach-Object { [regex]::Match($_.Name, '20\d\d').Value })
}
$allVers = @(@($userVers) + @($progVers) | Sort-Object -Unique -Descending)
$targets = if ($VectorworksVersion) { @($VectorworksVersion) } else { @($allVers | Where-Object { $Supported -contains $_ }) }
if ($VectorworksVersion -and $Supported -notcontains $VectorworksVersion) {
    Add-Check "vectorworks" "fail" "Vectorworks $VectorworksVersion is not supported (supported: $($Supported -join ', '))" "Stop and tell the user: Vectorworks $VectorworksVersion needs a new native build against its SDK (scripts\build-native-bridge.ps1) and live testing."
    $targets = @()
} elseif ($allVers.Count -eq 0) {
    Add-Check "vectorworks" "fail" "no Vectorworks installation found (Program Files or %APPDATA%\Nemetschek\Vectorworks)" "Install Vectorworks 2024 or 2025 first. This connector cannot work without it."
} elseif ($targets.Count -eq 0) {
    Add-Check "vectorworks" "fail" "found Vectorworks $($allVers -join ', '); supported: $($Supported -join ', ')" "Install Vectorworks 2024 or 2025, or stop: other versions need a new native build (scripts\build-native-bridge.ps1 with that SDK)."
} elseif (@($targets | Where-Object { $allVers -notcontains $_ }).Count) {
    Add-Check "vectorworks" "fail" "Vectorworks $($targets -join ', ') is not installed (found: $($allVers -join ', '))" "Install that version first, or omit -VectorworksVersion to use the installed one."
} else {
    $notRun = @($targets | Where-Object { $userVers -notcontains $_ })
    $detail = "found Vectorworks $($allVers -join ', '); will install for $($targets -join ', ')"
    if ($notRun.Count) {
        Add-Check "vectorworks" "warn" "$detail; $($notRun -join ', ') has never been started (no %APPDATA%\Nemetschek\Vectorworks\<ver> user folder yet)" "Ask the user to start Vectorworks $($notRun -join ', ') once (licence/login, close the start dialogs), quit it, then rerun preflight."
    } else { Add-Check "vectorworks" "pass" $detail }
    $untested = @($targets | Where-Object { $_ -ne "2024" })
    if ($untested.Count) { Add-Check "vectorworks_tested" "warn" "Vectorworks $($untested -join ', ') is built but not yet live-tested with this connector" "Install anyway; the smoke test (scripts\smoke_installed.py) is the acceptance check. Report its result." }
}

# ------------------------------------------------------------ native plug-in
$pbPath = Join-Path $Repo "native\PREBUILT.json"
$pb = $null
$pluginWrites = @()
if (Test-Path -LiteralPath $pbPath) { try { $pb = Get-Content -LiteralPath $pbPath -Raw | ConvertFrom-Json } catch { } }
foreach ($ver in $targets) {
    $dir = Join-Path $Repo "native\$ver"
    $have = (Test-Path -LiteralPath (Join-Path $dir "VectorworksMCPBridge.vlb")) -and (Test-Path -LiteralPath (Join-Path $dir "VectorworksMCPBridge.vwr"))
    if (-not $have) {
        Add-Check "plugin_$ver" "fail" "no prebuilt plug-in in native\$ver" "Clone github.com/BhaveshY/vectorworks-mcp again or git pull (native\$ver\ is tracked), or build it: install.ps1 -FullNative (needs Visual Studio C++ tools + the Vectorworks $ver SDK in third_party\)."
        continue
    }
    $e = if ($pb) { $pb.$ver } else { $null }
    if ($e -and (Get-Sha256 (Join-Path $dir "VectorworksMCPBridge.vlb")) -ne $e.vlb_sha256) {
        Add-Check "plugin_$ver" "fail" "native\$ver\VectorworksMCPBridge.vlb does not match native\PREBUILT.json (corrupt or partial download)" "Download the repository again (git clone, or the main-branch zip)."
    } else {
        $installed = Join-Path $env:APPDATA "Nemetschek\Vectorworks\$ver\Plug-ins\VectorworksMCPBridge.vlb"
        if ($e -and (Test-Path -LiteralPath (Join-Path $Repo ".git")) -and (Get-Command git.exe -ErrorAction SilentlyContinue)) {
            $tree = (& git.exe -C $Repo rev-parse HEAD:native_bridge/src 2>$null)
            if ($tree -and "$tree".Trim() -ne $e.native_src_tree) { Add-Check "plugin_${ver}_age" "warn" "the prebuilt plug-in ($($e.build_id)) is older than native_bridge\src in this checkout" "Maintainers: rebuild and run scripts\update-prebuilt.ps1. Installing the prebuilt still works." }
        }
        $same = $e -and (Test-Path -LiteralPath $installed) -and (Get-Sha256 $installed) -eq $e.vlb_sha256
        if (-not $same) { $pluginWrites += $ver }
        $state = if (-not (Test-Path -LiteralPath $installed)) { "not installed yet" } elseif ($same) { "already installed (same build)" } else { "an older build is installed (will be replaced)" }
        Add-Check "plugin_$ver" "pass" "prebuilt $(if ($e) { $e.build_id } else { 'plug-in' }) available; $state"
    }
}

if (@(Get-VectorworksProcesses).Count -eq 0) { Add-Check "vectorworks_running" "pass" "Vectorworks is not running" }
elseif ($pluginWrites.Count -eq 0) { Add-Check "vectorworks_running" "pass" "Vectorworks is running, but the plug-in is already up to date (nothing to write)" }
else {
    Add-Check "vectorworks_running" "warn" "Vectorworks is running; the plug-in for $($pluginWrites -join ', ') can only be written while it is closed" "Ask the user to SAVE their work and quit Vectorworks (never close it for them), then run the install. install.ps1 -WaitForVectorworksExit -WaitTimeoutSec 300 waits up to 5 min."
}

# ----------------------------------------------------------------- clients
$codex = Get-Command codex.exe, codex.cmd, codex -ErrorAction SilentlyContinue | Select-Object -First 1
$codexHome = Get-DefaultCodexHome
$claude = Get-Command claude.exe, claude.cmd, claude -ErrorAction SilentlyContinue | Select-Object -First 1
$claudeJson = Get-DefaultClaudeConfig
$haveCodex = [bool]$codex -or (Test-Path -LiteralPath $codexHome)
$haveClaude = [bool]$claude -or (Test-Path -LiteralPath $claudeJson)
$names = @()
if ($haveCodex) { $names += "Codex ($(if ($codex) { $codex.Source } else { $codexHome }))" }
if ($haveClaude) { $names += "Claude Code ($(if ($claude) { $claude.Source } else { $claudeJson }))" }
if ($names.Count) { Add-Check "clients" "pass" ("will register: " + ($names -join "; ")) }
else { Add-Check "clients" "warn" "neither Codex nor Claude Code was found; the server installs but no client is registered" "Install Codex or Claude Code, then rerun: scripts\register-clients.ps1 -Client all" }

# ----------------------------------------------------------------- network
$wheels = Test-Path -LiteralPath (Join-Path $Repo "wheels")
try {
    $null = Invoke-WebRequest -Uri "https://pypi.org/simple/pip/" -Method Head -UseBasicParsing -TimeoutSec 10
    Add-Check "network" "pass" "pypi.org reachable (dependencies are installed with pip --require-hashes)"
} catch {
    if ($wheels) { Add-Check "network" "pass" "pypi.org not reachable, but wheels\ is present (offline install)" }
    else { Add-Check "network" "fail" "pypi.org is not reachable: $($_.Exception.Message)" "Allow HTTPS to pypi.org / files.pythonhosted.org (proxy: set HTTPS_PROXY), or (no internet at all) use an offline zip from package-release.ps1 -IncludeWheels and install with -Offline." }
}

# ---------------------------------------------------------------- existing
$root = Get-DefaultRoot
$receiptPath = Join-Path $root "install-receipt.json"
if (Test-Path -LiteralPath $receiptPath) {
    try { $rc = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json; Add-Check "existing_install" "pass" "Vectorworks MCP $($rc.version) is installed at $root (the installer upgrades/repairs in place)" }
    catch { Add-Check "existing_install" "warn" "unreadable $receiptPath" "The installer treats it as a fresh install." }
} else { Add-Check "existing_install" "pass" "no previous install at $root" }

# ------------------------------------------------------------------ result
$blockers = @($checks | Where-Object { $_.status -eq "fail" } | ForEach-Object { $_.id })
$ps = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $Repo 'install.ps1')`" -WaitForVectorworksExit -WaitTimeoutSec 300 -Json"
$cmd = if ($pkg) { "powershell -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $Repo 'scripts\run-outside-msix.ps1')`"" } else { $ps }
$result = [ordered]@{ ready = ($blockers.Count -eq 0); blockers = $blockers; checks = $checks; install_command = $cmd; repo = $Repo }
if ($Json) { $result | ConvertTo-Json -Depth 5; exit $(if ($result.ready) { 0 } else { 1 }) }
foreach ($c in $checks) {
    $tag = switch ($c.status) { "pass" { "[ ok ]" } "warn" { "[warn]" } default { "[FAIL]" } }
    Write-Host "$tag $($c.id): $($c.detail)"
    if ($c.fix -and $c.status -ne "pass") { Write-Host "       fix: $($c.fix)" }
}
Write-Host ""
if ($result.ready) { Write-Host "Ready. Install with:`n  $cmd" } else { Write-Host "Not ready - fix: $($blockers -join ', ')" }
exit $(if ($result.ready) { 0 } else { 1 })
