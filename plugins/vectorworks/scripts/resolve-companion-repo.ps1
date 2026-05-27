$script:VectorworksMcpPluginScriptsRoot = $PSScriptRoot

function Resolve-VectorworksMcpCompanionRepo {
    param(
        [string[]]$ResolverArgs = @()
    )

    $Resolver = Join-Path $script:VectorworksMcpPluginScriptsRoot "resolve-vectorworks-mcp-repo.ps1"
    if (-not (Test-Path -LiteralPath $Resolver -PathType Leaf)) {
        throw "Vectorworks MCP repo resolver was not found at $Resolver"
    }

    $PreviousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $ResolverOutput = @(& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Resolver @ResolverArgs 2>&1)
        $ResolverExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    if ($ResolverExitCode -ne 0) {
        $ResolverDetails = ($ResolverOutput | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
        throw "Failed to resolve vectorworks-mcp companion repo; resolver exited with code $ResolverExitCode.$([Environment]::NewLine)$ResolverDetails"
    }

    $RepoRoot = ($ResolverOutput |
        Where-Object { $null -ne $_ -and -not [string]::IsNullOrWhiteSpace([string]$_) } |
        Select-Object -Last 1)
    if (-not $RepoRoot) {
        throw "Failed to resolve vectorworks-mcp companion repo; resolver returned no repo path."
    }

    try {
        $ResolvedRepoRoot = (Resolve-Path -LiteralPath ([string]$RepoRoot).Trim() -ErrorAction Stop).Path
    } catch {
        throw "Resolved vectorworks-mcp companion repo path does not exist: $RepoRoot"
    }

    $RequiredFiles = @(
        "server.py",
        "vw_listener.py",
        "scripts\run-mcp-server.ps1"
    )
    $MissingFiles = @($RequiredFiles | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $ResolvedRepoRoot $_) -PathType Leaf)
    })
    if ($MissingFiles.Count -gt 0) {
        throw "Resolved vectorworks-mcp companion repo is missing required file(s): $($MissingFiles -join ', '). Path: $ResolvedRepoRoot"
    }

    return $ResolvedRepoRoot
}
