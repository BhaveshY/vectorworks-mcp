$script = Join-Path $PSScriptRoot "vectorworksctl"
$scriptArgs = $args
$versionCheck = "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"

function Invoke-PythonCandidate {
    param(
        [string]$Executable,
        [string[]]$PrefixArgs,
        [string[]]$ScriptArgs
    )

    & $Executable @PrefixArgs -c $versionCheck 2>$null
    if ($LASTEXITCODE -eq 0) {
        & $Executable @PrefixArgs $script @ScriptArgs
        exit $LASTEXITCODE
    }
}

$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
    Invoke-PythonCandidate -Executable $py.Source -PrefixArgs @("-3") -ScriptArgs $scriptArgs
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    Invoke-PythonCandidate -Executable $python.Source -PrefixArgs @() -ScriptArgs $scriptArgs
}

Write-Error "Vectorworks error: Python 3.10+ is required. Install it with: winget install --id Python.Python.3.12 -e"
exit 1
