[CmdletBinding()]
param(
    [string]$HostName = "",
    [ValidateRange(1, 65535)]
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "resolve-companion-repo.ps1")

$PluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PluginManifestPath = Join-Path $PluginRoot ".claude-plugin\plugin.json"
$PluginMarketplacePath = Join-Path $PluginRoot ".claude-plugin\marketplace.json"

if (-not $HostName) {
    $HostName = if ($env:VW_MCP_HOST) { $env:VW_MCP_HOST } else { "127.0.0.1" }
}
if ($Port -eq 0) {
    $Port = if ($env:VW_MCP_PORT) { [int]$env:VW_MCP_PORT } else { 9877 }
}

function Test-TcpPort {
    param(
        [string]$ComputerName,
        [int]$PortNumber,
        [int]$TimeoutMilliseconds = 1000
    )
    $Client = [System.Net.Sockets.TcpClient]::new()
    try {
        $Async = $Client.BeginConnect($ComputerName, $PortNumber, $null, $null)
        if (-not $Async.AsyncWaitHandle.WaitOne($TimeoutMilliseconds, $false)) {
            return $false
        }
        $Client.EndConnect($Async)
        return $true
    } catch {
        return $false
    } finally {
        $Client.Close()
    }
}

function Write-PortDiagnostics {
    param(
        [string]$Address,
        [int]$PortNumber
    )
    $Connections = @(Get-NetTCPConnection -LocalPort $PortNumber -ErrorAction SilentlyContinue)
    if (-not $Connections) {
        Write-Host "Port owner: none found for $Address`:$PortNumber"
        return
    }

    Write-Host "TCP state for local port ${PortNumber}:"
    $Connections |
        Select-Object LocalAddress,LocalPort,RemoteAddress,RemotePort,State,OwningProcess |
        Format-Table -AutoSize | Out-String | Write-Host

    Write-Host "Owning process(es):"
    $Connections |
        Select-Object -ExpandProperty OwningProcess -Unique |
        ForEach-Object {
            Get-Process -Id $_ -ErrorAction SilentlyContinue |
                Select-Object Id,ProcessName,Path |
                Format-List | Out-String | Write-Host
        }
}

function ConvertTo-PythonRawStringLiteralText {
    param([string]$Value)
    return $Value.Replace("\", "\\").Replace('"', '\"')
}

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
    } else {
        Write-Host "Plugin marketplace: missing at $PluginMarketplacePath"
    }
}

