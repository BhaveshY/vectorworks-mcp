<#
.SYNOPSIS
  Install, repair or uninstall Vectorworks MCP 1.0 for the current Windows user.

.DESCRIPTION
  One command, idempotent, works from a git checkout or an extracted release zip:

    powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1

  1. Runtime   %USERPROFILE%\.vectorworks-mcp\runtime\<version>\ (app\ + venv\), built
               with python.org Python 3.12 (py -3.12); dependencies installed once with
               pip --require-hashes from requirements.lock; byte-compiled.
  2. Launcher  %USERPROFILE%\.vectorworks-mcp\bin\vectorworks-mcp.cmd (plain .cmd ->
               venv python.exe; repair-mode server when the runtime is missing).
  3. Token     %USERPROFILE%\.vectorworks-mcp\auth-token (kept if present; private ACL).
  4. Native    VectorworksMCPBridge.vlb/.vwr -> %APPDATA%\Nemetschek\Vectorworks\<ver>\Plug-ins
               (prebuilt from a release, or built with -FullNative); old ObjectExample.vlb/.vwr
               backed up and removed. Never while Vectorworks runs (use -WaitForVectorworksExit).
  5. Clients   scripts\register-clients.ps1: Codex [mcp_servers.vectorworks] + Claude Code
               user-scope server, both pointing at the launcher; skills-only plugins.
  6. Receipt   install-receipt.json (version, hashes, native hash, client changes) and
               current.txt. -Uninstall is driven by the receipt.

.EXAMPLE
  .\install.ps1                                   # runtime + clients (+ prebuilt native if Vectorworks is closed)
.EXAMPLE
  .\install.ps1 -FullNative -WaitForVectorworksExit
.EXAMPLE
  .\install.ps1 -Root C:\temp\vw -Client none -SkipNative -DryRun
.EXAMPLE
  .\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$Root = "",
    [ValidateSet("all", "codex", "claude-code", "none")]
    [string]$Client = "all",
    [string]$CodexHome = "",
    [string]$ClaudeConfig = "",
    [string]$PluginsDir = "",
    [string]$VectorworksVersion = "",
    [switch]$FullNative,
    [switch]$SkipNative,
    [string]$NativeArtifactDir = "",
    [switch]$WaitForVectorworksExit,
    [int]$WaitTimeoutSec = 1800,
    [string]$Python = "",
    [string]$WheelsDir = "",
    [switch]$Offline,
    [switch]$Force,
    [switch]$Uninstall,
    [switch]$Purge,
    [switch]$SkipDoctor,
    [Alias("WhatIf")][switch]$DryRun,
    [switch]$Json,
    [string]$SourceUrl = "https://github.com/BhaveshY/vectorworks-mcp/archive/refs/heads/main.zip"
)

$ErrorActionPreference = "Stop"
$InstallerVersion = "1.0.0"

