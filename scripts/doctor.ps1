<#
.SYNOPSIS
  Vectorworks MCP doctor: checks runtime, launcher, client registrations, stale
  installs, execution policy, measured MCP startup, native plug-in and bridge.

.DESCRIPTION
  Read-only unless -Fix is given. -Fix only (a) re-registers clients through
  scripts\register-clients.ps1 and (b) moves stale copies (old ~/repos clone,
  MSIX LocalCache runtimes, %LOCALAPPDATA%\VectorworksMCP) into
  <Root>\backups\stale-<time>\ - nothing is deleted.

  vectorworksctl doctor [-Json] [-Fix]   (also accepts --json / --fix)
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Root = "",
    [string]$CodexHome = "",
    [string]$ClaudeConfig = "",
    [string]$PluginsDir = "",
    [string]$VectorworksVersion = "",
    [switch]$Json,
    [switch]$Fix,
    [switch]$SkipStartup,
    [switch]$SkipBridge,
    [switch]$SkipLogs,
    [int]$StartupBudgetMs = 800,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest = @()
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "install-common.ps1")
foreach ($r in $Rest) {
    switch -Regex ($r) {
        '^(--json|/json)$' { $Json = $true }
        '^(--fix|/fix)$' { $Fix = $true }
        '^doctor$' { }
        default { Write-Warning "ignored argument: $r" }
    }
}
if ($Json) { $script:VwQuiet = $true }
if (-not $Root) { $Root = Get-DefaultRoot }
$Root = Get-FullPath $Root
$customCodex = [bool]$CodexHome
if (-not $CodexHome) { $CodexHome = Get-DefaultCodexHome }
if (-not $ClaudeConfig) { $ClaudeConfig = Get-DefaultClaudeConfig }
$launcher = Join-Path $Root "bin\vectorworks-mcp.cmd"
$contractTools = @("vw_status", "vw_read", "vw_capture", "vw_create", "vw_edit", "vw_apply", "vw_view", "vw_organize", "vw_document", "vw_script")

$checks = New-Object System.Collections.Generic.List[object]
function Add-Check([string]$Id, [string]$Status, [string]$Message, [string]$FixCmd = "", $Data = $null) {
    $c = [ordered]@{ id = $Id; status = $Status; message = $Message }
    if ($FixCmd) { $c.fix = $FixCmd }
    if ($null -ne $Data) { $c.data = $Data }
    $checks.Add($c) | Out-Null
    if (-not $Json) {
        $tag = @{ ok = "[ok]  "; warn = "[warn]"; fail = "[FAIL]"; info = "[info]" }[$Status]
        $color = @{ ok = "Green"; warn = "Yellow"; fail = "Red"; info = "Gray" }[$Status]
        Write-Host ("{0} {1,-22} {2}" -f $tag, $Id, $Message) -ForegroundColor $color
        if ($FixCmd) { Write-Host ("       fix: {0}" -f $FixCmd) -ForegroundColor DarkGray }
    }
}

$installCmd = "powershell -NoProfile -ExecutionPolicy Bypass -File <Vectorworks MCP folder>\install.ps1"
$nativeCmd = "close Vectorworks, then: $installCmd"
if (-not $Json) { Write-Host "Vectorworks MCP doctor  (root: $Root)" }

