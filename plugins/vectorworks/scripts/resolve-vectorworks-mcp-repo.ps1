[CmdletBinding()]
param(
    [string]$RepoPath = ""
)

$ErrorActionPreference = "Stop"

function Test-VectorworksMcpRepo {
    param([string]$Path)
    if (-not $Path) { return $false }
    try {
        $Resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    } catch {
        return $false
    }
    return (
        (Test-Path -LiteralPath (Join-Path $Resolved "server.py")) -and
        (Test-Path -LiteralPath (Join-Path $Resolved "vw_listener.py")) -and
        (Test-Path -LiteralPath (Join-Path $Resolved "scripts\run-mcp-server.ps1"))
    )
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

foreach ($Candidate in $Candidates) {
    if (Test-VectorworksMcpRepo $Candidate) {
        Write-Output (Resolve-Path -LiteralPath $Candidate).Path
        exit 0
    }
}

$Tried = ($Candidates | Where-Object { $_ } | ForEach-Object { "  - $_" }) -join [Environment]::NewLine
throw "Could not find the vectorworks-mcp repo. Set plugin user config 'vectorworks_repo' or VW_MCP_REPO to the repo folder. Tried:$([Environment]::NewLine)$Tried"