# ------------------------------------------------------------- bootstrap
# Run via `& ([scriptblock]::Create((irm <raw url>/install.ps1)))` there is no
# source folder: download the release zip and re-run the installer from it.
$SourceRoot = $PSScriptRoot
if (-not $SourceRoot -or -not (Test-Path -LiteralPath (Join-Path $SourceRoot "vectorworks_mcp\__init__.py"))) {
    if ($Uninstall) {
        $r = if ($Root) { $Root } else { Join-Path $env:USERPROFILE ".vectorworks-mcp" }
        $cur = Join-Path $r "current.txt"
        if (Test-Path -LiteralPath $cur) {
            $app = Join-Path $r ("runtime\" + (Get-Content -LiteralPath $cur -TotalCount 1).Trim() + "\app\install.ps1")
            if (Test-Path -LiteralPath $app) { & $app @PSBoundParameters; exit $LASTEXITCODE }
        }
        throw "No installed runtime found under $r to uninstall."
    }
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("vectorworks-mcp-src-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    $zip = Join-Path $tmp "src.zip"
    Write-Host "Downloading Vectorworks MCP source: $SourceUrl"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $SourceUrl -OutFile $zip -UseBasicParsing
    Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
    $inner = Get-ChildItem -LiteralPath $tmp -Recurse -Filter install.ps1 | Where-Object { Test-Path -LiteralPath (Join-Path $_.DirectoryName "vectorworks_mcp\__init__.py") } | Select-Object -First 1
    if (-not $inner) { throw "Downloaded archive does not contain a Vectorworks MCP source tree." }
    & $inner.FullName @PSBoundParameters
    exit $LASTEXITCODE
}

. (Join-Path $SourceRoot "scripts\install-common.ps1")
if ($Json) { $script:VwQuiet = $true }

if (-not $Root) { $Root = Get-DefaultRoot }
$Root = Get-FullPath $Root
$Bin = Join-Path $Root "bin"
$Launcher = Join-Path $Bin "vectorworks-mcp.cmd"
$ReceiptPath = Join-Path $Root "install-receipt.json"
$CurrentPath = Join-Path $Root "current.txt"
$MarketplaceDir = Join-Path $Root "marketplace"
$Stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
$payload = [ordered]@{ ok = $true; action = $(if ($Uninstall) { "uninstall" } else { "install" }); dry_run = [bool]$DryRun; root = $Root; warnings = @(); steps = [ordered]@{} }

function Add-Warn([string]$m) { $payload.warnings += $m; Write-Step $m "warn" }
function Plan([string]$m) { if ($DryRun) { Write-Step $m "plan"; return $true } return $false }

Assert-RealFilesystemPath $Root "Install root"
$package = Test-MsixProcess
if ($package) {
    Add-Warn "This PowerShell runs inside the MSIX package '$package' (e.g. the Codex app). AppData writes are virtualised there."
}
if (-not $DryRun) {
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
    Start-VwLog $Root $(if ($Uninstall) { "uninstall" } else { "install" })
}
Write-Step "Vectorworks MCP installer $InstallerVersion  (source: $SourceRoot, root: $Root)" "head"

$receipt = $null
if (Test-Path -LiteralPath $ReceiptPath) { try { $receipt = Get-Content -LiteralPath $ReceiptPath -Raw | ConvertFrom-Json } catch { Add-Warn "install-receipt.json is unreadable; treating as a fresh install" } }

# ======================================================================
# Uninstall
# ======================================================================
if ($Uninstall) {
    $regArgs = @{ Client = "all"; Root = $Root; Launcher = $Launcher; Unregister = $true; MarketplaceDir = $MarketplaceDir }
    if ($CodexHome) { $regArgs.CodexHome = $CodexHome }
    if ($ClaudeConfig) { $regArgs.ClaudeConfig = $ClaudeConfig }
    if ($Python) { $regArgs.Python = $Python }
    if ($DryRun) { $regArgs.DryRun = $true }
    Write-Step "Unregistering clients" "head"
    try { & (Join-Path $SourceRoot "scripts\register-clients.ps1") @regArgs } catch { Add-Warn "client unregistration: $($_.Exception.Message)" }

    Write-Step "Native plug-in" "head"
    if ($receipt -and $receipt.native -and $receipt.native.installed) {
        foreach ($n in @($receipt.native.installed)) {
            $dir = [string]$n.plugins_dir
            if (Get-VectorworksProcesses) { Add-Warn "Vectorworks is running; leaving $dir untouched (rerun -Uninstall after closing it)"; break }
            $bk = Join-Path $Root "backups\uninstall-$Stamp\$($n.vectorworks_version)"
            foreach ($p in $n.files.PSObject.Properties) {
                $f = Join-Path $dir $p.Name
                if ((Test-Path -LiteralPath $f) -and ((Get-Sha256 $f) -eq $p.Value)) {
                    if (-not (Plan "would move $f -> $bk")) {
                        New-Item -ItemType Directory -Path $bk -Force | Out-Null
                        Move-Item -LiteralPath $f -Destination (Join-Path $bk $p.Name) -Force
                        Write-Step "removed $f (backup in $bk)" "ok"
                    }
                }
            }
        }
    } else { Write-Step "no native plug-in recorded in the receipt" }

    Write-Step "Runtime" "head"
    $targets = @($Launcher, (Join-Path $Bin "vectorworksctl.cmd"), (Join-Path $Bin "repair_server.py"), $MarketplaceDir, $CurrentPath, $ReceiptPath)
    $rt = Join-Path $Root "runtime"
    if (Test-Path -LiteralPath $rt) { $targets += @(Get-ChildItem -LiteralPath $rt -Directory | ForEach-Object { $_.FullName }) }
    if ($Purge) { $targets += @((Join-Path $Root "auth-token"), (Join-Path $Root "captures"), (Join-Path $Root "instances")) }
    foreach ($t in $targets) {
        if (-not (Test-Path -LiteralPath $t)) { continue }
        if (Plan "would remove $t") { continue }
        try { Remove-Tree $t; Write-Step "removed $t" "ok" }
        catch { Add-Warn "could not remove $t (in use by a running MCP client? close Codex/Claude and rerun): $($_.Exception.Message)" }
    }
    Write-Step "Kept: auth-token, logs, backups$(if (-not $Purge) { ', captures, instances (use -Purge to remove)' })"
    if ($Json) { $payload | ConvertTo-Json -Depth 8 }
    exit 0
}

# ======================================================================
# Install
# ======================================================================
$version = Get-SourceVersion $SourceRoot
$payload.version = $version
Write-Step "Version $version" "head"

# ---------------------------------------------------------------- Python
$base = Find-BasePython $Python
if (-not $base) {
    $payload.ok = $false
    $msg = "Python 3.11+ from python.org was not found (Codex's bundled Python is never used). Install it with:  winget install --id Python.Python.3.12 -e --scope user   then rerun this installer."
    Write-Step $msg "fail"
    if ($Json) { $payload.error = $msg; $payload | ConvertTo-Json -Depth 8 }
    exit 2
}
Write-Step "Python $($base.version): $($base.executable)" "ok"
$payload.python = $base

# --------------------------------------------------------------- runtime
$sourceHashes = Get-ManifestHashes $SourceRoot
$lockHash = Get-Sha256 (Join-Path $SourceRoot "requirements.lock")
$currentId = $null
if (Test-Path -LiteralPath $CurrentPath) { $currentId = (Get-Content -LiteralPath $CurrentPath -TotalCount 1).Trim() }
$reuse = $false
if (-not $Force -and $receipt -and $currentId -and $receipt.runtime_id -eq $currentId -and $receipt.version -eq $version -and
    $receipt.requirements_lock_sha256 -eq $lockHash -and (Test-HashMapsEqual $sourceHashes $receipt.app_files) -and
    (Test-Path -LiteralPath (Join-Path $Root "runtime\$currentId\venv\Scripts\python.exe")) -and
    $receipt.python.base_executable -eq $base.executable) {
    $reuse = $true
}

$runtimeId = $version
$sameAsSource = [bool]($currentId -and (Test-SamePath $SourceRoot (Join-Path $Root "runtime\$currentId\app")))
if ($reuse) {
    $runtimeId = $currentId
    Write-Step "runtime $runtimeId is already installed and verified - reusing" "ok"
} elseif ($sameAsSource) {
    # Repair run from the installed copy (vectorworksctl repair): rebuild the venv in place.
    $runtimeId = $currentId
    Write-Step "repairing runtime $runtimeId in place" "info"
} else {
    $target = Join-Path $Root "runtime\$runtimeId"
    if (Test-Path -LiteralPath $target) {
        if (-not (Plan "would replace existing $target")) {
            $old = Join-Path $Root "runtime\$runtimeId.old-$Stamp"
            try { Rename-Item -LiteralPath $target -NewName (Split-Path -Leaf $old) -ErrorAction Stop; Write-Step "moved previous runtime to $old" }
            catch {
                $runtimeId = "$version-$Stamp"
                Write-Step "runtime $version is in use by a running client; installing side by side as $runtimeId" "warn"
            }
        }
    }
}
$RuntimeDir = Join-Path $Root "runtime\$runtimeId"
$AppDir = Join-Path $RuntimeDir "app"
$VenvDir = Join-Path $RuntimeDir "venv"
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
$payload.runtime_id = $runtimeId
$payload.runtime_dir = $RuntimeDir

if (-not $reuse -and $sameAsSource -and -not $Force -and (Test-Path -LiteralPath $VenvPy)) {
    # In-place repair: keep a venv that still works (it may be in use by a running client).
    $probe = Invoke-Native -FilePath $VenvPy -Arguments @("-I", "-X", "utf8", "-c", "import vectorworks_mcp, sys; sys.stdout.write(vectorworks_mcp.__version__)") -WorkingDirectory $env:TEMP -TimeoutSec 120
    if ($probe.ExitCode -eq 0 -and $probe.StdOut.Trim() -eq $version) { $reuse = $true; Write-Step "venv of runtime $runtimeId works - kept" "ok" }
}
if (-not $reuse) {
    if (-not (Plan "would copy runtime files to $AppDir and build $VenvDir")) {
        if (-not $sameAsSource) {
            New-Item -ItemType Directory -Path $AppDir -Force | Out-Null
            $n = Copy-RuntimeTree $SourceRoot $AppDir
            Write-Step "copied $n files to $AppDir" "ok"
        }
        Remove-Tree $VenvDir
        $r = Invoke-Native -FilePath $base.executable -Arguments @("-I", "-m", "venv", $VenvDir) -TimeoutSec 300
        if ($r.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $VenvPy)) { throw "venv creation failed: $($r.StdErr)" }
        Write-Step "created venv ($([math]::Round($r.Ms / 1000, 1)) s)" "ok"

        $pipArgs = @("-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--require-hashes", "-r", (Join-Path $AppDir "requirements.lock"))
        $wheels = if ($WheelsDir) { $WheelsDir } elseif (Test-Path -LiteralPath (Join-Path $SourceRoot "wheels")) { Join-Path $SourceRoot "wheels" } else { "" }
        if ($wheels) { $pipArgs += @("--find-links", $wheels); if ($Offline -or -not $WheelsDir) { $pipArgs += "--no-index" } }
        elseif ($Offline) { throw "-Offline needs a wheels folder (release zip built with package-release.ps1 -IncludeWheels, or -WheelsDir)." }
        Write-Step "installing locked dependencies (pip --require-hashes$(if ($wheels) { ", wheels: $wheels" }))..."
        $r = Invoke-Native -FilePath $VenvPy -Arguments $pipArgs -TimeoutSec 1200
        if ($r.ExitCode -ne 0) {
            $tail = (($r.StdErr + "`n" + $r.StdOut).Trim() -split "`n" | Select-Object -Last 8) -join "`n"
            throw "pip install failed (exit $($r.ExitCode)). Log: $script:VwLogFile`n$tail"
        }
        Write-Step "dependencies installed ($([math]::Round($r.Ms / 1000, 1)) s)" "ok"

        $site = Join-Path $VenvDir "Lib\site-packages"
        Write-Utf8File (Join-Path $site "vectorworks_mcp_app.pth") ($AppDir + "`n")
        $r = Invoke-Native -FilePath $VenvPy -Arguments @("-I", "-m", "compileall", "-q", "-j", "0", $AppDir) -TimeoutSec 600
        if ($r.ExitCode -ne 0) { Add-Warn "compileall reported problems (see log)" }
        $r = Invoke-Native -FilePath $VenvPy -Arguments @("-I", "-X", "utf8", "-c", "import vectorworks_mcp, sys; sys.stdout.write(vectorworks_mcp.__version__)") -WorkingDirectory $env:TEMP -TimeoutSec 120
        if ($r.ExitCode -ne 0 -or $r.StdOut.Trim() -ne $version) { throw "runtime smoke import failed: $($r.StdErr.Trim()) $($r.StdOut.Trim())" }
        Write-Step "runtime $runtimeId imports vectorworks_mcp $version" "ok"
    }
}

# --------------------------------------------------------------- launcher
$tplDir = if (Test-Path -LiteralPath (Join-Path $AppDir "bin\vectorworks-mcp.cmd.template")) { Join-Path $AppDir "bin" } else { Join-Path $SourceRoot "bin" }
# Repair/upgrade command shown by the host in upgrade_required / setup hints.
$tempRoot = (Get-FullPath ([System.IO.Path]::GetTempPath())).TrimEnd('\') + '\'
$cmdSource = $SourceRoot
if ($sameAsSource -and $receipt -and $receipt.source_dir -and (Test-Path -LiteralPath (Join-Path ([string]$receipt.source_dir) "install.ps1"))) { $cmdSource = [string]$receipt.source_dir }
if ((Get-FullPath $cmdSource).StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or (Test-SamePath $cmdSource $AppDir)) {
    $installCommand = "close Vectorworks, then run: `"$Bin\vectorworksctl.cmd`" repair"
} else {
    $installCommand = "close Vectorworks, then run: powershell -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $cmdSource 'install.ps1')`" -FullNative -WaitForVectorworksExit"
}
$launcherText = (Get-Content -LiteralPath (Join-Path $tplDir "vectorworks-mcp.cmd.template") -Raw).Replace("@@BASE_PYTHON@@", $base.executable).Replace("@@INSTALL_COMMAND@@", $installCommand.Replace('%', '%%'))
$ctlText = Get-Content -LiteralPath (Join-Path $tplDir "vectorworksctl.cmd.template") -Raw
if (-not (Plan "would write $Launcher, $Bin\vectorworksctl.cmd, $Bin\repair_server.py")) {
    $changed = Write-BatchFile $Launcher $launcherText
    $null = Write-BatchFile (Join-Path $Bin "vectorworksctl.cmd") $ctlText
    $repairSrc = if (Test-Path -LiteralPath (Join-Path $AppDir "vectorworks_mcp\repair_server.py")) { Join-Path $AppDir "vectorworks_mcp\repair_server.py" } else { Join-Path $SourceRoot "vectorworks_mcp\repair_server.py" }
    Copy-Item -LiteralPath $repairSrc -Destination (Join-Path $Bin "repair_server.py") -Force
    Write-Step "launcher $Launcher$(if (-not $changed) { ' (unchanged)' })" "ok"
}

# ------------------------------------------------------------- auth token
$tokenPath = Join-Path $Root "auth-token"
$tokenCreated = $false
$existingToken = ""
if (Test-Path -LiteralPath $tokenPath) { $existingToken = [string](Get-Content -LiteralPath $tokenPath -TotalCount 1 -ErrorAction SilentlyContinue) }
if ($existingToken.Trim().Length -lt 32) {
    if (-not (Plan "would create $tokenPath (private ACL)")) {
        $bytes = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
        [System.IO.File]::WriteAllText($tokenPath, $token, (New-Object System.Text.ASCIIEncoding))
        $tokenCreated = $true
        Write-Step "created auth-token (restart Vectorworks so the bridge reads it)" "ok"
    }
} else { Write-Step "auth-token present (kept)" "ok" }
$existingToken = $null
if (-not $DryRun -and (Test-Path -LiteralPath $tokenPath)) {
    try { Set-PrivateFileAcl $tokenPath } catch { Add-Warn "could not restrict the auth-token ACL: $($_.Exception.Message)" }
}
if (-not $DryRun) { foreach ($d in @("instances", "logs", "captures", "backups")) { New-Item -ItemType Directory -Path (Join-Path $Root $d) -Force | Out-Null } }

# ------------------------------------------------- skills marketplace copy
if (-not (Plan "would refresh $MarketplaceDir (skills-only plugin for Codex)")) {
    $mSrc = if (Test-Path -LiteralPath (Join-Path $AppDir "plugins\vectorworks")) { $AppDir } else { $SourceRoot }
    Remove-Tree $MarketplaceDir
    New-Item -ItemType Directory -Path (Join-Path $MarketplaceDir ".agents\plugins") -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $mSrc ".agents\plugins\marketplace.json") -Destination (Join-Path $MarketplaceDir ".agents\plugins\marketplace.json") -Force
    if (Test-Path -LiteralPath (Join-Path $mSrc ".claude-plugin\marketplace.json")) {
        New-Item -ItemType Directory -Path (Join-Path $MarketplaceDir ".claude-plugin") -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $mSrc ".claude-plugin\marketplace.json") -Destination (Join-Path $MarketplaceDir ".claude-plugin\marketplace.json") -Force
    }
    New-Item -ItemType Directory -Path (Join-Path $MarketplaceDir "plugins") -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $mSrc "plugins\vectorworks") -Destination (Join-Path $MarketplaceDir "plugins") -Recurse -Force
    Write-Step "skills marketplace $MarketplaceDir" "ok"
}

