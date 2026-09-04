[CmdletBinding()]
param(
    [string]$StandalonePluginPath = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BundledPlugin = Join-Path $RepoRoot "plugins\vectorworks"
$ServerPath = Join-Path $RepoRoot "server.py"
$BundledMcpPath = Join-Path $BundledPlugin ".mcp.json"
$BundledClaudeMcpPath = Join-Path $BundledPlugin ".claude-plugin\mcp.json"
$RepoMcpPath = Join-Path $RepoRoot ".mcp.json"
$RepoMarketplacePath = Join-Path $RepoRoot ".agents\plugins\marketplace.json"

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
    ".codex-plugin\plugin.json",
    ".claude-plugin\mcp.json",
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
    "scripts\start-vectorworks-native-smoke.ps1",
    "skills\setup\SKILL.md",
    "skills\ping\SKILL.md",
    "skills\diagnose\SKILL.md",
    "skills\work\SKILL.md"
)
foreach ($RelativePath in $RequiredFiles) {
    Assert-File $RelativePath
}
if (-not (Test-Path -LiteralPath $RepoMarketplacePath -PathType Leaf)) {
    throw "Repository Codex marketplace is missing: $RepoMarketplacePath"
}

$BundledCompanionContract = Join-Path $BundledPlugin "scripts\check-companion-contract.ps1"
& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $BundledCompanionContract -RepoPath $RepoRoot
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$CodexManifest = Get-Content -Raw -LiteralPath (Join-Path $BundledPlugin ".codex-plugin\plugin.json") | ConvertFrom-Json
if ($CodexManifest.name -ne "vectorworks" -or
    $CodexManifest.version -ne "0.6.0" -or
    $CodexManifest.skills -ne "./skills/" -or
    $CodexManifest.mcpServers -ne "./.mcp.json" -or
    $CodexManifest.interface.displayName -ne "Vectorworks MCP") {
    throw "Bundled Codex plugin manifest is not a valid Vectorworks plugin manifest."
}
$ClaudeManifest = Get-Content -Raw -LiteralPath (Join-Path $BundledPlugin ".claude-plugin\plugin.json") | ConvertFrom-Json
if ($ClaudeManifest.name -ne "vectorworks" -or
    $ClaudeManifest.version -ne "0.6.0" -or
    $ClaudeManifest.mcpServers -ne "./.claude-plugin/mcp.json") {
    throw "Bundled Claude Code plugin manifest is not a valid Vectorworks plugin manifest."
}
$Marketplace = Get-Content -Raw -LiteralPath (Join-Path $BundledPlugin ".claude-plugin\marketplace.json") | ConvertFrom-Json
if ($Marketplace.name -ne "vectorworks-claude-plugin" -or $Marketplace.plugins[0].name -ne "vectorworks") {
    throw "Bundled plugin marketplace manifest is invalid."
}
$RepoMarketplace = Get-Content -Raw -LiteralPath $RepoMarketplacePath | ConvertFrom-Json
$RepoMarketplaceEntry = @($RepoMarketplace.plugins)[0]
if ($RepoMarketplace.name -ne "vectorworks-mcp" -or
    $RepoMarketplace.interface.displayName -ne "Vectorworks MCP" -or
    @($RepoMarketplace.plugins).Count -ne 1 -or
    $RepoMarketplaceEntry.name -ne "vectorworks" -or
    $RepoMarketplaceEntry.source.source -ne "local" -or
    $RepoMarketplaceEntry.source.path -ne "./plugins/vectorworks" -or
    $RepoMarketplaceEntry.policy.installation -ne "AVAILABLE" -or
    $RepoMarketplaceEntry.policy.authentication -ne "ON_INSTALL" -or
    $RepoMarketplaceEntry.category -ne "Productivity") {
    throw "Repository Codex marketplace manifest is invalid."
}
$RepoMarketplaceTarget = [System.IO.Path]::GetFullPath(
    (Join-Path $RepoRoot ([string]$RepoMarketplaceEntry.source.path).TrimStart(".", "/", "\"))
)
if ($RepoMarketplaceTarget -ne [System.IO.Path]::GetFullPath($BundledPlugin)) {
    throw "Repository Codex marketplace source does not resolve to the bundled Vectorworks plugin."
}

$RepoMcp = Get-Content -Raw -LiteralPath $RepoMcpPath | ConvertFrom-Json
$BundledMcp = Get-Content -Raw -LiteralPath $BundledMcpPath | ConvertFrom-Json
$BundledClaudeMcp = Get-Content -Raw -LiteralPath $BundledClaudeMcpPath | ConvertFrom-Json
$RepoEnv = $RepoMcp.mcpServers.vectorworks.env
$BundledEnv = $BundledMcp.mcpServers.vectorworks.env
$BundledClaudeEnv = $BundledClaudeMcp.mcpServers.vectorworks.env
foreach ($Key in @("VW_MCP_HOST", "VW_MCP_PORT", "VW_MCP_TIMEOUT", "VW_MCP_PREFLIGHT_CACHE_MS", "VW_MCP_TOOL_PROFILE")) {
    if ($RepoEnv.$Key -ne $BundledEnv.$Key) {
        throw "Bundled Codex MCP env default drift for $Key. Repo=$($RepoEnv.$Key), bundled=$($BundledEnv.$Key)"
    }
    if ($RepoEnv.$Key -ne $BundledClaudeEnv.$Key) {
        throw "Bundled Claude MCP env default drift for $Key. Repo=$($RepoEnv.$Key), bundled=$($BundledClaudeEnv.$Key)"
    }
}
$CodexMcpText = Get-Content -Raw -LiteralPath $BundledMcpPath
$ClaudeMcpText = Get-Content -Raw -LiteralPath $BundledClaudeMcpPath
if ($CodexMcpText -notmatch '\$\{PLUGIN_ROOT\}/scripts/run-vectorworks-mcp\.ps1' -or
    $CodexMcpText -match 'CLAUDE_PLUGIN_ROOT|user_config|EnablePythonDialogFallback|allow-python-fallback') {
    throw "Bundled Codex MCP config must use PLUGIN_ROOT and must not enable the modal Python fallback."
}
if ($ClaudeMcpText -notmatch '\$\{CLAUDE_PLUGIN_ROOT\}/scripts/run-vectorworks-mcp\.ps1' -or
    $ClaudeMcpText -match 'EnablePythonDialogFallback|allow-python-fallback') {
    throw "Bundled Claude MCP config must use CLAUDE_PLUGIN_ROOT and must not enable the modal Python fallback."
}

$Resolver = Get-Content -Raw -LiteralPath (Join-Path $BundledPlugin "scripts\resolve-vectorworks-mcp-repo.ps1")
if ($Resolver -notmatch "InstallIfMissing" -or $Resolver -notmatch "RequireContract" -or $Resolver -notmatch "\.vectorworks-mcp-contract\.json") {
    throw "Bundled resolver must support auto-clone and current connector contract validation."
}
if ($Resolver -notmatch 'MinimumContractVersion\s*=\s*17') {
    throw "Bundled resolver must reject connector contracts older than version 17."
}
foreach ($Feature in @("native-phase4-apply-operations", "fast-native-tool-profile", "structured-mcp-results", "codex-plugin-package", "native-documentation-lifecycle", "native-document-target-binding", "review-all-sheets")) {
    if ($Resolver -notmatch [regex]::Escape($Feature)) {
        throw "Bundled resolver must require connector feature '$Feature'."
    }
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
    "scripts\smoke-native-bridge.ps1",
    "scripts\start-vectorworks-native-smoke.ps1"
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
$FastNativeMatch = [regex]::Match(
    $ServerText,
    'FAST_NATIVE_TOOL_NAMES\s*=\s*frozenset\(\s*\{(?<body>.*?)\}\s*\)',
    [System.Text.RegularExpressions.RegexOptions]::Singleline
)
if (-not $FastNativeMatch.Success) {
    throw "Server is missing the FAST_NATIVE_TOOL_NAMES production manifest."
}
$ServerTools = @(
    [regex]::Matches($FastNativeMatch.Groups['body'].Value, '["''](?<name>vw_[A-Za-z0-9_]+)["'']') |
        ForEach-Object { $_.Groups['name'].Value } |
        Sort-Object -Unique
)
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
if (@($ServerTools | Where-Object { $_ -notin $SafetyTools }).Count -gt 0) {
    throw "Bundled production tools must all have server TOOL_SAFETY entries."
}

if ($StandalonePluginPath) {
    $StandaloneRoot = (Resolve-Path -LiteralPath $StandalonePluginPath).Path
    $CanonicalPaths = @(
        ".mcp.json",
        ".codex-plugin\plugin.json",
        ".claude-plugin\mcp.json",
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
        "scripts\start-vectorworks-native-smoke.ps1",
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
