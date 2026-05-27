[CmdletBinding()]
param(
    [string]$RepoPath = "",
    [switch]$Advisory
)

$ErrorActionPreference = "Stop"

$Resolver = Join-Path $PSScriptRoot "resolve-vectorworks-mcp-repo.ps1"
$ResolverArgs = @()
if ($RepoPath) {
    $ResolverArgs += @("-RepoPath", $RepoPath)
} elseif ($env:VW_MCP_REPO) {
    $ResolverArgs += @("-RepoPath", $env:VW_MCP_REPO)
}

try {
    $RepoRoot = (& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Resolver @ResolverArgs | Select-Object -Last 1).Trim()
} catch {
    if ($Advisory) {
        Write-Warning $_.Exception.Message
        exit 0
    }
    throw
}

$RequiredScripts = @(
    "scripts\run-mcp-server.ps1",
    "scripts\register-claude-code.ps1",
    "scripts\verify-no-vectorworks.ps1",
    "scripts\test-vectorworks-listener.ps1",
    "scripts\doctor-vectorworks-mcp.ps1",
    "scripts\check-native-bridge-prereqs.ps1",
    "scripts\bootstrap-native-bridge.ps1",
    "scripts\prepare-native-bridge-source.ps1",
    "scripts\build-native-bridge.ps1",
    "scripts\smoke-native-bridge.ps1"
)

$ContractMarker = Join-Path $RepoRoot ".vectorworks-mcp-contract.json"
if (-not (Test-Path -LiteralPath $ContractMarker)) {
    throw "Companion repo is missing .vectorworks-mcp-contract.json"
}
try {
    $Contract = Get-Content -Raw -LiteralPath $ContractMarker | ConvertFrom-Json
} catch {
    throw "Companion repo has invalid .vectorworks-mcp-contract.json"
}
try {
    $ContractVersion = [int]$Contract.contractVersion
} catch {
    throw "Companion repo contract marker is incompatible. Expected numeric contractVersion >= 2."
}
if ($Contract.name -ne "vectorworks-mcp" -or $ContractVersion -lt 2) {
    throw "Companion repo contract marker is incompatible. Expected vectorworks-mcp contractVersion >= 2."
}

$Missing = @()
foreach ($RelativePath in $RequiredScripts) {
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot $RelativePath))) {
        $Missing += $RelativePath
    }
}

if ($Missing.Count -gt 0) {
    throw "Companion repo is missing required script(s): $($Missing -join ', ')"
}

$ServerPath = Join-Path $RepoRoot "server.py"
$ConnectorMcpPath = Join-Path $RepoRoot ".mcp.json"
$ToolMapPath = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")).Path "references\tool-map.md"
$PluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PluginMcpPath = Join-Path $PluginRoot ".mcp.json"
$ServerText = Get-Content -Raw -LiteralPath $ServerPath
$ToolMapText = Get-Content -Raw -LiteralPath $ToolMapPath

