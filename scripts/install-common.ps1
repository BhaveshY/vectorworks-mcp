# Shared helpers for install.ps1, register-clients.ps1, doctor.ps1 and
# package-release.ps1. Dot-source it: . "$PSScriptRoot\install-common.ps1"
# Windows PowerShell 5.1 compatible. No side effects on load.


$script:VwProduct = "vectorworks-mcp"
$script:VwLogFile = $null
$script:VwQuiet = $false
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

# Files and folders that make up a runtime (and a release package). Paths are
# relative to the source root. Directories are copied recursively without
# __pycache__ / *.pyc. Missing optional entries are skipped.
$script:VwRuntimeRequired = @(
    "vectorworks_mcp",
    "requirements.lock",
    "install.ps1",
    "scripts\install-common.ps1",
    "scripts\register-clients.ps1",
    "scripts\register_clients.py",
    "scripts\doctor.ps1",
    "scripts\doctor_probe.py",
    "bin\vectorworks-mcp.cmd.template",
    "bin\vectorworksctl.cmd.template",
    "plugins\vectorworks",
    ".agents\plugins\marketplace.json",
    ".claude-plugin\marketplace.json"
)
$script:VwRuntimeOptional = @(
    "legacy",
    "server.py",
    "pyproject.toml",
    "requirements.txt",
    "README.md",
    "INSTALL.md",
    "AGENT_INSTALL.md",
    "AGENTS.md",
    "scripts\preflight.ps1",
    "scripts\run-outside-msix.ps1",
    "scripts\smoke_installed.py",
    "docs\V1_CONTRACT.md",
    "docs\PARITY.md",
    "scripts\package-release.ps1",
    "scripts\test-repair-server.py",
    "scripts\check-skill-tool-names.py",
    "native"
)

function Get-FullPath([string]$Path) {
    return [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Path))
}