# --------------------------------------------------------- activate runtime
if (-not (Plan "would set current.txt -> $runtimeId")) {
    Write-Utf8File $CurrentPath $runtimeId
    Write-Step "active runtime: $runtimeId" "ok"
}

# ----------------------------------------------------------------- native
$native = [ordered]@{ status = "skipped"; installed = @() }
function Find-NativeArtifact([string]$ver) {
    # -FullNative prefers the fresh local build; otherwise the prebuilt native\<ver> (release zip or
    # the copy tracked in git, see native\PREBUILT.json). A dev build output is never used implicitly.
    $cands = @()
    if ($NativeArtifactDir) { $cands += @((Join-Path $NativeArtifactDir $ver), $NativeArtifactDir) }
    if ($FullNative) { $cands += (Join-Path $SourceRoot "native_bridge\project\out\$ver\Release") }
    $cands += @((Join-Path $SourceRoot "native\$ver"), (Join-Path $SourceRoot "native"))
    foreach ($c in $cands) {
        if ((Test-Path -LiteralPath (Join-Path $c "VectorworksMCPBridge.vlb")) -and (Test-Path -LiteralPath (Join-Path $c "VectorworksMCPBridge.vwr"))) { return (Get-FullPath $c) }
    }
    return $null
}
function Test-VisualStudio {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path -LiteralPath $vswhere)) { return $false }
    $r = Invoke-Native -FilePath $vswhere -Arguments @("-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath") -TimeoutSec 60
    return [bool]($r.ExitCode -eq 0 -and $r.StdOut.Trim())
}

