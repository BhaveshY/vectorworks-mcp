<#
.SYNOPSIS
  Everything that can be verified without Vectorworks (used by CI).

.DESCRIPTION
  1. PowerShell parse check of install.ps1 and scripts\*.ps1
  2. python compileall (vectorworks_mcp, legacy, scripts)
  3. repair-mode server stdio test (legacy + 2026-07-28 eras)
  4. client registration fixture tests (Codex TOML / Claude JSON)
  5. bundled plugin contract (versions, skills-only, front matter)
  6. skill/doc tool names + example arguments against the live tools/list
  7. host end-to-end test against the mock bridge + unit tests in tests\ (if any)
  8. (unless -SkipInstall) sandbox install into a temp root with -Client none:
     launcher -> initialize -> tools/list timing, repair mode via the launcher,
     idempotent re-run, uninstall
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "install-common.ps1")
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Python) { $f = Find-BasePython; if (-not $f) { throw "Python 3.11+ is required" }; $Python = $f.executable }
$failed = New-Object System.Collections.Generic.List[string]

function Step([string]$Label, [scriptblock]$Body) {
    Write-Host ""
    Write-Host "== $Label" -ForegroundColor Cyan
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        & $Body
        if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "exit code $LASTEXITCODE" }
        Write-Host ("   passed ({0:n1} s)" -f $sw.Elapsed.TotalSeconds) -ForegroundColor Green
    } catch {
        $failed.Add("$Label - $($_.Exception.Message)") | Out-Null
        Write-Host "   FAILED: $($_.Exception.Message)" -ForegroundColor Red
    }
    $global:LASTEXITCODE = 0
}

function Py([string[]]$PyArgs) {
    & $Python @PyArgs
    if ($LASTEXITCODE -ne 0) { throw "$($PyArgs -join ' ') exited $LASTEXITCODE" }
}

Step "PowerShell parse check" {
    $bad = @()
    foreach ($f in @(Get-Item (Join-Path $repo "install.ps1")) + @(Get-ChildItem (Join-Path $repo "scripts") -Filter *.ps1)) {
        $errs = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$errs)
        if ($errs) { $bad += "$($f.Name): $($errs[0].Message) (line $($errs[0].Extent.StartLineNumber))" }
    }
    if ($bad) { throw ($bad -join "; ") }
}

Step "python compileall" {
    $dirs = @("vectorworks_mcp", "legacy", "scripts") | ForEach-Object { Join-Path $repo $_ } | Where-Object { Test-Path -LiteralPath $_ }
    $env:PYTHONPYCACHEPREFIX = Join-Path ([IO.Path]::GetTempPath()) "vwmcp-verify-pycache"
    Py (@("-m", "compileall", "-q") + $dirs)
    Remove-Item Env:\PYTHONPYCACHEPREFIX
}

Step "repair server (stdio, both protocol eras)" { Py @((Join-Path $repo "scripts\test-repair-server.py")) }
Step "client registration fixtures" { Py @((Join-Path $repo "scripts\test-register-clients.py")) }
Step "bundled plugin contract" { & (Join-Path $repo "scripts\check-bundled-plugin-contract.ps1") }
Step "skill/doc tool names vs live tools/list" { Py @((Join-Path $repo "scripts\check-skill-tool-names.py"), "--live") }

if (Test-Path -LiteralPath (Join-Path $repo "scripts\e2e_host.py")) {
    Step "host end-to-end vs mock bridge (scripts\e2e_host.py)" { Py @((Join-Path $repo "scripts\e2e_host.py")) }
}

$unit = @(Get-ChildItem (Join-Path $repo "tests") -Filter "test_*.py" -ErrorAction SilentlyContinue)
if ($unit.Count) { Step "unit tests (tests\)" { Push-Location $repo; try { Py @("-m", "unittest", "discover", "-s", "tests", "-v") } finally { Pop-Location } } }

if (-not $SkipInstall) {
    $root = Join-Path ([IO.Path]::GetTempPath()) ("vwmcp-verify-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
    Step "sandbox install (temp root, no clients, no native)" {
        & (Join-Path $repo "install.ps1") -Root $root -Client none -SkipNative -SkipDoctor -Python $Python
        if ($LASTEXITCODE -ne 0) { throw "install.ps1 exited $LASTEXITCODE" }
    }
    $launcher = Join-Path $root "bin\vectorworks-mcp.cmd"
    Step "launcher -> initialize -> tools/list" {
        $raw = & $Python (Join-Path $repo "scripts\doctor_probe.py") startup --launcher $launcher --runs 3
        $st = ($raw | Out-String) | ConvertFrom-Json
        if (-not $st.ok) { throw "probe failed: $($st.stderr_tail)" }
        if ($st.repair_mode) { throw "launcher started repair mode" }
        if (@($st.tools).Count -ne 10) { throw "expected 10 tools, got $(@($st.tools).Count)" }
        Write-Host "   tools/list after $($st.tools_list_ms) ms (first run $($st.first_run.tools_list_ms) ms)"
    }
    Step "idempotent re-run reuses the runtime" {
        $out = & (Join-Path $repo "install.ps1") -Root $root -Client none -SkipNative -SkipDoctor -Python $Python *>&1 | Out-String
        if ($out -notmatch "reusing") { throw "second install did not reuse the runtime" }
    }
    Step "repair mode through the launcher" {
        Move-Item (Join-Path $root "current.txt") (Join-Path $root "current.txt.off")
        try { Py @((Join-Path $repo "scripts\test-repair-server.py"), "--launcher", $launcher) }
        finally { Move-Item (Join-Path $root "current.txt.off") (Join-Path $root "current.txt") }
    }
    Step "uninstall" {
        & (Join-Path $repo "install.ps1") -Root $root -Uninstall -Client none -CodexHome (Join-Path $root "no-codex") -ClaudeConfig (Join-Path $root "no-claude\.claude.json")
        if (Test-Path -LiteralPath $launcher) { throw "launcher still present after uninstall" }
    }
    Remove-Tree $root
}

Write-Host ""
if ($failed.Count) {
    Write-Host "$($failed.Count) check(s) failed:" -ForegroundColor Red
    $failed | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
Write-Host "No-Vectorworks verification passed." -ForegroundColor Green
