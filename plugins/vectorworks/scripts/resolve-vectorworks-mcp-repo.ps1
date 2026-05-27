[CmdletBinding()]
param(
    [string]$RepoPath = "",
    [switch]$InstallIfMissing,
    [string]$InstallRoot = "",
    [string]$InstallRepoUrl = "https://github.com/BhaveshY/vectorworks-mcp.git",
    [switch]$RequireContract,
    [ValidateRange(1, 100)]
    [int]$MinimumContractVersion = 9,
    [string[]]$RequiredFeatures = @("stable-loader", "loader-clipboard-copy", "native-bridge-scaffold", "native-bridge-scaffold-copy", "native-doctor-next-command", "native-doctor-command-spec", "native-bridge-project-wire", "native-doctor-next-runner")
)

$ErrorActionPreference = "Stop"
$RejectedRepos = [System.Collections.Generic.List[string]]::new()

function Test-VectorworksMcpRepo {
    param(
        [string]$Path,
        [switch]$RequireContract
    )
    if (-not $Path) { return $false }
    try {
        $Resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    } catch {
        return $false
    }
    $LooksLikeRepo = (
        (Test-Path -LiteralPath (Join-Path $Resolved "server.py")) -and
        (Test-Path -LiteralPath (Join-Path $Resolved "vw_listener.py")) -and
        (Test-Path -LiteralPath (Join-Path $Resolved "scripts\run-mcp-server.ps1"))
    )
    if (-not $LooksLikeRepo) {
        return $false
    }

    if (-not $RequireContract) {
        return $true
    }

    $ContractPath = Join-Path $Resolved ".vectorworks-mcp-contract.json"
    if (-not (Test-Path -LiteralPath $ContractPath)) {
        $script:RejectedRepos.Add("$Resolved (missing .vectorworks-mcp-contract.json)") | Out-Null
        return $false
    }
    try {
        $Contract = Get-Content -Raw -LiteralPath $ContractPath | ConvertFrom-Json
    } catch {
        $script:RejectedRepos.Add("$Resolved (invalid .vectorworks-mcp-contract.json)") | Out-Null
        return $false
    }
    if ($Contract.name -ne "vectorworks-mcp") {
        $script:RejectedRepos.Add("$Resolved (contract name is '$($Contract.name)')") | Out-Null
        return $false
    }
    try {
        $ContractVersion = [int]$Contract.contractVersion
    } catch {
        $script:RejectedRepos.Add("$Resolved (missing numeric contractVersion)") | Out-Null
        return $false
    }
    if ($ContractVersion -lt $MinimumContractVersion) {
        $script:RejectedRepos.Add("$Resolved (contractVersion $($Contract.contractVersion) < $MinimumContractVersion)") | Out-Null
        return $false
    }
    $ContractFeatures = @($Contract.requiredFeatures | ForEach-Object { [string]$_ })
    foreach ($RequiredFeature in $RequiredFeatures) {
        if ($RequiredFeature -notin $ContractFeatures) {
            $script:RejectedRepos.Add("$Resolved (missing required feature '$RequiredFeature')") | Out-Null
            return $false
        }
    }
    return $true
}

function Add-Candidate {
    param(
        [System.Collections.Generic.List[string]]$Candidates,
        [string]$Path
    )
    if ($Path -match '^\$\{.*\}$') { return }
    if ($Path -and -not $Candidates.Contains($Path)) {
        $Candidates.Add($Path)
    }
}

$Candidates = [System.Collections.Generic.List[string]]::new()
Add-Candidate $Candidates $RepoPath
Add-Candidate $Candidates $env:VW_MCP_REPO
Add-Candidate $Candidates $env:VECTORWORKS_MCP_REPO
Add-Candidate $Candidates $env:CLAUDE_PROJECT_DIR
Add-Candidate $Candidates (Get-Location).Path
Add-Candidate $Candidates (Join-Path $PSScriptRoot "..\..\..")

if ($env:USERPROFILE) {
    Add-Candidate $Candidates (Join-Path $env:USERPROFILE "repos\vectorworks-mcp")
    Add-Candidate $Candidates (Join-Path $env:USERPROFILE "Downloads\vectorworks-mcp")
}

if ($RepoPath) {
    if (Test-VectorworksMcpRepo $RepoPath -RequireContract:$($RequireContract.IsPresent)) {
        Write-Output (Resolve-Path -LiteralPath $RepoPath).Path
        exit 0
    }
    if ($RequireContract -and $RejectedRepos.Count -gt 0) {
        $Rejected = ($RejectedRepos | ForEach-Object { "  - $_" }) -join [Environment]::NewLine
        throw "The requested vectorworks-mcp checkout does not satisfy the plugin companion contract. Update it with git pull, remove the stale folder, or set VW_MCP_REPO to a current checkout:$([Environment]::NewLine)$Rejected"
    }
}

foreach ($Candidate in $Candidates) {
    if (Test-VectorworksMcpRepo $Candidate -RequireContract:$($RequireContract.IsPresent)) {
        Write-Output (Resolve-Path -LiteralPath $Candidate).Path
        exit 0
    }
}

if ($RejectedRepos.Count -gt 0) {
    $Rejected = ($RejectedRepos | ForEach-Object { "  - $_" }) -join [Environment]::NewLine
    throw "Found vectorworks-mcp checkout(s), but they do not satisfy the plugin companion contract. Update them with git pull, remove stale folders, or set VW_MCP_REPO to a current checkout:$([Environment]::NewLine)$Rejected"
}

if ($InstallIfMissing) {
    $Git = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $Git) {
        throw "Could not find the vectorworks-mcp repo and git.exe is not available to clone it. Install Git, set plugin user config 'vectorworks_repo', or set VW_MCP_REPO."
    }
    if (-not $InstallRoot) {
        if (-not $env:USERPROFILE) {
            throw "Could not choose an install folder because USERPROFILE is not set. Pass -InstallRoot or set VW_MCP_REPO."
        }
        $InstallRoot = Join-Path $env:USERPROFILE "repos"
    }

    $Target = Join-Path $InstallRoot "vectorworks-mcp"
    if (Test-Path -LiteralPath $Target) {
        throw "Install target exists but is not a valid vectorworks-mcp repo: $Target. Fix or remove it, or set VW_MCP_REPO."
    }

    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    Write-Host "Cloning vectorworks-mcp connector:"
    Write-Host $InstallRepoUrl
    Write-Host "Target: $Target"
    & $Git.Source clone $InstallRepoUrl $Target
    if ($LASTEXITCODE -ne 0) {
        throw "git clone failed with exit code $LASTEXITCODE"
    }
    if (-not (Test-VectorworksMcpRepo $Target -RequireContract:$($RequireContract.IsPresent))) {
        throw "Cloned repo does not look like vectorworks-mcp: $Target"
    }
    Write-Output (Resolve-Path -LiteralPath $Target).Path
    exit 0
}

$Tried = ($Candidates | Where-Object { $_ } | ForEach-Object { "  - $_" }) -join [Environment]::NewLine
throw "Could not find the vectorworks-mcp repo. Run /vectorworks:setup to clone it, set plugin user config 'vectorworks_repo', or set VW_MCP_REPO to the repo folder. Tried:$([Environment]::NewLine)$Tried"