function Test-SamePath([string]$A, [string]$B) {
    if (-not $A -or -not $B) { return $false }
    return [string]::Equals((Get-FullPath $A).TrimEnd('\'), (Get-FullPath $B).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
}

function Get-DefaultRoot {
    $profileDir = $env:USERPROFILE
    if (-not $profileDir) { $profileDir = [Environment]::GetFolderPath("UserProfile") }
    return (Join-Path $profileDir ".vectorworks-mcp")
}

function Get-DefaultCodexHome {
    if ($env:CODEX_HOME) { return (Get-FullPath $env:CODEX_HOME) }
    return (Join-Path $env:USERPROFILE ".codex")
}

function Get-DefaultClaudeConfig {
    if ($env:CLAUDE_CONFIG_DIR) { return (Join-Path (Get-FullPath $env:CLAUDE_CONFIG_DIR) ".claude.json") }
    return (Join-Path $env:USERPROFILE ".claude.json")
}

function Start-VwLog([string]$Root, [string]$Name, [switch]$DryRun) {
    if ($DryRun) { $script:VwLogFile = $null; return }
    $logs = Join-Path $Root "logs"
    if (-not (Test-Path -LiteralPath $logs)) { New-Item -ItemType Directory -Path $logs -Force | Out-Null }
    $stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
    $script:VwLogFile = Join-Path $logs "$Name-$stamp.log"
    [System.IO.File]::WriteAllText($script:VwLogFile, "", $script:Utf8NoBom)
}

function Write-VwLog([string]$Message) {
    if ($script:VwLogFile) {
        $line = "[{0}] {1}{2}" -f (Get-Date).ToString("s"), $Message, [Environment]::NewLine
        [System.IO.File]::AppendAllText($script:VwLogFile, $line, $script:Utf8NoBom)
    }
}

function Write-Step([string]$Message, [string]$Level = "info") {
    Write-VwLog "$Level $Message"
    if ($script:VwQuiet) { return }
    switch ($Level) {
        "ok" { Write-Host "  [ok]   $Message" -ForegroundColor Green }
        "warn" { Write-Host "  [warn] $Message" -ForegroundColor Yellow }
        "fail" { Write-Host "  [fail] $Message" -ForegroundColor Red }
        "plan" { Write-Host "  [dry]  $Message" -ForegroundColor Cyan }
        "head" { Write-Host ""; Write-Host "== $Message" }
        default { Write-Host "  $Message" }
    }
}

function Write-Utf8File([string]$Path, [string]$Content) {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $tmp = "$Path.tmp-$([Guid]::NewGuid().ToString('N').Substring(0,8))"
    [System.IO.File]::WriteAllText($tmp, $Content, $script:Utf8NoBom)
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Write-JsonFile([string]$Path, $Object) {
    Write-Utf8File $Path (($Object | ConvertTo-Json -Depth 12) + "`n")
}

function Get-OemEncoding {
    $cp = [System.Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage
    try { return [System.Text.Encoding]::GetEncoding($cp) } catch { return [System.Text.Encoding]::ASCII }
}

# Batch files are read by cmd.exe in the OEM code page and must use CRLF.
function Write-BatchFile([string]$Path, [string]$Content) {
    $normalized = ($Content -replace "`r`n", "`n") -replace "`n", "`r`n"
    $enc = Get-OemEncoding
    if (Test-Path -LiteralPath $Path) {
        $existing = [System.IO.File]::ReadAllBytes($Path)
        $wanted = $enc.GetBytes($normalized)
        if ([System.Linq.Enumerable]::SequenceEqual([byte[]]$existing, [byte[]]$wanted)) { return $false }
    }
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $tmp = "$Path.tmp-$([Guid]::NewGuid().ToString('N').Substring(0,8))"
    [System.IO.File]::WriteAllBytes($tmp, $enc.GetBytes($normalized))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
    return $true
}

function Get-Sha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-TreeHashes([string]$Root) {
    $result = [ordered]@{}
    if (-not (Test-Path -LiteralPath $Root)) { return $result }
    $rootFull = (Get-FullPath $Root).TrimEnd('\') + '\'
    Get-ChildItem -LiteralPath $Root -Recurse -File -Force |
        Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.Extension -ne '.pyc' } |
        Sort-Object FullName |
        ForEach-Object {
            $rel = $_.FullName.Substring($rootFull.Length).Replace('\', '/')
            $result[$rel] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    return $result
}

function Test-MsixProcess {
    # True when this process runs with an MSIX package identity (e.g. launched
    # from the Codex desktop app). Writes to AppData are then redirected into
    # %LOCALAPPDATA%\Packages\<pkg>\LocalCache and are invisible to other apps.
    try {
        if (-not ("VwMcp.PackageProbe" -as [type])) {
            Add-Type -Namespace VwMcp -Name PackageProbe -MemberDefinition @'
[DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
public static extern int GetCurrentPackageFullName(ref int packageFullNameLength, System.Text.StringBuilder packageFullName);
'@ -ErrorAction Stop
        }
        $len = 0
        $rc = [VwMcp.PackageProbe]::GetCurrentPackageFullName([ref]$len, $null)
        # 15700 = APPMODEL_ERROR_NO_PACKAGE; 122 = ERROR_INSUFFICIENT_BUFFER (has a package)
        if ($rc -eq 122 -and $len -gt 0) {
            $sb = New-Object System.Text.StringBuilder $len
            [void][VwMcp.PackageProbe]::GetCurrentPackageFullName([ref]$len, $sb)
            return $sb.ToString()
        }
        return $null
    } catch {
        return $null
    }
}

function Test-IsMsixRedirectedPath([string]$Path) {
    $full = Get-FullPath $Path
    $packages = Join-Path $env:LOCALAPPDATA "Packages"
    return $full.StartsWith($packages.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or ($full -match '\\Packages\\[^\\]+\\LocalCache\\')
}

function Assert-RealFilesystemPath([string]$Path, [string]$Label) {
    if (Test-IsMsixRedirectedPath $Path) {
        throw "$Label resolves into an MSIX package LocalCache ($Path). That location is private to one app. Run the installer from a normal PowerShell window and use the default path under %USERPROFILE%."
    }
}

function Set-PrivateFileAcl([string]$Path) {
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = New-Object System.Security.Principal.SecurityIdentifier ([System.Security.Principal.WellKnownSidType]::LocalSystemSid, $null)
    $admins = New-Object System.Security.Principal.SecurityIdentifier ([System.Security.Principal.WellKnownSidType]::BuiltinAdministratorsSid, $null)
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($user)
    foreach ($sid in @($user, $system, $admins)) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, "FullControl", "Allow")
        $acl.AddAccessRule($rule)
    }
    [System.IO.File]::SetAccessControl($Path, $acl)
}

function Get-AclSummary([string]$Path) {
    try {
        $acl = Get-Acl -LiteralPath $Path
        $broad = @()
        foreach ($rule in $acl.Access) {
            if ($rule.AccessControlType -ne "Allow") { continue }
            try { $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { $sid = "" }
            if ($sid -in @("S-1-1-0", "S-1-5-11", "S-1-5-32-545")) { $broad += $rule.IdentityReference.Value }
        }
        return [ordered]@{ protected = [bool]$acl.AreAccessRulesProtected; broad_read = @($broad) }
    } catch {
        return [ordered]@{ protected = $false; broad_read = @("unknown: $($_.Exception.Message)") }
    }
}

# Runs a native executable without PowerShell's stderr-to-ErrorRecord
# conversion. Returns @{ ExitCode; StdOut; StdErr; Ms }.
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = "",
        [hashtable]$Environment = @{},
        [int]$TimeoutSec = 900,
        [string]$StdIn = $null
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = (($Arguments | ForEach-Object { ConvertTo-CommandLineArg $_ }) -join " ")
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = ($null -ne $StdIn)
    $psi.CreateNoWindow = $true
    $psi.StandardOutputEncoding = $script:Utf8NoBom
    $psi.StandardErrorEncoding = $script:Utf8NoBom
    if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }
    $psi.EnvironmentVariables["PYTHONUTF8"] = "1"
    $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
    foreach ($k in $Environment.Keys) {
        if ($null -eq $Environment[$k]) { $psi.EnvironmentVariables.Remove($k) } else { $psi.EnvironmentVariables[$k] = [string]$Environment[$k] }
    }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $proc = [System.Diagnostics.Process]::Start($psi)
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    if ($null -ne $StdIn) {
        $proc.StandardInput.Write($StdIn)
        $proc.StandardInput.Close()
    }
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try { $proc.Kill() } catch { }
        throw "Timed out after $TimeoutSec s: $FilePath $($psi.Arguments)"
    }
    $proc.WaitForExit()
    $sw.Stop()
    $result = @{ ExitCode = $proc.ExitCode; StdOut = $outTask.Result; StdErr = $errTask.Result; Ms = $sw.ElapsedMilliseconds }
    Write-VwLog ("exec [{0}] {1} {2} ({3} ms)" -f $result.ExitCode, $FilePath, $psi.Arguments, $result.Ms)
    if ($result.StdOut) { Write-VwLog ("stdout: " + $result.StdOut.TrimEnd()) }
    if ($result.StdErr) { Write-VwLog ("stderr: " + $result.StdErr.TrimEnd()) }
    return $result
}

# MSVC command-line quoting (what CommandLineToArgvW / Python expect).
function ConvertTo-CommandLineArg([string]$Arg) {
    if ($null -eq $Arg) { return '""' }
    if ($Arg.Length -gt 0 -and $Arg -notmatch '[\s"]') { return $Arg }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $backslashes = 0
    foreach ($ch in $Arg.ToCharArray()) {
        if ($ch -eq '\') { $backslashes++; continue }
        if ($ch -eq '"') {
            [void]$sb.Append('\' * ($backslashes * 2 + 1)); [void]$sb.Append('"'); $backslashes = 0; continue
        }
        if ($backslashes) { [void]$sb.Append('\' * $backslashes); $backslashes = 0 }
        [void]$sb.Append($ch)
    }
    if ($backslashes) { [void]$sb.Append('\' * ($backslashes * 2)) }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Test-ForbiddenPython([string]$Exe) {
    # Codex ships a private Python runtime; never build on it (it moves with
    # Codex updates and lives in a sandbox-specific location).
    $p = $Exe.ToLowerInvariant()
    return ($p -like '*\.cache\codex-runtimes\*') -or ($p -like '*\openai\codex\*') -or ($p -like '*\windowsapps\*') -or ($p -like '*\packages\*\localcache\*')
}

# Finds a python.org-style interpreter >= 3.11, preferring 3.12.
function Find-BasePython([string]$Explicit = "") {
    $candidates = New-Object System.Collections.Generic.List[object]
    if ($Explicit) { $candidates.Add(@($Explicit)) }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { foreach ($v in @("-3.12", "-3.13", "-3.11")) { $candidates.Add(@($py.Source, $v)) } }
    foreach ($v in @("312", "313", "311")) {
        foreach ($base in @("$env:LOCALAPPDATA\Programs\Python\Python$v", "$env:ProgramFiles\Python$v")) {
            $exe = Join-Path $base "python.exe"
            if (Test-Path -LiteralPath $exe) { $candidates.Add(@($exe)) }
        }
    }
    $plain = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($plain) { $candidates.Add(@($plain.Source)) }
    foreach ($cand in $candidates) {
        $exe = $cand[0]
        $pre = @()
        if ($cand.Count -gt 1) { $pre = @($cand[1..($cand.Count - 1)]) }
        try {
            $r = Invoke-Native -FilePath $exe -Arguments ($pre + @("-I", "-c", "import sys,json;print(json.dumps([sys.executable,list(sys.version_info[:3]),sys.prefix==sys.base_prefix]))")) -TimeoutSec 30
        } catch { continue }
        if ($r.ExitCode -ne 0) { continue }
        try { $info = $r.StdOut.Trim() | ConvertFrom-Json } catch { continue }
        $real = [string]$info[0]
        $ver = @($info[1])
        if (-not [bool]$info[2]) { continue } # a venv, not a base interpreter
        if (Test-ForbiddenPython $real) { continue }
        if ($ver[0] -ne 3 -or $ver[1] -lt 11) { continue }
        return [ordered]@{ executable = $real; version = ($ver -join "."); found_via = ($cand -join " ") }
    }
    return $null
}

function Get-SourceVersion([string]$SourceRoot) {
    $init = Join-Path $SourceRoot "vectorworks_mcp\__init__.py"
    if (Test-Path -LiteralPath $init) {
        $m = Select-String -LiteralPath $init -Pattern '^__version__\s*=\s*[''"]([^''"]+)[''"]' | Select-Object -First 1
        if ($m) { return $m.Matches[0].Groups[1].Value }
    }
    $pyproject = Join-Path $SourceRoot "pyproject.toml"
    if (Test-Path -LiteralPath $pyproject) {
        $m = Select-String -LiteralPath $pyproject -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
        if ($m) { return $m.Matches[0].Groups[1].Value }
    }
    throw "Cannot determine the Vectorworks MCP version from $SourceRoot"
}

function Copy-RuntimeTree([string]$SourceRoot, [string]$Destination) {
    $copied = 0
    foreach ($rel in ($script:VwRuntimeRequired + $script:VwRuntimeOptional)) {
        $src = Join-Path $SourceRoot $rel
        if (-not (Test-Path -LiteralPath $src)) {
            if ($script:VwRuntimeRequired -contains $rel) { throw "Source is incomplete, missing: $src" }
            continue
        }
        $dst = Join-Path $Destination $rel
        if (Test-Path -LiteralPath $src -PathType Container) {
            $srcFull = (Get-FullPath $src).TrimEnd('\') + '\'
            Get-ChildItem -LiteralPath $src -Recurse -File -Force |
                Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.Extension -ne '.pyc' } |
                ForEach-Object {
                    $target = Join-Path $dst $_.FullName.Substring($srcFull.Length)
                    $tdir = Split-Path -Parent $target
                    if (-not (Test-Path -LiteralPath $tdir)) { New-Item -ItemType Directory -Path $tdir -Force | Out-Null }
                    Copy-Item -LiteralPath $_.FullName -Destination $target -Force
                    $copied++
                }
        } else {
            $tdir = Split-Path -Parent $dst
            if (-not (Test-Path -LiteralPath $tdir)) { New-Item -ItemType Directory -Path $tdir -Force | Out-Null }
            Copy-Item -LiteralPath $src -Destination $dst -Force
            $copied++
        }
    }
    return $copied
}

function Get-VectorworksProcesses {
    return @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^Vectorworks(20\d\d)?$' })
}

function Get-InstalledVectorworksVersions {
    $found = @()
    $base = Join-Path $env:APPDATA "Nemetschek\Vectorworks"
    if (Test-Path -LiteralPath $base) {
        $found += @(Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^20\d\d$' } | ForEach-Object { $_.Name })
    }
    return @($found | Sort-Object -Unique -Descending)
}

function Test-IsOurOldBridge([string]$VlbPath) {
    # The pre-1.0 bridge was built from the SDK sample and kept its name
    # (ObjectExample.vlb). Only treat it as ours if it references our state dir.
    try {
        $bytes = [System.IO.File]::ReadAllBytes($VlbPath)
        $text = [System.Text.Encoding]::ASCII.GetString($bytes)
        return ($text.Contains(".vectorworks-mcp") -or $text.Contains("VW_MCP_AUTH_TOKEN"))
    } catch { return $false }
}

# rel path ('/'-separated) -> sha256 for every file Copy-RuntimeTree would copy.
function Get-ManifestHashes([string]$SourceRoot) {
    $result = [ordered]@{}
    $rootFull = (Get-FullPath $SourceRoot).TrimEnd('\') + '\'
    $files = New-Object System.Collections.Generic.List[string]
    foreach ($rel in ($script:VwRuntimeRequired + $script:VwRuntimeOptional)) {
        $src = Join-Path $SourceRoot $rel
        if (-not (Test-Path -LiteralPath $src)) { continue }
        if (Test-Path -LiteralPath $src -PathType Container) {
            Get-ChildItem -LiteralPath $src -Recurse -File -Force |
                Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.Extension -ne '.pyc' } |
                ForEach-Object { $files.Add($_.FullName) }
        } else { $files.Add((Get-FullPath $src)) }
    }
    foreach ($f in ($files | Sort-Object -Unique)) {
        $result[$f.Substring($rootFull.Length).Replace('\', '/')] = (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    return $result
}

function Test-HashMapsEqual($A, $B) {
    if ($null -eq $A -or $null -eq $B) { return $false }
    $ha = @{}; foreach ($k in $A.Keys) { $ha[$k] = $A[$k] }
    $count = 0
    foreach ($p in $B.PSObject.Properties) {
        $count++
        if (-not $ha.ContainsKey($p.Name) -or $ha[$p.Name] -ne $p.Value) { return $false }
    }
    return ($count -eq $ha.Count)
}

# Remove-Item fails on paths longer than MAX_PATH in Windows PowerShell 5.1
# (deep venv site-packages); fall back to rd with the extended-length prefix.
function Remove-Tree([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    try { Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop; return } catch { }
    $full = Get-FullPath $Path
    if (Test-Path -LiteralPath $full -PathType Leaf) { throw "cannot remove $full" }
    $long = ('\' + '\?\') + $full
    $r = Invoke-Native -FilePath (Join-Path $env:SystemRoot "System32\cmd.exe") -Arguments @("/d", "/c", "rd", "/s", "/q", $long) -TimeoutSec 300
    if (Test-Path -LiteralPath $full) { throw "cannot remove $full (in use?) $($r.StdErr.Trim())" }
}