# ------------------------------------------------------------------ runtime
$receiptPath = Join-Path $Root "install-receipt.json"
$receipt = $null
if (Test-Path -LiteralPath $receiptPath) { try { $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json } catch { } }
if ($receipt -and $receipt.source_dir -and (Test-Path -LiteralPath (Join-Path $receipt.source_dir "install.ps1"))) {
    $installCmd = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $receipt.source_dir 'install.ps1')`""
    $nativeCmd = "close Vectorworks, then: $installCmd"
}
$runtimeId = $null
if (Test-Path -LiteralPath (Join-Path $Root "current.txt")) { $runtimeId = (Get-Content -LiteralPath (Join-Path $Root "current.txt") -TotalCount 1).Trim() }
$runtimeDir = if ($runtimeId) { Join-Path $Root "runtime\$runtimeId" } else { $null }
$venvPy = if ($runtimeDir) { Join-Path $runtimeDir "venv\Scripts\python.exe" } else { $null }

if (Test-IsMsixRedirectedPath $Root) { Add-Check "root" "fail" "Install root is inside an MSIX LocalCache: $Root" $installCmd }
elseif (-not (Test-Path -LiteralPath $Root)) { Add-Check "root" "fail" "Not installed: $Root does not exist" $installCmd }
else { Add-Check "root" "ok" $Root }

if (-not $runtimeId) { Add-Check "runtime" "fail" "current.txt missing - no active runtime" $installCmd }
elseif (-not (Test-Path -LiteralPath $venvPy)) { Add-Check "runtime" "fail" "runtime $runtimeId has no venv ($venvPy)" $installCmd }
elseif (-not (Test-Path -LiteralPath (Join-Path $runtimeDir "app\vectorworks_mcp\__main__.py"))) { Add-Check "runtime" "fail" "runtime $runtimeId has no server code" $installCmd }
else { Add-Check "runtime" "ok" "runtime $runtimeId ($runtimeDir)" }

if (-not $receipt) { Add-Check "receipt" "fail" "install-receipt.json missing or unreadable" $installCmd }
else {
    $bad = @(); $missing = @()
    if ($runtimeDir -and $receipt.app_files) {
        $appDir = Join-Path $runtimeDir "app"
        foreach ($p in $receipt.app_files.PSObject.Properties) {
            $f = Join-Path $appDir ($p.Name.Replace('/', '\'))
            if (-not (Test-Path -LiteralPath $f)) { $missing += $p.Name; continue }
            if ((Get-Sha256 $f) -ne $p.Value) { $bad += $p.Name }
        }
    }
    if ($receipt.runtime_id -ne $runtimeId) { Add-Check "receipt" "warn" "receipt is for runtime $($receipt.runtime_id) but current.txt says $runtimeId" $installCmd }
    elseif ($bad.Count -or $missing.Count) { Add-Check "receipt" "fail" "$($bad.Count) modified / $($missing.Count) missing runtime files (e.g. $((@($bad + $missing) | Select-Object -First 3) -join ', '))" $installCmd }
    else { Add-Check "receipt" "ok" "version $($receipt.version), $(@($receipt.app_files.PSObject.Properties).Count) files verified, installed $($receipt.installed_at)" }
    if ($receipt.python -and $receipt.python.base_executable -and -not (Test-Path -LiteralPath $receipt.python.base_executable)) {
        Add-Check "python" "fail" "base interpreter $($receipt.python.base_executable) is gone (venv is broken)" $installCmd
    } elseif ($receipt.python) { Add-Check "python" "ok" "$($receipt.python.base_executable) ($($receipt.python.version))" }
}

# ----------------------------------------------------------------- launcher
if (-not (Test-Path -LiteralPath $launcher)) { Add-Check "launcher" "fail" "launcher missing: $launcher" $installCmd }
else {
    $text = Get-Content -LiteralPath $launcher -Raw
    # Executable lines only (the install-command hint text mentions powershell).
    $execLines = @(($text -split "`r?`n") | Where-Object { $_ -notmatch '(?i)^\s*(rem\b|set\s|if not defined VW_MCP_INSTALL_COMMAND|>&2 echo)' })
    $okText = ($text -match '-m vectorworks_mcp') -and -not ($execLines -match '(?i)powershell|\bpip\b|\bgit\b')
    $repairOk = Test-Path -LiteralPath (Join-Path $Root "bin\repair_server.py")
    if (-not $okText) { Add-Check "launcher" "fail" "launcher content is not the 1.0 template" $installCmd }
    elseif (-not $repairOk) { Add-Check "launcher" "warn" "bin\repair_server.py missing (no repair mode)" $installCmd }
    elseif ($receipt -and $receipt.launcher -and (Get-Sha256 $launcher) -ne $receipt.launcher.sha256) { Add-Check "launcher" "warn" "launcher differs from the installed one (edited?)" $installCmd }
    else { Add-Check "launcher" "ok" "$launcher (plain .cmd -> python.exe, no PowerShell)" }
}

$pkg = Test-MsixProcess
if ($pkg) { Add-Check "msix" "warn" "doctor runs inside MSIX package $pkg - AppData paths may be virtualised; run from a normal PowerShell for accurate results" }

# -------------------------------------------------------------- auth token
$tokenPath = Join-Path $Root "auth-token"
if (-not (Test-Path -LiteralPath $tokenPath)) { Add-Check "auth_token" "fail" "auth-token missing" $installCmd }
else {
    $acl = Get-AclSummary $tokenPath
    $len = (Get-Content -LiteralPath $tokenPath -TotalCount 1).Trim().Length
    if ($len -lt 32) { Add-Check "auth_token" "fail" "auth-token too short ($len chars)" $installCmd }
    elseif (-not $acl.protected -or @($acl.broad_read).Count) { Add-Check "auth_token" "warn" "auth-token readable by others ($(@($acl.broad_read) -join ', '); inherits=$(-not $acl.protected))" $installCmd }
    else { Add-Check "auth_token" "ok" "present, private ACL" }
}

# --------------------------------------------------------- helper python
$py = $null
if ($receipt -and $receipt.python -and $receipt.python.base_executable -and (Test-Path -LiteralPath $receipt.python.base_executable)) { $py = [string]$receipt.python.base_executable }
if (-not $py) { $f = Find-BasePython; if ($f) { $py = $f.executable } }
function Invoke-Py([string]$Script, [string[]]$PyArgs, [int]$Timeout = 120) {
    if (-not $py) { return $null }
    $r = Invoke-Native -FilePath $py -Arguments (@("-I", "-X", "utf8", (Join-Path $PSScriptRoot $Script)) + $PyArgs) -TimeoutSec $Timeout
    try { return ($r.StdOut | ConvertFrom-Json) } catch { return $null }
}

# ----------------------------------------------------------------- clients
$insp = Invoke-Py "register_clients.py" @("inspect", "--codex-home", $CodexHome, "--claude-config", $ClaudeConfig, "--launcher", $launcher)
$regFix = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $PSScriptRoot 'register-clients.ps1')`" -Client all"
$needReg = $false
if (-not $insp) { Add-Check "clients" "warn" "could not inspect client configs (no Python 3.11+ found)" }
else {
    $cx = $insp.codex
    if (-not $cx.exists) { Add-Check "codex" "info" "Codex not configured ($($cx.config))" }
    else {
        $defs = @($cx.definitions)
        if ($defs.Count -eq 0) { Add-Check "codex" "fail" "no active vectorworks MCP server in Codex" $regFix; $needReg = $true }
        elseif ($defs.Count -gt 1) { Add-Check "codex" "fail" "$($defs.Count) active vectorworks definitions: $(($defs | ForEach-Object { $_.source }) -join ' | ')" $regFix (@($defs)); $needReg = $true }
        elseif (-not ($cx.PSObject.Properties["server"] -and $cx.server.points_at_launcher)) { Add-Check "codex" "fail" "[mcp_servers.vectorworks] does not point at $launcher" $regFix; $needReg = $true }
        else {
            $s = $cx.server
            $probs = @()
            if ([double]$s.startup_timeout_sec -lt 30) { $probs += "startup_timeout_sec=$($s.startup_timeout_sec)" }
            if ([double]$s.tool_timeout_sec -lt 180) { $probs += "tool_timeout_sec=$($s.tool_timeout_sec)" }
            if (-not $cx.mcp_optional_startup_grace_ms -or [int]$cx.mcp_optional_startup_grace_ms -lt 5000) { $probs += "mcp_optional_startup_grace_ms=$($cx.mcp_optional_startup_grace_ms)" }
            if ($probs.Count) { Add-Check "codex" "warn" "single registration, but $($probs -join ', ')" $regFix; $needReg = $true }
            else { Add-Check "codex" "ok" "one [mcp_servers.vectorworks] -> launcher; startup 30 s, tools 180 s, grace $($cx.mcp_optional_startup_grace_ms) ms" }
        }
        $mk = $cx.marketplace
        if ($mk -and $mk.source_type -eq "local" -and (Test-SamePath ([string]$mk.source).Replace('\\?\', '') (Join-Path $Root "marketplace"))) {
            $cache = Join-Path $CodexHome "plugins\cache\vectorworks-mcp\vectorworks"
            $vers = @(if (Test-Path -LiteralPath $cache) { Get-ChildItem -LiteralPath $cache -Directory | ForEach-Object { $_.Name } })
            if ($vers -contains "1.0.0") { Add-Check "codex_skills" "ok" "skills-only plugin vectorworks@vectorworks-mcp 1.0.0" }
            else { Add-Check "codex_skills" "warn" "local marketplace set, plugin cache has: $($vers -join ', ')" "codex plugin add vectorworks@vectorworks-mcp" }
        } else {
            Add-Check "codex_skills" "warn" "marketplace vectorworks-mcp is not the local 1.0 copy ($($mk.source))" $regFix
        }
    }
    $cl = $insp.claude
    # Synced Claude plugin 'vectorworks' >= 1.0 (github.com/BhaveshY/vectorworks-claude-plugin)
    # provides server + skill itself; then a user-scope server would be a duplicate.
    $synced = @($insp.stale | Where-Object { $_.kind -eq "claude-desktop-org-plugin" }) | Select-Object -First 1
    $syncedMajor = 0
    if ($synced) { [int]::TryParse(([string]$synced.version -split "\.")[0], [ref]$syncedMajor) | Out-Null }
    $viaPlugin = $synced -and $syncedMajor -ge 1
    if (-not $cl.exists -and -not $viaPlugin) { Add-Check "claude" "info" "Claude Code not configured ($($cl.config))" }
    elseif ($viaPlugin) {
        if ($cl.user) { Add-Check "claude" "warn" "synced plugin vectorworks $($synced.version) AND a user-scope vectorworks server: two servers" $regFix; $needReg = $true }
        else { Add-Check "claude" "ok" "synced plugin vectorworks $($synced.version) -> launcher (server + skill)" }
    }
    elseif (-not $cl.user) { Add-Check "claude" "fail" "no user-scope vectorworks MCP server in Claude Code" $regFix; $needReg = $true }
    elseif (-not $cl.user.points_at_launcher) { Add-Check "claude" "fail" "user-scope vectorworks points elsewhere: $($cl.user.command) $(@($cl.user.args) -join ' ')" $regFix; $needReg = $true }
    else { Add-Check "claude" "ok" "user-scope vectorworks -> launcher" }
    if ($viaPlugin) {
        if ($cl.skills_plugin_enabled) { Add-Check "claude_skills" "warn" "local skills plugin vectorworks@vectorworks-mcp duplicates the synced plugin's skill" $regFix; $needReg = $true }
    } elseif ($cl.exists -and $cl.PSObject.Properties["skills_plugin_enabled"]) {
        if ($cl.skills_plugin_enabled) { Add-Check "claude_skills" "ok" "skills plugin vectorworks@vectorworks-mcp enabled" }
        else { Add-Check "claude_skills" "warn" "skills plugin vectorworks@vectorworks-mcp not enabled in Claude Code" $regFix }
        foreach ($o in @($cl.other_vectorworks_plugins)) { Add-Check "claude_plugin" "warn" "other enabled vectorworks plugin: $o (0.x skills/server)" "claude plugin disable $o" }
    }
    foreach ($o in @($cl.project_overrides)) {
        Add-Check "claude_project" "warn" "project $($o.project) defines its own 'vectorworks' server (overrides the user one)" "cd `"$($o.project)`"; claude mcp remove -s local vectorworks"
    }
    foreach ($s in @($insp.stale)) {
        switch ($s.kind) {
            "claude-desktop-org-plugin" {
                $maj = 0; [int]::TryParse(([string]$s.version -split "\.")[0], [ref]$maj) | Out-Null
                if ($maj -ge 1) { continue }
                $st = if ($s.defines_mcp_server) { "warn" } else { "info" }
                Add-Check "stale_cowork" $st "Claude desktop org plugin 'vectorworks' $($s.version) (synced from claude.ai org; its skills reference 0.7 tools$(if ($s.defines_mcp_server) { ' and it starts a second vectorworks server' }))" "Restart Claude so it re-syncs the plugin from github.com/BhaveshY/vectorworks-claude-plugin (now 1.0), then rerun install.ps1 (or scripts\register-clients.ps1)."
            }
            "claude-code-plugin-cache" { Add-Check "stale_plugin" "info" "Claude Code plugin cache vectorworks $($s.version): $($s.path)" "claude plugin update vectorworks (or /plugin in Claude Code)" }
            "old-git-clone" { Add-Check "stale_clone" "warn" "old clone used by 0.x resolvers: $($s.path)" "vectorworksctl doctor -Fix   (moves it to $Root\backups)" }
            default { Add-Check "stale_runtime" "warn" "$($s.kind): $($s.path) ($(@($s.versions) -join ', '))" "vectorworksctl doctor -Fix   (moves it to $Root\backups)" }
        }
    }
}

# --------------------------------------------------------- execution policy
try {
    $pol = @(Get-ExecutionPolicy -List | ForEach-Object { [ordered]@{ scope = [string]$_.Scope; policy = [string]$_.ExecutionPolicy } })
    $gpo = @($pol | Where-Object { $_.scope -in @("MachinePolicy", "UserPolicy") -and $_.policy -notin @("Undefined", "Bypass", "Unrestricted", "RemoteSigned") })
    $summary = ($pol | ForEach-Object { "$($_.scope)=$($_.policy)" }) -join ", "
    if ($gpo.Count) { Add-Check "exec_policy" "warn" "Group Policy enforces $($gpo[0].policy): installer/doctor scripts need IT approval; the MCP launcher itself does not use PowerShell. ($summary)" "" $pol }
    else { Add-Check "exec_policy" "ok" $summary }
} catch { Add-Check "exec_policy" "info" "could not read: $($_.Exception.Message)" }

# ------------------------------------------------------------ MCP startup
if (-not $SkipStartup -and (Test-Path -LiteralPath $launcher) -and $py) {
    $st = Invoke-Py "doctor_probe.py" @("startup", "--launcher", $launcher, "--runs", "2", "--timeout", "25") 90
    if (-not $st) { Add-Check "mcp_startup" "fail" "startup probe failed to run" }
    elseif (-not $st.ok) { Add-Check "mcp_startup" "fail" "launcher did not answer initialize/tools/list (exit $($st.exit_code)) $($st.stderr_tail)" $installCmd $st }
    else {
        $first = $st.first_run.tools_list_ms
        $best = $st.tools_list_ms
        $names = @($st.tools)
        if ($st.repair_mode) { Add-Check "mcp_startup" "fail" "server runs in REPAIR MODE (runtime missing/broken)" $installCmd }
        else {
            $missingTools = @($contractTools | Where-Object { $names -notcontains $_ })
            $msg = "initialize $($st.initialize_ms) ms, tools/list $best ms (first run $first ms), $($names.Count) tools, $($st.tools_list_bytes) bytes"
            if ([int]$best -gt $StartupBudgetMs) { Add-Check "mcp_startup" "warn" "$msg - over the $StartupBudgetMs ms budget (Codex may skip the server at thread start)" "" $st }
            else { Add-Check "mcp_startup" "ok" $msg }
            if ($missingTools.Count) { Add-Check "mcp_tools" "warn" "tools/list lacks contract tools: $($missingTools -join ', ')" }
            else { Add-Check "mcp_tools" "ok" "all 10 contract tools listed" }
        }
    }
}

# ------------------------------------------------------------ native plug-in
$versions = if ($VectorworksVersion) { @($VectorworksVersion) } else { Get-InstalledVectorworksVersions }
$running = Get-VectorworksProcesses
if (@($versions).Count -eq 0 -and -not $PluginsDir) { Add-Check "native" "info" "no Vectorworks user folder found under %APPDATA%\Nemetschek\Vectorworks" }
foreach ($ver in @($versions)) {
    $pdir = if ($PluginsDir) { $PluginsDir } else { Join-Path $env:APPDATA "Nemetschek\Vectorworks\$ver\Plug-ins" }
    $vlb = Join-Path $pdir "VectorworksMCPBridge.vlb"
    $old = Join-Path $pdir "ObjectExample.vlb"
    $nativeFix = $nativeCmd
    if (Test-Path -LiteralPath $vlb) {
        $h = Get-Sha256 $vlb
        $want = $null
        if ($receipt -and $receipt.native -and $receipt.native.files) { $want = $receipt.native.files.'VectorworksMCPBridge.vlb' }
        if ($want -and $want -ne $h) { Add-Check "native_$ver" "warn" "VectorworksMCPBridge.vlb hash differs from the receipt" $nativeFix }
        else { Add-Check "native_$ver" "ok" "VectorworksMCPBridge.vlb sha256 $($h.Substring(0,12))... in $pdir" }
        if (-not (Test-Path -LiteralPath (Join-Path $pdir "VectorworksMCPBridge.vwr"))) { Add-Check "native_$ver" "fail" "VectorworksMCPBridge.vwr resource file missing" $nativeFix }
    } elseif (Test-Path -LiteralPath $old) {
        Add-Check "native_$ver" "warn" "only the pre-1.0 bridge (ObjectExample.vlb) is installed" $nativeFix
    } else { Add-Check "native_$ver" "fail" "no bridge plug-in in $pdir" $nativeFix }
    if ((Test-Path -LiteralPath $vlb) -and (Test-Path -LiteralPath $old)) { Add-Check "native_$ver" "warn" "old ObjectExample.vlb still present next to VectorworksMCPBridge.vlb (two bridges)" $nativeFix }
}
if ($running.Count) { Add-Check "vectorworks" "info" "running: $(($running | ForEach-Object { "$($_.ProcessName) pid $($_.Id)" } | Select-Object -First 3) -join ', ')" }
else { Add-Check "vectorworks" "info" "not running" }

# ------------------------------------------------------------------- bridge
if (-not $SkipBridge -and $py) {
    $br = Invoke-Py "doctor_probe.py" @("bridge", "--root", $Root) 60
    if ($br) {
        $live = @($br.instances | Where-Object { $_.alive })
        $dead = @($br.instances | Where-Object { -not $_.alive })
        if ($dead.Count) { Add-Check "discovery" "info" "$($dead.Count) stale discovery file(s) (dead PIDs; the host prunes them)" }
        if ($live.Count) { Add-Check "discovery" "ok" "$($live.Count) live instance(s): $(($live | ForEach-Object { "pid $($_.pid) port $($_.port) rev $($_.capability_revision)" }) -join '; ')" }
        elseif ($running.Count) { Add-Check "discovery" "warn" "Vectorworks runs but wrote no discovery file (pre-1.0 bridge or plug-in not loaded)" }
        foreach ($p in @($br.pings)) {
            if ($p.success) {
                $rev = $p.capability_revision
                $st = if ($rev -and [int]$rev -ge 6) { "ok" } else { "warn" }
                Add-Check "bridge_ping" $st "port $($p.port): $($p.ms) ms, rev $rev, bridge $($p.bridge_version), ui_state $($p.ui_state), doc '$($p.active_document)'" $(if ($st -eq "warn") { "bridge is pre-1.0 (capability_revision < 6): close Vectorworks and run install.ps1" } else { "" })
            } elseif ("$($p.error) $($p.error_code)" -match '(?i)auth') {
                Add-Check "bridge_ping" "warn" "port $($p.port): the bridge rejected this install's auth-token ($Root\auth-token)" "restart Vectorworks so the bridge reads the current token (it reads %USERPROFILE%\.vectorworks-mcp\auth-token at start-up)"
            } elseif ($running.Count) {
                Add-Check "bridge_ping" "warn" "port $($p.port): $($p.error)" "start Vectorworks with the bridge plug-in loaded"
            } else { Add-Check "bridge_ping" "info" "port $($p.port): no bridge (Vectorworks not running)" }
        }
    }
}

# ------------------------------------------------------------- Codex logs
if (-not $SkipLogs -and $py -and (Test-Path -LiteralPath $CodexHome)) {
    $lg = Invoke-Py "register_clients.py" @("codex-logs", "--codex-home", $CodexHome, "--days", "7") 60
    if ($lg -and @($lg.databases).Count) {
        $c = $lg.counts
        $get = { param($n) if ($c -and $c.PSObject.Properties[$n]) { [int]$c.$n } else { 0 } }
        $omitted = & $get "omitted_pending"
        $nowait = & $get "cached_catalog_without_wait"
        $ps = & $get "powershell_launcher"
        $init = & $get "initialized"
        $msg = "last 7 days: $init starts, $omitted omitted at thread start (grace too short / slow start), $nowait turns without waiting, $ps PowerShell-launcher starts, $($lg.errors) errors"
        $status = if ($omitted -gt 0 -or $lg.errors -gt 0) { "warn" } else { "ok" }
        Add-Check "codex_logs" $status $msg "" $lg
    } elseif ($lg -and $lg.PSObject.Properties["read_errors"]) { Add-Check "codex_logs" "info" "Codex logs not readable: $(@($lg.read_errors) -join '; ')" }
}

# -------------------------------------------------------------------- fixes
$fixed = @()
if ($Fix) {
    if ($needReg -and (Test-Path -LiteralPath $launcher)) {
        $a = @{ Client = "all"; Root = $Root; Launcher = $launcher }
        if ($customCodex) { $a.CodexHome = $CodexHome }
        & (Join-Path $PSScriptRoot "register-clients.ps1") @a
        $fixed += "re-registered clients"
    }
    if ($insp -and -not (Test-SamePath $Root (Get-DefaultRoot))) {
        $fixed += "stale copies left alone: -Root is not the default install root (sandbox)"
    } elseif ($insp) {
        $stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
        foreach ($s in @($insp.stale)) {
            if ($s.kind -in @("old-git-clone", "msix-localcache-runtime", "old-localappdata-runtime") -and (Test-Path -LiteralPath $s.path)) {
                $dest = Join-Path $Root "backups\stale-$stamp\$($s.kind)"
                New-Item -ItemType Directory -Path (Split-Path -Parent $dest) -Force | Out-Null
                try { Move-Item -LiteralPath $s.path -Destination $dest; $fixed += "moved $($s.path) -> $dest" }
                catch { $fixed += "could not move $($s.path): $($_.Exception.Message)" }
            }
        }
    }
    foreach ($f in $fixed) { if (-not $Json) { Write-Host "  fixed: $f" -ForegroundColor Cyan } }
}

$fails = @($checks | Where-Object { $_.status -eq "fail" }).Count
$warns = @($checks | Where-Object { $_.status -eq "warn" }).Count
$result = [ordered]@{ ok = ($fails -eq 0); root = $Root; launcher = $launcher; failures = $fails; warnings = $warns; checks = $checks; fixed = $fixed }
if ($Json) { $result | ConvertTo-Json -Depth 10 }
else {
    Write-Host ""
    if ($fails) { Write-Host "$fails problem(s), $warns warning(s). Run the 'fix' commands above (or vectorworksctl doctor -Fix for registration/stale copies)." -ForegroundColor Red }
    elseif ($warns) { Write-Host "Working, with $warns warning(s)." -ForegroundColor Yellow }
    else { Write-Host "All checks passed." -ForegroundColor Green }
}
if ($fails) { exit 1 }