$isCheckout = Test-Path -LiteralPath (Join-Path $SourceRoot ".git")
$prebuiltPath = Join-Path $SourceRoot "native\PREBUILT.json"
$prebuilt = $null
if (Test-Path -LiteralPath $prebuiltPath) { try { $prebuilt = Get-Content -LiteralPath $prebuiltPath -Raw | ConvertFrom-Json } catch { Add-Warn "native\PREBUILT.json is unreadable" } }
if ($SkipNative) {
    Write-Step "Native plug-in: skipped (-SkipNative)" "head"
} elseif ($isCheckout -and -not $FullNative -and -not $NativeArtifactDir -and -not $prebuilt) {
    # A checkout without native\PREBUILT.json may hold a stale dev build only.
    Write-Step "Native plug-in: not changed (git checkout - pass -FullNative to build/install VectorworksMCPBridge)" "head"
    $native.status = "not_requested"
} else {
    Write-Step "Native plug-in" "head"
    $versions = if ($VectorworksVersion) { @($VectorworksVersion) } else { @(Get-InstalledVectorworksVersions) }
    if ($versions.Count -eq 0) { $versions = @("2024"); if (-not $PluginsDir) { Add-Warn "no Vectorworks user folder found; assuming 2024" } }
    $native.status = "ok"
    foreach ($ver in $versions) {
        $art = Find-NativeArtifact $ver
        if ($FullNative -and (Test-Path -LiteralPath (Join-Path $SourceRoot "scripts\build-native-bridge.ps1"))) {
            if (Test-VisualStudio) {
                if (-not (Plan "would build the native bridge for $ver (scripts\build-native-bridge.ps1)")) {
                    Write-Step "building native bridge for Vectorworks $ver (Visual Studio found)..."
                    try {
                        & (Join-Path $SourceRoot "scripts\build-native-bridge.ps1") -VectorworksVersion $ver -Configuration Release | ForEach-Object { Write-VwLog "build: $_" }
                    } catch { Add-Warn "native build for $ver failed: $($_.Exception.Message)$(if ($art) { ' - using the existing build' })" }
                    $art = Find-NativeArtifact $ver
                }
            } elseif (-not $art) { Add-Warn "-FullNative: Visual Studio C++ tools not found and no prebuilt VectorworksMCPBridge.vlb for $ver" }
        }
        if (-not $art) {
            $native.status = "no_artifact"
            $msg = "no VectorworksMCPBridge.vlb/.vwr for Vectorworks $ver (use a release zip, -NativeArtifactDir, or -FullNative with Visual Studio)"
            if ($FullNative) { $payload.ok = $false; Write-Step $msg "fail" } else { Write-Step $msg "warn" }
            continue
        }
        $pb = if ($prebuilt) { $prebuilt.$ver } else { $null }
        if ($pb -and (Test-SamePath $art (Join-Path $SourceRoot "native\$ver"))) {
            if (-not $pb.live_tested) { Add-Warn "the prebuilt plug-in for Vectorworks $ver ($($pb.build_id)) has not been live-tested yet - run scripts\smoke_installed.py after starting Vectorworks" }
            if ($isCheckout -and (Get-Command git.exe -ErrorAction SilentlyContinue)) {
                $tree = (& git.exe -C $SourceRoot rev-parse HEAD:native_bridge/src 2>$null)
                if ($tree -and "$tree".Trim() -ne $pb.native_src_tree) { Add-Warn "the prebuilt plug-in for Vectorworks $ver ($($pb.build_id)) is older than native_bridge\src in this checkout" }
            }
        }
        $pdir = if ($PluginsDir) { Get-FullPath $PluginsDir } else { Join-Path $env:APPDATA "Nemetschek\Vectorworks\$ver\Plug-ins" }
        Assert-RealFilesystemPath $pdir "Vectorworks Plug-ins folder"
        if ($package -and -not $PluginsDir) { throw "Refusing to install the native plug-in from inside the MSIX package '$package': the files would land in its private LocalCache. Run install.ps1 from a normal PowerShell window." }
        $files = [ordered]@{}
        foreach ($n in @("VectorworksMCPBridge.vlb", "VectorworksMCPBridge.vwr")) { $files[$n] = Get-Sha256 (Join-Path $art $n) }
        $oldFiles = @(@("ObjectExample.vlb", "ObjectExample.vwr") | Where-Object { Test-Path -LiteralPath (Join-Path $pdir $_) })
        $oldVlb = Join-Path $pdir "ObjectExample.vlb"
        if ((Test-Path -LiteralPath $oldVlb) -and -not (Test-IsOurOldBridge $oldVlb)) { Add-Warn "$oldVlb is not the old Vectorworks MCP bridge; leaving it alone"; $oldFiles = @() }
        $upToDate = ($oldFiles.Count -eq 0)
        foreach ($n in $files.Keys) { if ((Get-Sha256 (Join-Path $pdir $n)) -ne $files[$n]) { $upToDate = $false } }
        $entry = [ordered]@{ vectorworks_version = $ver; plugins_dir = $pdir; source = $art; files = $files; backup_dir = $null; removed = @() }
        if ($upToDate) { Write-Step "Vectorworks ${ver}: VectorworksMCPBridge already installed" "ok"; $native.installed += $entry; continue }
        # Test hook: a sandbox -PluginsDir may ignore a running Vectorworks (never the real folder).
        $ignoreRunning = $PluginsDir -and $env:VW_MCP_TEST_IGNORE_RUNNING -eq "1"
        if ((Get-VectorworksProcesses) -and -not $ignoreRunning) {
            if ($WaitForVectorworksExit -and -not $DryRun) {
                Write-Step "Vectorworks is running - waiting up to $WaitTimeoutSec s for it to exit (save and close it)..." "warn"
                $deadline = (Get-Date).AddSeconds($WaitTimeoutSec)
                while ((Get-VectorworksProcesses) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
            }
            if ((Get-VectorworksProcesses) -and -not $ignoreRunning) {
                $native.status = "pending_vectorworks_running"
                Add-Warn "Vectorworks is running: the bridge plug-in for $ver was NOT installed. Close Vectorworks and rerun: install.ps1 -FullNative (or add -WaitForVectorworksExit)"
                continue
            }
        }
        if (Plan "would install $art -> $pdir (backup + remove $($oldFiles -join ', '))") { continue }
        New-Item -ItemType Directory -Path $pdir -Force | Out-Null
        $bk = Join-Path $Root "backups\plugins-$Stamp\$ver"
        foreach ($n in @(@($files.Keys) + $oldFiles)) {
            $f = Join-Path $pdir $n
            if (Test-Path -LiteralPath $f) { New-Item -ItemType Directory -Path $bk -Force | Out-Null; Copy-Item -LiteralPath $f -Destination (Join-Path $bk $n) -Force; $entry.backup_dir = $bk }
        }
        foreach ($n in $oldFiles) { Remove-Item -LiteralPath (Join-Path $pdir $n) -Force; $entry.removed += $n }
        foreach ($n in $files.Keys) { Copy-Item -LiteralPath (Join-Path $art $n) -Destination (Join-Path $pdir $n) -Force }
        foreach ($n in $files.Keys) { if ((Get-Sha256 (Join-Path $pdir $n)) -ne $files[$n]) { throw "copy verification failed for $pdir\$n" } }
        Write-Step "Vectorworks ${ver}: installed VectorworksMCPBridge.vlb/.vwr$(if ($entry.removed.Count) { "; removed old $($entry.removed -join ', ') (backup $bk)" })" "ok"
        $native.installed += $entry
    }
}
$payload.steps.native = $native

# ---------------------------------------------------------------- clients
$clientsResult = $null
if ($Client -ne "none") {
    Write-Step "Client registration ($Client)" "head"
    $regArgs = @{ Client = $Client; Root = $Root; Launcher = $Launcher; MarketplaceDir = $MarketplaceDir; Python = $base.executable; Json = $true }
    if ($CodexHome) { $regArgs.CodexHome = $CodexHome }
    if ($ClaudeConfig) { $regArgs.ClaudeConfig = $ClaudeConfig }
    if ($DryRun) { $regArgs.DryRun = $true }
    $regScript = Join-Path $SourceRoot "scripts\register-clients.ps1"
    $raw = & $regScript @regArgs
    try { $clientsResult = ($raw | Out-String) | ConvertFrom-Json } catch { Add-Warn "client registration output unreadable: $raw" }
    if ($clientsResult) {
        foreach ($p in $clientsResult.clients.PSObject.Properties) {
            foreach ($c in @($p.Value.changes)) { Write-Step "$($p.Name): $c" ($(if ($DryRun) { "plan" } else { "ok" })) }
            foreach ($c in @($p.Value.notes)) { Write-Step "$($p.Name): $c" }
            if (-not $p.Value.ok) { $payload.ok = $false; Write-Step "$($p.Name): $($p.Value.error)" "fail" }
        }
    }
    $payload.steps.clients = $clientsResult
} else { Write-Step "Client registration skipped (-Client none)" "head" }

# ---------------------------------------------------------------- receipt
if (-not $DryRun) {
    $appHashes = if ($reuse -and $receipt -and $receipt.app_files -and -not $sameAsSource) { $receipt.app_files } else { Get-TreeHashes $AppDir }
    $prevIds = @()
    if ($receipt -and $receipt.runtime_id -and $receipt.runtime_id -ne $runtimeId) { $prevIds += $receipt.runtime_id }
    $sourceDir = $SourceRoot
    if ($sameAsSource -and $receipt -and $receipt.source_dir -and (Test-Path -LiteralPath (Join-Path ([string]$receipt.source_dir) "install.ps1"))) { $sourceDir = [string]$receipt.source_dir }
    $newReceipt = [ordered]@{
        schema = 1
        product = "vectorworks-mcp"
        version = $version
        runtime_id = $runtimeId
        installer_version = $InstallerVersion
        installed_at = (Get-Date).ToUniversalTime().ToString("o")
        source_dir = $sourceDir
        source_kind = $(if (Test-Path -LiteralPath (Join-Path $SourceRoot ".git")) { "checkout" } elseif ($sameAsSource) { "runtime" } else { "package" })
        root = $Root
        launcher = [ordered]@{ path = $Launcher; sha256 = (Get-Sha256 $Launcher) }
        python = [ordered]@{ base_executable = $base.executable; version = $base.version; venv = $VenvDir }
        requirements_lock_sha256 = $lockHash
        app_files = $appHashes
        auth_token = [ordered]@{ path = $tokenPath; created_now = $tokenCreated }
        native = $native
        clients = $clientsResult
        previous_runtime_ids = $prevIds
    }
    Write-JsonFile $ReceiptPath $newReceipt
    Write-Step "receipt $ReceiptPath" "ok"

    # Remove superseded runtimes that are no longer in use.
    $rtRoot = Join-Path $Root "runtime"
    foreach ($d in @(Get-ChildItem -LiteralPath $rtRoot -Directory -ErrorAction SilentlyContinue)) {
        if ($d.Name -eq $runtimeId) { continue }
        try { Remove-Tree $d.FullName; Write-Step "removed old runtime $($d.Name)" }
        catch { Write-VwLog "old runtime $($d.Name) still in use; will be removed by a later install" }
    }
}

# ---------------------------------------------------------------- doctor
if (-not $SkipDoctor -and -not $DryRun) {
    Write-Step "Doctor" "head"
    $docArgs = @{ Root = $Root; Json = $true }
    if ($CodexHome) { $docArgs.CodexHome = $CodexHome }
    if ($ClaudeConfig) { $docArgs.ClaudeConfig = $ClaudeConfig }
    if ($PluginsDir) { $docArgs.PluginsDir = $PluginsDir }
    try {
        $doc = (& (Join-Path $SourceRoot "scripts\doctor.ps1") @docArgs | Out-String) | ConvertFrom-Json
        $payload.doctor = [ordered]@{ ok = $doc.ok; failures = $doc.failures; warnings = $doc.warnings }
        foreach ($c in @($doc.checks)) {
            if ($c.status -in @("fail", "warn") -or $c.id -in @("mcp_startup", "codex", "claude")) {
                $fixHint = if ($c.PSObject.Properties["fix"]) { "  -> $($c.fix)" } else { "" }
                Write-Step "$($c.id): $($c.message)$fixHint" ([string]$c.status)
            }
        }
    } catch { Add-Warn "doctor failed: $($_.Exception.Message)" }
}

Write-Step "Done" "head"
if (-not $Json) {
    Write-Host "  Launcher:  $Launcher"
    Write-Host "  Doctor:    $Bin\vectorworksctl.cmd doctor"
    Write-Host "  Restart Codex / Claude Code so they pick up the vectorworks server."
    if ($native.status -eq "pending_vectorworks_running") { Write-Host "  Native plug-in pending: close Vectorworks and rerun with -FullNative." -ForegroundColor Yellow }
    if ($tokenCreated) { Write-Host "  A new auth token was created: restart Vectorworks." -ForegroundColor Yellow }
}
if ($Json) { $payload | ConvertTo-Json -Depth 10 }
if (-not $payload.ok) { exit 1 }
exit 0
