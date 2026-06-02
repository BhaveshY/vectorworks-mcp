$script = Join-Path $PSScriptRoot "vectorworksctl"
$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    & $python.Source $script @args
    exit $LASTEXITCODE
}

$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
    & $py.Source -3 $script @args
    exit $LASTEXITCODE
}

Write-Error "Vectorworks error: Python 3 is required. Install Python for Windows or make python/py available on PATH."
exit 1
