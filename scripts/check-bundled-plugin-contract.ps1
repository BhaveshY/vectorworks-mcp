[CmdletBinding()]
param(
    [string]$StandalonePluginPath = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BundledPlugin = Join-Path $RepoRoot "plugins\vectorworks"
$ServerPath = Join-Path $RepoRoot "server.py"
$BundledMcpPath = Join-Path $BundledPlugin ".mcp.json"
$RepoMcpPath = Join-Path $RepoRoot ".mcp.json"

function Assert-File {
    param([string]$RelativePath)
    $Path = Join-Path $BundledPlugin $RelativePath
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Bundled plugin is missing $RelativePath"
    }
}

function Test-PythonCommand {
    param(
        [string]$Command,
        [string[]]$Args = @()
    )

    try {
        & $Command @($Args + @("-c", "import sys; sys.exit(0)")) *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Get-FirstPythonCommand {
    $RepoVenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if ((Test-Path -LiteralPath $RepoVenvPython -PathType Leaf) -and (Test-PythonCommand -Command $RepoVenvPython)) {
        return [pscustomobject]@{ Command = $RepoVenvPython; Args = @() }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return [pscustomobject]@{ Command = "py"; Args = @("-3") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return [pscustomobject]@{ Command = "python"; Args = @() }
    }
    throw "Python was not found; cannot validate bundled plugin safety metadata. Run scripts\bootstrap-agent.ps1 first or install Python 3."
}

$RequiredFiles = @(
    ".mcp.json",
    ".claude-plugin\plugin.json",
    ".claude-plugin\marketplace.json",
    "references\tool-map.md",
    "bin\vectorworksctl",
    "bin\vectorworksctl.cmd",
    "bin\vectorworksctl.ps1",
    "scripts\bootstrap-vectorworks-mcp.ps1",
    "scripts\copy-vectorworks-loader.ps1",
    "scripts\copy-native-bridge-scaffold.ps1",
    "scripts\diagnose-vectorworks-mcp.ps1",
    "scripts\doctor-vectorworks-mcp.ps1",
    "scripts\doctor-native-bridge.ps1",
    "scripts\invoke-native-bridge-next.ps1",
    "scripts\resolve-companion-repo.ps1",
    "scripts\resolve-vectorworks-mcp-repo.ps1",
    "scripts\run-vectorworks-mcp.ps1",
    "scripts\test-vectorworks-listener.ps1",
    "scripts\check-companion-contract.ps1",
    "scripts\bootstrap-native-bridge.ps1",
    "scripts\prepare-native-bridge-source.ps1",
    "scripts\build-native-bridge.ps1",
    "scripts\wire-native-bridge-project.ps1",
    "scripts\smoke-native-bridge.ps1",
    "skills\setup\SKILL.md",
    "skills\ping\SKILL.md",
    "skills\diagnose\SKILL.md",
    "skills\work\SKILL.md"
)
foreach ($RelativePath in $RequiredFiles) {
    Assert-File $RelativePath
}

$BundledCompanionContract = Join-Path $BundledPlugin "scripts\check-companion-contract.ps1"
& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $BundledCompanionContract -RepoPath $RepoRoot
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$Manifest = Get-Content -Raw -LiteralPath (Join-Path $BundledPlugin ".claude-plugin\plugin.json") | ConvertFrom-Json
if ($Manifest.name -ne "vectorworks" -or $Manifest.mcpServers -ne "./.mcp.json") {
    throw "Bundled plugin manifest is not a valid Vectorworks plugin manifest."
}
$Marketplace = Get-Content -Raw -LiteralPath (Join-Path $BundledPlugin ".claude-plugin\marketplace.json") | ConvertFrom-Json
if ($Marketplace.name -ne "vectorworks-claude-plugin" -or $Marketplace.plugins[0].name -ne "vectorworks") {
    throw "Bundled plugin marketplace manifest is invalid."
}

$RepoMcp = Get-Content -Raw -LiteralPath $RepoMcpPath | ConvertFrom-Json
$BundledMcp = Get-Content -Raw -LiteralPath $BundledMcpPath | ConvertFrom-Json
$RepoEnv = $RepoMcp.mcpServers.vectorworks.env
$BundledEnv = $BundledMcp.mcpServers.vectorworks.env
foreach ($Key in @("VW_MCP_HOST", "VW_MCP_PORT", "VW_MCP_TIMEOUT", "VW_MCP_PREFLIGHT_CACHE_MS")) {
    if ($RepoEnv.$Key -ne $BundledEnv.$Key) {
        throw "Bundled plugin MCP env default drift for $Key. Repo=$($RepoEnv.$Key), bundled=$($BundledEnv.$Key)"
    }
}

$Resolver = Get-Content -Raw -LiteralPath (Join-Path $BundledPlugin "scripts\resolve-vectorworks-mcp-repo.ps1")
if ($Resolver -notmatch "InstallIfMissing" -or $Resolver -notmatch "RequireContract" -or $Resolver -notmatch "\.vectorworks-mcp-contract\.json") {
    throw "Bundled resolver must support auto-clone and current connector contract validation."
}

$Claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $Claude) {
    $Claude = Get-Command claude.exe -ErrorAction SilentlyContinue
}
if ($Claude) {
    Push-Location $BundledPlugin
    try {
        & $Claude.Source plugin validate .
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Warning "claude CLI not found; skipping official Claude bundled-plugin validation."
}

foreach ($RelativePath in @(
    "scripts\run-vectorworks-mcp.ps1",
    "scripts\bootstrap-vectorworks-mcp.ps1",
    "scripts\copy-vectorworks-loader.ps1",
    "scripts\copy-native-bridge-scaffold.ps1",
    "scripts\diagnose-vectorworks-mcp.ps1",
    "scripts\doctor-vectorworks-mcp.ps1",
    "scripts\test-vectorworks-listener.ps1",
    "scripts\doctor-native-bridge.ps1",
    "scripts\invoke-native-bridge-next.ps1",
    "scripts\bootstrap-native-bridge.ps1",
    "scripts\prepare-native-bridge-source.ps1",
    "scripts\build-native-bridge.ps1",
    "scripts\wire-native-bridge-project.ps1",
    "scripts\smoke-native-bridge.ps1"
)) {
    $Text = Get-Content -Raw -LiteralPath (Join-Path $BundledPlugin $RelativePath)
    if ($Text -notmatch "RequireContract") {
        throw "Bundled wrapper $RelativePath must require the current connector contract."
    }
    if ($Text -notmatch "Resolve-VectorworksMcpCompanionRepo") {
        throw "Bundled wrapper $RelativePath must use the shared companion repo resolver helper."
    }
}

$ServerText = Get-Content -Raw -LiteralPath $ServerPath
$ToolMapText = Get-Content -Raw -LiteralPath (Join-Path $BundledPlugin "references\tool-map.md")
$ServerTools = @([regex]::Matches($ServerText, 'def (vw_[A-Za-z0-9_]+)\(') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
$DocumentedTools = @([regex]::Matches($ToolMapText, '`(vw_[A-Za-z0-9_]+)`') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
if (@($ServerTools | Where-Object { $_ -notin $DocumentedTools }).Count -gt 0 -or
    @($DocumentedTools | Where-Object { $_ -notin $ServerTools }).Count -gt 0) {
    throw "Bundled plugin tool map does not match server tools."
}

$Python = Get-FirstPythonCommand
$env:VW_BUNDLED_PLUGIN_CONTRACT_REPO = $RepoRoot
$SafetyCode = "import json, os, sys; sys.path.insert(0, os.environ['VW_BUNDLED_PLUGIN_CONTRACT_REPO']); import server; print(json.dumps(server.TOOL_SAFETY, sort_keys=True))"
$ToolSafetyJson = & $Python.Command @($Python.Args) -c $SafetyCode
if ($LASTEXITCODE -ne 0) {
    throw "Could not import server TOOL_SAFETY."
}
$ToolSafety = $ToolSafetyJson | ConvertFrom-Json
$SafetyTools = @($ToolSafety.PSObject.Properties.Name | Sort-Object -Unique)
if (@($SafetyTools | Where-Object { $_ -notin $DocumentedTools }).Count -gt 0 -or
    @($DocumentedTools | Where-Object { $_ -notin $SafetyTools }).Count -gt 0) {
    throw "Bundled plugin tool map must match server TOOL_SAFETY exactly."
}

if ($StandalonePluginPath) {
    $StandaloneRoot = (Resolve-Path -LiteralPath $StandalonePluginPath).Path
    $CanonicalPaths = @(
        ".mcp.json",
        ".claude-plugin\plugin.json",
        ".claude-plugin\marketplace.json",
        "references\tool-map.md",
        "bin\vectorworksctl",
        "bin\vectorworksctl.cmd",
        "bin\vectorworksctl.ps1",
        "scripts\bootstrap-vectorworks-mcp.ps1",
        "scripts\copy-vectorworks-loader.ps1",
        "scripts\copy-native-bridge-scaffold.ps1",
        "scripts\diagnose-vectorworks-mcp.ps1",
        "scripts\doctor-vectorworks-mcp.ps1",
        "scripts\doctor-native-bridge.ps1",
        "scripts\invoke-native-bridge-next.ps1",
        "scripts\resolve-companion-repo.ps1",
        "scripts\resolve-vectorworks-mcp-repo.ps1",
        "scripts\run-vectorworks-mcp.ps1",
        "scripts\test-vectorworks-listener.ps1",
        "scripts\check-companion-contract.ps1",
        "scripts\bootstrap-native-bridge.ps1",
        "scripts\prepare-native-bridge-source.ps1",
        "scripts\build-native-bridge.ps1",
        "scripts\wire-native-bridge-project.ps1",
        "scripts\smoke-native-bridge.ps1",
        "skills\setup\SKILL.md",
        "skills\ping\SKILL.md",
        "skills\diagnose\SKILL.md",
        "skills\work\SKILL.md"
    )
    foreach ($RelativePath in $CanonicalPaths) {
        $BundledText = Get-Content -Raw -LiteralPath (Join-Path $BundledPlugin $RelativePath)
        $StandaloneText = Get-Content -Raw -LiteralPath (Join-Path $StandaloneRoot $RelativePath)
        if ($BundledText -ne $StandaloneText) {
            throw "Bundled plugin drift from standalone plugin: $RelativePath"
        }
    }
}

Write-Host "OK: bundled plugin contract passed."
