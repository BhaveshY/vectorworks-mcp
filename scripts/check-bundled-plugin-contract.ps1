<#
.SYNOPSIS
  Static checks for the bundled Codex + Claude plugin and marketplaces.

.DESCRIPTION
  - one version everywhere: vectorworks_mcp.__version__, both plugin manifests,
    both marketplaces;
  - plugins are skills-only (no mcpServers, no .mcp.json, no ${PLUGIN_ROOT} /
    ${CLAUDE_PLUGIN_ROOT}, no VW_MCP_PORT pin) - the server is registered once
    per client by install.ps1;
  - every skill has SKILL.md with name + description front matter (Codex also
    gets agents/openai.yaml);
  - no leftover 0.x plugin scripts.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$plugin = Join-Path $repo "plugins\vectorworks"
$problems = New-Object System.Collections.Generic.List[string]
function Fail([string]$m) { $problems.Add($m) | Out-Null; Write-Host "FAIL $m" -ForegroundColor Red }
function Ok([string]$m) { Write-Host "ok   $m" }
function Read-Json([string]$p) { Get-Content -LiteralPath (Join-Path $repo $p) -Raw | ConvertFrom-Json }

$init = Get-Content -LiteralPath (Join-Path $repo "vectorworks_mcp\__init__.py") -Raw
$version = [regex]::Match($init, '__version__\s*=\s*"([^"]+)"').Groups[1].Value
$codexManifest = Read-Json "plugins\vectorworks\.codex-plugin\plugin.json"
$claudeManifest = Read-Json "plugins\vectorworks\.claude-plugin\plugin.json"
$codexMarket = Read-Json ".agents\plugins\marketplace.json"
$claudeMarket = Read-Json ".claude-plugin\marketplace.json"
$versions = [ordered]@{
    "vectorworks_mcp.__version__" = $version
    ".codex-plugin/plugin.json" = $codexManifest.version
    ".claude-plugin/plugin.json" = $claudeManifest.version
    ".agents/plugins/marketplace.json" = (@($codexMarket.plugins | Where-Object { $_.name -eq "vectorworks" })[0]).version
    ".claude-plugin/marketplace.json" = (@($claudeMarket.plugins | Where-Object { $_.name -eq "vectorworks" })[0]).version
}
$distinct = @($versions.Values | Sort-Object -Unique)
if ($distinct.Count -ne 1) { Fail ("version mismatch: " + (($versions.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ", ")) }
else { Ok "one version everywhere: $($distinct[0])" }

if ((@($codexMarket.plugins)[0]).source.path -ne "./plugins/vectorworks") { Fail "Codex marketplace must point at ./plugins/vectorworks" } else { Ok "Codex marketplace source" }
if ((@($claudeMarket.plugins)[0]).source -ne "./plugins/vectorworks") { Fail "Claude marketplace must point at ./plugins/vectorworks" } else { Ok "Claude marketplace source" }

foreach ($m in @(@{ n = "codex"; j = $codexManifest }, @{ n = "claude"; j = $claudeManifest })) {
    if ($m.j.PSObject.Properties["mcpServers"]) { Fail "$($m.n) plugin manifest defines mcpServers (plugins must be skills-only)" } else { Ok "$($m.n) plugin is skills-only" }
}
if ($codexManifest.skills -ne "./skills/") { Fail "Codex manifest skills must be ./skills/" }
foreach ($f in @(".mcp.json", ".claude-plugin\mcp.json", ".claude-plugin\marketplace.json", "scripts", "references")) {
    if (Test-Path -LiteralPath (Join-Path $plugin $f)) { Fail "stale plugin path present: plugins\vectorworks\$f" }
}
$allText = Get-ChildItem -LiteralPath $plugin -Recurse -File | ForEach-Object { [pscustomobject]@{ f = $_.FullName; t = (Get-Content -LiteralPath $_.FullName -Raw) } }
foreach ($x in $allText) {
    foreach ($bad in @('${PLUGIN_ROOT}', '${CLAUDE_PLUGIN_ROOT}', 'VW_MCP_PORT', 'run-vectorworks-mcp.ps1', 'run-mcp-server.ps1')) {
        if ($x.t -and $x.t.Contains($bad)) { Fail "$($x.f) contains $bad" }
    }
}
Ok "no plugin-root variables, port pins or 0.x runner references"

$skills = @(Get-ChildItem -LiteralPath (Join-Path $plugin "skills") -Directory)
if ($skills.Count -lt 1) { Fail "no skills" }
foreach ($s in $skills) {
    $md = Join-Path $s.FullName "SKILL.md"
    if (-not (Test-Path -LiteralPath $md)) { Fail "$($s.Name): SKILL.md missing"; continue }
    $t = Get-Content -LiteralPath $md -Raw
    $fm = [regex]::Match($t, '^---\r?\n(.*?)\r?\n---', 'Singleline')
    if (-not $fm.Success) { Fail "$($s.Name): no front matter"; continue }
    $nm = [regex]::Match($fm.Groups[1].Value, '(?m)^name:\s*(.+)$').Groups[1].Value.Trim()
    $desc = [regex]::Match($fm.Groups[1].Value, '(?m)^description:\s*(.+)$').Groups[1].Value.Trim()
    if ($nm -ne $s.Name) { Fail "$($s.Name): front matter name '$nm' differs from folder" }
    if ($desc.Length -lt 40 -or $desc.Length -gt 1024) { Fail "$($s.Name): description length $($desc.Length) (40..1024)" }
    if (-not (Test-Path -LiteralPath (Join-Path $s.FullName "agents\openai.yaml"))) { Fail "$($s.Name): agents/openai.yaml missing (Codex)" }
    Ok "skill $($s.Name) ($([math]::Round($t.Length / 1024, 1)) KB)"
}
foreach ($cmd in @("bin\vectorworksctl.cmd")) {
    $bytes = [System.IO.File]::ReadAllBytes((Join-Path $plugin $cmd))
    $text = [System.Text.Encoding]::ASCII.GetString($bytes)
    if ($text -match "[^\r]\n") { Fail "$cmd must use CRLF line endings" } else { Ok "$cmd CRLF" }
}

if ($problems.Count) { Write-Host "$($problems.Count) problem(s)" -ForegroundColor Red; exit 1 }
Write-Host "bundled plugin contract OK"