function Get-ConnectorContractDiagnostics {
    param([string]$RepoPath)

    $ContractPath = Join-Path $RepoPath ".vectorworks-mcp-contract.json"
    if (-not (Test-Path -LiteralPath $ContractPath)) {
        return [pscustomobject]@{
            status = "missing"
            version = $null
            features = @()
            detail = $ContractPath
        }
    }

    try {
        $Contract = Get-Content -Raw -LiteralPath $ContractPath | ConvertFrom-Json
        $Version = [int]$Contract.contractVersion
        $Features = @($Contract.requiredFeatures | ForEach-Object { [string]$_ })
        return [pscustomobject]@{
            status = "ok"
            version = $Version
            features = $Features
            detail = ("version={0}; features={1}" -f $Version, ($Features -join ", "))
        }
    } catch {
        return [pscustomobject]@{
            status = "error"
            version = $null
            features = @()
            detail = $_.Exception.Message
        }
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

function Get-LoaderMetadataDiagnostics {
    param(
        [string]$RepoPath,
        [string]$LauncherPath,
        [string]$LoaderPath,
        [pscustomobject]$ContractInfo
    )

    if (-not (Test-Path -LiteralPath $LoaderPath)) {
        return [pscustomobject]@{
            status = "missing"
            detail = $LoaderPath
        }
    }

    $LoaderText = Get-Content -Raw -LiteralPath $LoaderPath
    $Problems = [System.Collections.Generic.List[string]]::new()
    if (-not $LoaderText.Contains("VW_MCP_LOADER_METADATA")) {
        $Problems.Add("missing VW_MCP_LOADER_METADATA") | Out-Null
    }

    $ExpectedRepoLiteral = ConvertTo-PythonRawStringLiteralText $RepoPath
    if (-not $LoaderText.Contains($ExpectedRepoLiteral)) {
        $Problems.Add("repoRoot does not match $RepoPath") | Out-Null
    }

    $ExpectedLauncherLiteral = ConvertTo-PythonRawStringLiteralText $LauncherPath
    if (-not $LoaderText.Contains($ExpectedLauncherLiteral)) {
        $Problems.Add("launcherPath does not match $LauncherPath") | Out-Null
    }

    if ($ContractInfo -and $ContractInfo.status -eq "ok") {
        if ($LoaderText -notmatch ('"contractVersion":\s*' + [regex]::Escape([string]$ContractInfo.version))) {
            $Problems.Add("contractVersion does not match $($ContractInfo.version)") | Out-Null
        }
        foreach ($Feature in @($ContractInfo.features)) {
            if (-not $LoaderText.Contains('"' + $Feature + '"')) {
                $Problems.Add("missing required feature $Feature") | Out-Null
            }
        }
    }

    $GeneratedAt = ""
    if ($LoaderText -match '"generatedAtUtc":\s*"([^"]+)"') {
        $GeneratedAt = $Matches[1]
    }

    if ($Problems.Count -gt 0) {
        $Detail = ($Problems -join "; ")
        if ($GeneratedAt) { $Detail += "; generatedAtUtc=$GeneratedAt" }
        return [pscustomobject]@{
            status = "stale"
            detail = $Detail
        }
    }

    $OkDetail = "metadata matches repo, launcher, and contract"
    if ($GeneratedAt) { $OkDetail += "; generatedAtUtc=$GeneratedAt" }
    return [pscustomobject]@{
        status = "ok"
        detail = $OkDetail
    }
}

function Test-ConnectorContractGate {
    param([string]$RepoPath)

    $StrictArgs = @("-RepoPath", $RepoPath, "-RequireContract")
    try {
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Resolver @StrictArgs | Out-Null
        return [pscustomobject]@{
            status = "ok"
            detail = "strict companion contract accepted"
        }
    } catch {
        return [pscustomobject]@{
            status = "error"
            detail = $_.Exception.Message
        }
    }
}

Write-PluginIdentityDiagnostics

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$RepoRoot = $null
try {
    $ResolverArgs = @()
    if ($env:VW_MCP_REPO) { $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO) }
    $RepoRoot = Resolve-VectorworksMcpCompanionRepo -ResolverArgs $ResolverArgs
} catch {
    Write-Host "Repo: NOT FOUND"
    Write-Host $_.Exception.Message
}

if ($RepoRoot) {
    $Launcher = Join-Path $RepoRoot "vw_start_listener_2024.py"
    $Loader = Join-Path $RepoRoot "vw_load_listener_2024.py"
    $LauncherOk = (Test-Path -LiteralPath $Launcher) -and ((Get-Content -Raw -Path $Launcher) -match 'os\.environ\["VW_MCP_MODE"\]\s*=\s*["'']dialog["'']')
    $NativeBridgeDir = Join-Path $RepoRoot "native_bridge"
    $NativeChecker = Join-Path $RepoRoot "scripts\check-native-bridge-prereqs.ps1"
    $ContractInfo = Get-ConnectorContractDiagnostics -RepoPath $RepoRoot
    $ContractGateInfo = Test-ConnectorContractGate -RepoPath $RepoRoot
    $LoaderMetadataInfo = Get-LoaderMetadataDiagnostics -RepoPath $RepoRoot -LauncherPath $Launcher -LoaderPath $Loader -ContractInfo $ContractInfo
    Write-Host "Repo: $RepoRoot"
    Write-Host "Connector contract: $($ContractInfo.status) - $($ContractInfo.detail)"
    Write-Host "Connector contract gate: $($ContractGateInfo.status) - $($ContractGateInfo.detail)"
    Write-ConnectorGitDiagnostics -RepoPath $RepoRoot
    Write-Host "Generated launcher: $Launcher"
    Write-Host "Generated Vectorworks loader: $Loader"
    Write-Host "Generated loader metadata: $($LoaderMetadataInfo.status) - $($LoaderMetadataInfo.detail)"
    Write-Host "Launcher agent-session mode: $LauncherOk"
    Write-Host "Native bridge scaffold: $([bool](Test-Path -LiteralPath $NativeBridgeDir))"
    Write-Host "Native bridge prereq checker: $([bool](Test-Path -LiteralPath $NativeChecker))"
}

$Claude = Get-Command claude -ErrorAction SilentlyContinue
Write-Host "claude on PATH: $([bool]$Claude)"
if ($Claude) { Write-Host "claude path: $($Claude.Source)" }
$TcpReachable = Test-TcpPort -ComputerName $HostName -PortNumber $Port
Write-Host "Listener TCP $HostName`:$Port reachable: $TcpReachable"
Write-PortDiagnostics -Address $HostName -PortNumber $Port

if ($RepoRoot -and $TcpReachable) {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "test-vectorworks-listener.ps1") -HostName $HostName -Port $Port
}