$ServerTools = @([regex]::Matches($ServerText, 'def (vw_[A-Za-z0-9_]+)\(') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
$DocumentedTools = @([regex]::Matches($ToolMapText, '`(vw_[A-Za-z0-9_]+)`') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
$MissingDocs = @($ServerTools | Where-Object { $_ -notin $DocumentedTools })
$StaleDocs = @($DocumentedTools | Where-Object { $_ -notin $ServerTools })

if ($MissingDocs.Count -gt 0 -or $StaleDocs.Count -gt 0) {
    throw "Tool map drift. Missing docs: $($MissingDocs -join ', '); stale docs: $($StaleDocs -join ', ')"
}

function Get-FirstPythonCommand {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return [pscustomobject]@{ Command = "py"; Args = @("-3") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return [pscustomobject]@{ Command = "python"; Args = @() }
    }
    throw "Python was not found; cannot validate companion server safety metadata."
}

function Get-ScriptParameterNames {
    param([string]$Path)
    $Tokens = $null
    $ParseErrors = $null
    $Ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$Tokens, [ref]$ParseErrors)
    if ($ParseErrors.Count -gt 0) {
        throw "Could not parse $Path`: $($ParseErrors[0].Message)"
    }
    if (-not $Ast.ParamBlock) {
        return @()
    }
    return @($Ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
}

$Python = Get-FirstPythonCommand
$env:VW_COMPANION_CONTRACT_REPO = $RepoRoot
$SafetyCode = "import json, os, sys; sys.path.insert(0, os.environ['VW_COMPANION_CONTRACT_REPO']); import server; print(json.dumps(server.TOOL_SAFETY, sort_keys=True))"
$ToolSafetyJson = & $Python.Command @($Python.Args) -c $SafetyCode
if ($LASTEXITCODE -ne 0) {
    throw "Could not import companion server TOOL_SAFETY."
}
$ToolSafety = $ToolSafetyJson | ConvertFrom-Json
$SafetyTools = @($ToolSafety.PSObject.Properties.Name | Sort-Object -Unique)
if (@($SafetyTools | Where-Object { $_ -notin $DocumentedTools }).Count -gt 0 -or
    @($DocumentedTools | Where-Object { $_ -notin $SafetyTools }).Count -gt 0) {
    throw "Tool safety drift. Tool map must match server TOOL_SAFETY exactly."
}

$RequiredSafetyKeys = @("category", "wire_action", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint", "requires_cad_preflight")
$RequiredVariantKeys = @("readOnlyHint", "destructiveHint", "idempotentHint", "writesDocument", "writesFiles", "confirmationRequired")
$AllowedCategories = @(
    "metadata",
    "health",
    "document-read",
    "document-write",
    "document-export",
    "file-write",
    "listener-control",
    "mixed-document-write",
    "mixed-destructive",
    "trusted-code"
)
foreach ($ToolName in $SafetyTools) {
    $Safety = $ToolSafety.$ToolName
    $MissingSafetyKeys = @($RequiredSafetyKeys | Where-Object { $Safety.PSObject.Properties.Name -notcontains $_ })
    if ($MissingSafetyKeys.Count -gt 0) {
        throw "TOOL_SAFETY.$ToolName missing key(s): $($MissingSafetyKeys -join ', ')"
    }
    if ($Safety.category -notin $AllowedCategories) {
        throw "TOOL_SAFETY.$ToolName has unknown category '$($Safety.category)'"
    }
    if ($null -ne $Safety.wire_action -and -not ($Safety.wire_action -is [string])) {
        throw "TOOL_SAFETY.$ToolName wire_action must be string or null"
    }
    foreach ($Key in @("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint", "requires_cad_preflight")) {
        if (-not ($Safety.$Key -is [bool])) {
            throw "TOOL_SAFETY.$ToolName.$Key must be boolean"
        }
    }
    if ($Safety.readOnlyHint -and $Safety.destructiveHint) {
        throw "TOOL_SAFETY.$ToolName cannot be both readOnly and destructive"
    }
    if ($Safety.PSObject.Properties.Name -contains "actions") {
        if ($Safety.action_param -ne "action") {
            throw "TOOL_SAFETY.$ToolName action_param must be 'action'"
        }
        foreach ($ActionProperty in $Safety.actions.PSObject.Properties) {
            $Variant = $ActionProperty.Value
            $MissingVariantKeys = @($RequiredVariantKeys | Where-Object { $Variant.PSObject.Properties.Name -notcontains $_ })
            if ($MissingVariantKeys.Count -gt 0) {
                throw "TOOL_SAFETY.$ToolName.$($ActionProperty.Name) missing key(s): $($MissingVariantKeys -join ', ')"
            }
            foreach ($Key in @("readOnlyHint", "destructiveHint", "idempotentHint", "writesDocument", "writesFiles", "confirmationRequired")) {
                if (-not ($Variant.$Key -is [bool])) {
                    throw "TOOL_SAFETY.$ToolName.$($ActionProperty.Name).$Key must be boolean"
                }
            }
            if (($Variant.PSObject.Properties.Name -contains "writesSelection") -and -not ($Variant.writesSelection -is [bool])) {
                throw "TOOL_SAFETY.$ToolName.$($ActionProperty.Name).writesSelection must be boolean when present"
            }
            if ($Variant.readOnlyHint -and $Variant.destructiveHint) {
                throw "TOOL_SAFETY.$ToolName.$($ActionProperty.Name) cannot be both readOnly and destructive"
            }
        }
    }
}

$SafetyDocRows = @{}
$SafetyPattern = '^\|\s*`(vw_[^`]+)`\s*\|\s*`([^`]*)`\s*\|\s*`([^`]*)`\s*\|\s*`(true|false)`\s*\|\s*`(true|false)`\s*\|\s*`(true|false)`\s*\|\s*`(true|false)`\s*\|\s*`(true|false)`\s*\|'
foreach ($Line in ($ToolMapText -split "`r?`n")) {
    $Match = [regex]::Match($Line, $SafetyPattern)
    if ($Match.Success) {
        $SafetyDocRows[$Match.Groups[1].Value] = [pscustomobject]@{
            category = $Match.Groups[2].Value
            wire_action = $Match.Groups[3].Value
            readOnlyHint = [bool]::Parse($Match.Groups[4].Value)
            destructiveHint = [bool]::Parse($Match.Groups[5].Value)
            idempotentHint = [bool]::Parse($Match.Groups[6].Value)
            openWorldHint = [bool]::Parse($Match.Groups[7].Value)
            requires_cad_preflight = [bool]::Parse($Match.Groups[8].Value)
        }
    }
}
foreach ($ToolName in $SafetyTools) {
    if (-not $SafetyDocRows.ContainsKey($ToolName)) {
        throw "Tool map safety table missing $ToolName"
    }
    $Safety = $ToolSafety.$ToolName
    $Doc = $SafetyDocRows[$ToolName]
    $WireAction = if ($null -eq $Safety.wire_action) { "" } else { [string]$Safety.wire_action }
    foreach ($Key in @("category", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint", "requires_cad_preflight")) {
        if ($Doc.$Key -ne $Safety.$Key) {
            throw "Tool map safety table drift for $ToolName.$Key. Doc=$($Doc.$Key), server=$($Safety.$Key)"
        }
    }
    if ($Doc.wire_action -ne $WireAction) {
        throw "Tool map safety table drift for $ToolName.wire_action. Doc=$($Doc.wire_action), server=$WireAction"
    }
}

$VariantDocRows = @{}
$VariantPattern = '^\|\s*`(vw_[^`.]+\.[^`]+)`\s*\|\s*`(true|false)`\s*\|\s*`(true|false)`\s*\|\s*`(true|false)`\s*\|\s*`(true|false)`\s*\|\s*`(true|false)`\s*\|\s*`(true|false)`\s*\|\s*`(true|false)`\s*\|'
foreach ($Line in ($ToolMapText -split "`r?`n")) {
    $Match = [regex]::Match($Line, $VariantPattern)
    if ($Match.Success) {
        $VariantDocRows[$Match.Groups[1].Value] = [pscustomobject]@{
            readOnlyHint = [bool]::Parse($Match.Groups[2].Value)
            destructiveHint = [bool]::Parse($Match.Groups[3].Value)
            idempotentHint = [bool]::Parse($Match.Groups[4].Value)
            writesDocument = [bool]::Parse($Match.Groups[5].Value)
            writesSelection = [bool]::Parse($Match.Groups[6].Value)
            writesFiles = [bool]::Parse($Match.Groups[7].Value)
            confirmationRequired = [bool]::Parse($Match.Groups[8].Value)
        }
    }
}
foreach ($ToolName in $SafetyTools) {
    $Safety = $ToolSafety.$ToolName
    if ($Safety.PSObject.Properties.Name -notcontains "actions") { continue }
    foreach ($ActionProperty in $Safety.actions.PSObject.Properties) {
        $DocKey = "$ToolName.$($ActionProperty.Name)"
        if (-not $VariantDocRows.ContainsKey($DocKey)) {
            throw "Tool map mixed-action safety table missing $DocKey"
        }
        $Variant = $ActionProperty.Value
        $Doc = $VariantDocRows[$DocKey]
        foreach ($Key in @("readOnlyHint", "destructiveHint", "idempotentHint", "writesDocument", "writesFiles", "confirmationRequired")) {
            if ($Doc.$Key -ne $Variant.$Key) {
                throw "Tool map mixed-action table drift for $DocKey.$Key. Doc=$($Doc.$Key), server=$($Variant.$Key)"
            }
        }
        $WritesSelection = if ($Variant.PSObject.Properties.Name -contains "writesSelection") { [bool]$Variant.writesSelection } else { $false }
        if ($Doc.writesSelection -ne $WritesSelection) {
            throw "Tool map mixed-action table drift for $DocKey.writesSelection. Doc=$($Doc.writesSelection), server=$WritesSelection"
        }
    }
}

if (-not (Test-Path -LiteralPath $ConnectorMcpPath)) {
    throw "Companion repo is missing .mcp.json"
}
$ConnectorMcp = Get-Content -Raw -LiteralPath $ConnectorMcpPath | ConvertFrom-Json
$PluginMcp = Get-Content -Raw -LiteralPath $PluginMcpPath | ConvertFrom-Json
$ConnectorEnv = $ConnectorMcp.mcpServers.vectorworks.env
$PluginEnv = $PluginMcp.mcpServers.vectorworks.env
foreach ($Key in @("VW_MCP_HOST", "VW_MCP_PORT", "VW_MCP_TIMEOUT", "VW_MCP_PREFLIGHT_CACHE_MS")) {
    if ($ConnectorEnv.$Key -ne $PluginEnv.$Key) {
        throw "MCP env default drift for $Key. Connector=$($ConnectorEnv.$Key), plugin=$($PluginEnv.$Key)"
    }
}

$WrapperParamContracts = @{
    "scripts\test-vectorworks-listener.ps1" = "scripts\test-vectorworks-listener.ps1"
    "scripts\doctor-vectorworks-mcp.ps1" = "scripts\doctor-vectorworks-mcp.ps1"
    "scripts\bootstrap-native-bridge.ps1" = "scripts\bootstrap-native-bridge.ps1"
    "scripts\prepare-native-bridge-source.ps1" = "scripts\prepare-native-bridge-source.ps1"
    "scripts\build-native-bridge.ps1" = "scripts\build-native-bridge.ps1"
    "scripts\smoke-native-bridge.ps1" = "scripts\smoke-native-bridge.ps1"
}
foreach ($RelativeWrapper in $WrapperParamContracts.Keys) {
    $WrapperPath = Join-Path $PluginRoot $RelativeWrapper
    $CompanionPath = Join-Path $RepoRoot $WrapperParamContracts[$RelativeWrapper]
    if (-not (Test-Path -LiteralPath $WrapperPath)) {
        throw "Plugin wrapper missing: $RelativeWrapper"
    }
    $WrapperParams = @(Get-ScriptParameterNames -Path $WrapperPath)
    $CompanionParams = @(Get-ScriptParameterNames -Path $CompanionPath)
    $MissingWrapperParams = @($CompanionParams | Where-Object { $_ -notin $WrapperParams })
    if ($MissingWrapperParams.Count -gt 0) {
        throw "Plugin wrapper $RelativeWrapper does not expose companion parameter(s): $($MissingWrapperParams -join ', ')"
    }
}

Write-Host "OK: companion contract matches $RepoRoot"
