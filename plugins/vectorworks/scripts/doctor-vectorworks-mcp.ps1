[CmdletBinding()]
param(
    [string]$RepoPath = "",
    [string]$HostName = "",
    [ValidateRange(1, 65535)]
    [int]$Port = 0,
    [ValidateRange(100, 30000)]
    [int]$TimeoutMilliseconds = 1200,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")

$PluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PluginManifestPath = Join-Path $PluginRoot ".claude-plugin\plugin.json"
$PluginMarketplacePath = Join-Path $PluginRoot ".claude-plugin\marketplace.json"
$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"

function Write-PluginIdentityDiagnostics {
    Write-Host "Plugin root: $PluginRoot"

    if (Test-Path -LiteralPath $PluginManifestPath) {
        try {
            $Manifest = Get-Content -Raw -LiteralPath $PluginManifestPath | ConvertFrom-Json
            Write-Host "Plugin version: $($Manifest.name) $($Manifest.version)"
            if ($Manifest.repository) {
                Write-Host "Plugin repository: $($Manifest.repository)"
            }
        } catch {
            Write-Host "Plugin version: unreadable manifest - $($_.Exception.Message)"
        }
    } else {
        Write-Host "Plugin version: manifest missing at $PluginManifestPath"
    }

    if (Test-Path -LiteralPath $PluginMarketplacePath) {
        try {
            $Marketplace = Get-Content -Raw -LiteralPath $PluginMarketplacePath | ConvertFrom-Json
            $PluginSource = if ($Marketplace.plugins -and $Marketplace.plugins.Count -gt 0) { $Marketplace.plugins[0].source } else { "" }
            Write-Host "Plugin marketplace: $($Marketplace.name); source=$PluginSource"
        } catch {
            Write-Host "Plugin marketplace: unreadable - $($_.Exception.Message)"
        }
    }
}

function Get-ConnectorContractDiagnostics {
    param([string]$RepoPath)

    $ContractPath = Join-Path $RepoPath ".vectorworks-mcp-contract.json"
    if (-not (Test-Path -LiteralPath $ContractPath)) {
        return "missing - $ContractPath"
    }
    try {
        $Contract = Get-Content -Raw -LiteralPath $ContractPath | ConvertFrom-Json
        $Features = @($Contract.requiredFeatures | ForEach-Object { [string]$_ })
        return ("ok - version={0}; features={1}" -f ([int]$Contract.contractVersion), ($Features -join ", "))
    } catch {
        return "error - $($_.Exception.Message)"
    }
}

function Write-ConnectorGitDiagnostics {
    param([string]$RepoPath)

    $Git = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $Git) {
        $Git = Get-Command git -ErrorAction SilentlyContinue
    }
    if (-not $Git) {
        Write-Host "Connector git: git not found on PATH"
        return
    }

    try {
        $Branch = (& $Git.Source -C $RepoPath rev-parse --abbrev-ref HEAD 2>$null | Out-String).Trim()
        $Head = (& $Git.Source -C $RepoPath rev-parse --short HEAD 2>$null | Out-String).Trim()
        $Porcelain = (& $Git.Source -C $RepoPath status --porcelain 2>$null | Out-String)
        if (-not $Branch) { $Branch = "unknown" }
        if (-not $Head) { $Head = "unknown" }
        $Dirty = -not [string]::IsNullOrWhiteSpace($Porcelain)
        Write-Host "Connector git: branch=$Branch; head=$Head; dirty=$Dirty"
    } catch {
        Write-Host "Connector git: unavailable - $($_.Exception.Message)"
    }
}

if (-not $Json) {
    Write-PluginIdentityDiagnostics
    try {
        $SoftResolverArgs = @()
        if ($RepoPath) { $SoftResolverArgs += @("-RepoPath", $RepoPath) }
        elseif ($env:VW_MCP_REPO) { $SoftResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
        $SoftRepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $SoftResolverArgs
        if ($SoftRepoRoot) {
            Write-Host "Repo: $SoftRepoRoot"
            Write-Host "Connector contract: $(Get-ConnectorContractDiagnostics -RepoPath $SoftRepoRoot)"
            Write-ConnectorGitDiagnostics -RepoPath $SoftRepoRoot
        }
    } catch {
        Write-Host "Repo: NOT FOUND"
        Write-Host $_.Exception.Message
    }
}

$ResolverArgs = @("-RequireContract")
if ($RepoPath) { $ResolverArgs += @("-RepoPath", $RepoPath) }
elseif ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
$RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs
$Doctor = Join-Path $RepoRoot "scripts\doctor-vectorworks-mcp.ps1"

if (-not (Test-Path -LiteralPath $Doctor)) {
    throw "Companion repo doctor script was not found at $Doctor"
}

$Args = @()
if ($HostName) { $Args += @("-HostName", $HostName) }
if ($Port -ne 0) { $Args += @("-Port", $Port) }
$Args += @("-TimeoutMilliseconds", $TimeoutMilliseconds)
if ($Json) { $Args += "-Json" }

& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Doctor @Args
exit $LASTEXITCODE
