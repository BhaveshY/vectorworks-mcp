[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [string]$VectorworksVersion = "2024",
    [string]$WorktreeRoot = "",
    [string]$ProjectPath = "",
    [string]$SourceDir = "",
    [string]$FiltersPath = "",
    [switch]$CheckOnly,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $WorktreeRoot) {
    $WorktreeRoot = Join-Path $RepoRoot "native_bridge\worktree\SDKExamples"
}
$BridgeProjectRoot = Join-Path $WorktreeRoot "Examples$VectorworksVersion\VectorworksMCPBridge"
if (-not $SourceDir) {
    $SourceDir = Join-Path $BridgeProjectRoot "Source\VectorworksMCPBridge"
}

$CompileFiles = @(
    "BridgeProtocol.cpp",
    "VectorworksMCPBridge.cpp"
)
$HeaderFiles = @(
    "BridgeProtocol.hpp",
    "BridgeDispatcher.hpp",
    "CadRequestQueue.hpp"
)

function Get-FirstProjectFile {
    param([string]$Root)
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return ""
    }
    $Match = Get-ChildItem -LiteralPath $Root -Recurse -File -Filter "*.vcxproj" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\(Source|include|SDKLib|ThirdPartySource)\\' } |
        Sort-Object FullName |
        Select-Object -First 1
    if ($Match) { return $Match.FullName }
    return ""
}

function Get-RelativePathForProject {
    param(
        [string]$BaseDirectory,
        [string]$TargetPath
    )
    $BaseFull = [System.IO.Path]::GetFullPath($BaseDirectory)
    $TargetFull = [System.IO.Path]::GetFullPath($TargetPath)
    if (-not $BaseFull.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
        $BaseFull += [System.IO.Path]::DirectorySeparatorChar
    }
    $BaseUri = [System.Uri]$BaseFull
    $TargetUri = [System.Uri]$TargetFull
    return [System.Uri]::UnescapeDataString($BaseUri.MakeRelativeUri($TargetUri).ToString()).Replace("/", "\")
}

function Normalize-ProjectInclude {
    param([string]$Value)
    return ([string]$Value).Replace("/", "\").Trim()
}

function New-MsbuildElement {
    param(
        [xml]$Document,
        [string]$Name
    )
    $Namespace = $Document.DocumentElement.NamespaceURI
    if ($Namespace) {
        return $Document.CreateElement($Name, $Namespace)
    }
    return $Document.CreateElement($Name)
}

function Get-MsbuildNodes {
    param(
        [xml]$Document,
        [string]$Name
    )
    $Namespace = $Document.DocumentElement.NamespaceURI
    if ($Namespace) {
        $Manager = [System.Xml.XmlNamespaceManager]::new($Document.NameTable)
        $Manager.AddNamespace("msb", $Namespace)
        return $Document.SelectNodes("//msb:$Name", $Manager)
    }
    return $Document.SelectNodes("//$Name")
}

function Add-ProjectItem {
    param(
        [xml]$Document,
        [System.Xml.XmlElement]$ItemGroup,
        [string]$ItemName,
        [string]$Include
    )
    $Item = New-MsbuildElement -Document $Document -Name $ItemName
    $Item.SetAttribute("Include", $Include)
    [void]$ItemGroup.AppendChild($Item)
}

function Ensure-ProjectItems {
    param(
        [xml]$Document,
        [hashtable]$ExpectedItems
    )
    $Added = [System.Collections.Generic.List[string]]::new()
    $Existing = @{}
    foreach ($ItemName in $ExpectedItems.Keys) {
        foreach ($Node in @(Get-MsbuildNodes -Document $Document -Name $ItemName)) {
            if ($Node.Include) {
                $Existing["$ItemName|$(Normalize-ProjectInclude $Node.Include)"] = $true
            }
        }
    }

    $ItemGroup = New-MsbuildElement -Document $Document -Name "ItemGroup"
    $ItemGroupHasItems = $false
    foreach ($ItemName in ($ExpectedItems.Keys | Sort-Object)) {
        foreach ($Include in $ExpectedItems[$ItemName]) {
            $Key = "$ItemName|$(Normalize-ProjectInclude $Include)"
            if (-not $Existing.ContainsKey($Key)) {
                Add-ProjectItem -Document $Document -ItemGroup $ItemGroup -ItemName $ItemName -Include $Include
                $Added.Add($Key)
                $ItemGroupHasItems = $true
            }
        }
    }
    if ($ItemGroupHasItems) {
        [void]$Document.DocumentElement.AppendChild($ItemGroup)
    }
    return @($Added)
}

function New-FiltersDocument {
    $Document = [xml]'<?xml version="1.0" encoding="utf-8"?><Project ToolsVersion="4.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003"></Project>'
    return $Document
}

function Ensure-FilterDefinitions {
    param([xml]$Document)
    $Existing = @{}
    foreach ($Node in @(Get-MsbuildNodes -Document $Document -Name "Filter")) {
        if ($Node.Include) {
            $Existing[[string]$Node.Include] = $true
        }
    }
    $ItemGroup = New-MsbuildElement -Document $Document -Name "ItemGroup"
    $Changed = $false
    foreach ($FilterName in @("Source Files", "Header Files")) {
        if (-not $Existing.ContainsKey($FilterName)) {
            $Filter = New-MsbuildElement -Document $Document -Name "Filter"
            $Filter.SetAttribute("Include", $FilterName)
            [void]$ItemGroup.AppendChild($Filter)
            $Changed = $true
        }
    }
    if ($Changed) {
        [void]$Document.DocumentElement.AppendChild($ItemGroup)
    }
    return $Changed
}

function Add-FilterItem {
    param(
        [xml]$Document,
        [System.Xml.XmlElement]$ItemGroup,
        [string]$ItemName,
        [string]$Include,
        [string]$FilterName
    )
    $Item = New-MsbuildElement -Document $Document -Name $ItemName
    $Item.SetAttribute("Include", $Include)
    $Filter = New-MsbuildElement -Document $Document -Name "Filter"
    $Filter.InnerText = $FilterName
    [void]$Item.AppendChild($Filter)
    [void]$ItemGroup.AppendChild($Item)
}

function Ensure-FilterItems {
    param(
        [xml]$Document,
        [hashtable]$ExpectedItems
    )
    $Added = [System.Collections.Generic.List[string]]::new()
    $Existing = @{}
    foreach ($ItemName in $ExpectedItems.Keys) {
        foreach ($Node in @(Get-MsbuildNodes -Document $Document -Name $ItemName)) {
            if ($Node.Include) {
                $Existing["$ItemName|$(Normalize-ProjectInclude $Node.Include)"] = $true
            }
        }
    }

    $ItemGroup = New-MsbuildElement -Document $Document -Name "ItemGroup"
    $ItemGroupHasItems = $false
    foreach ($ItemName in ($ExpectedItems.Keys | Sort-Object)) {
        $FilterName = if ($ItemName -eq "ClCompile") { "Source Files" } else { "Header Files" }
        foreach ($Include in $ExpectedItems[$ItemName]) {
            $Key = "$ItemName|$(Normalize-ProjectInclude $Include)"
            if (-not $Existing.ContainsKey($Key)) {
                Add-FilterItem -Document $Document -ItemGroup $ItemGroup -ItemName $ItemName -Include $Include -FilterName $FilterName
                $Added.Add($Key)
                $ItemGroupHasItems = $true
            }
        }
    }
    if ($ItemGroupHasItems) {
        [void]$Document.DocumentElement.AppendChild($ItemGroup)
    }
    return @($Added)
}

if (-not $ProjectPath) {
    $ProjectPath = Get-FirstProjectFile -Root $BridgeProjectRoot
}
if (-not $ProjectPath -or -not (Test-Path -LiteralPath $ProjectPath -PathType Leaf)) {
    throw "No SDK Visual C++ project (*.vcxproj) was found. Run scripts\prepare-native-bridge-source.ps1 first or pass -ProjectPath."
}
$ProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$ProjectDirectory = Split-Path -Parent $ProjectPath

if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
    throw "Native bridge scaffold source folder was not found at $SourceDir. Run scripts\copy-native-bridge-scaffold.ps1 first."
}
$SourceDir = (Resolve-Path -LiteralPath $SourceDir).Path

$ExpectedItems = @{
    ClCompile = @()
    ClInclude = @()
}
foreach ($FileName in $CompileFiles) {
    $FullPath = Join-Path $SourceDir $FileName
    if (-not (Test-Path -LiteralPath $FullPath -PathType Leaf)) {
        throw "Native bridge compile source is missing: $FullPath"
    }
    $ExpectedItems.ClCompile += (Get-RelativePathForProject -BaseDirectory $ProjectDirectory -TargetPath $FullPath)
}
foreach ($FileName in $HeaderFiles) {
    $FullPath = Join-Path $SourceDir $FileName
    if (-not (Test-Path -LiteralPath $FullPath -PathType Leaf)) {
        throw "Native bridge header source is missing: $FullPath"
    }
    $ExpectedItems.ClInclude += (Get-RelativePathForProject -BaseDirectory $ProjectDirectory -TargetPath $FullPath)
}

$ProjectDocument = [xml](Get-Content -Raw -LiteralPath $ProjectPath)
$MissingBefore = [System.Collections.Generic.List[string]]::new()
foreach ($ItemName in ($ExpectedItems.Keys | Sort-Object)) {
    $ExistingIncludes = @(Get-MsbuildNodes -Document $ProjectDocument -Name $ItemName | ForEach-Object { Normalize-ProjectInclude $_.Include })
    foreach ($Include in $ExpectedItems[$ItemName]) {
        if ((Normalize-ProjectInclude $Include) -notin $ExistingIncludes) {
            $MissingBefore.Add("$ItemName|$(Normalize-ProjectInclude $Include)")
        }
    }
}

$AddedProjectItems = @()
$ProjectChanged = $false
if (-not $CheckOnly) {
    $AddedProjectItems = @(Ensure-ProjectItems -Document $ProjectDocument -ExpectedItems $ExpectedItems)
    $ProjectChanged = @($AddedProjectItems).Count -gt 0
    if ($ProjectChanged -and $PSCmdlet.ShouldProcess($ProjectPath, "Wire native bridge source files into SDK project")) {
        $ProjectDocument.Save($ProjectPath)
    }
}

if (-not $FiltersPath) {
    $CandidateFiltersPath = "$ProjectPath.filters"
    $FiltersPath = $CandidateFiltersPath
}

$AddedFilterItems = @()
$FiltersChanged = $false
if (-not $CheckOnly) {
    if (Test-Path -LiteralPath $FiltersPath -PathType Leaf) {
        $FiltersDocument = [xml](Get-Content -Raw -LiteralPath $FiltersPath)
    } else {
        $FiltersDocument = New-FiltersDocument
    }
    $FilterDefinitionsChanged = Ensure-FilterDefinitions -Document $FiltersDocument
    $AddedFilterItems = @(Ensure-FilterItems -Document $FiltersDocument -ExpectedItems $ExpectedItems)
    $FiltersChanged = $FilterDefinitionsChanged -or (@($AddedFilterItems).Count -gt 0)
    if ($FiltersChanged -and $PSCmdlet.ShouldProcess($FiltersPath, "Wire native bridge source files into SDK project filters")) {
        $FiltersDocument.Save($FiltersPath)
    }
}

$Report = [pscustomobject]@{
    vectorworksVersion = $VectorworksVersion
    worktreeRoot = $WorktreeRoot
    projectPath = $ProjectPath
    filtersPath = $FiltersPath
    sourceDir = $SourceDir
    expectedProjectItems = [pscustomobject]@{
        ClCompile = @($ExpectedItems.ClCompile)
        ClInclude = @($ExpectedItems.ClInclude)
    }
    missingProjectItems = @($MissingBefore)
    projectWired = @($MissingBefore).Count -eq 0
    checkOnly = [bool]$CheckOnly
    projectChanged = [bool]$ProjectChanged
    filtersChanged = [bool]$FiltersChanged
    addedProjectItems = @($AddedProjectItems)
    addedFilterItems = @($AddedFilterItems)
}

if ($Json) {
    $Report | ConvertTo-Json -Depth 8
} else {
    Write-Host "Vectorworks native bridge project wiring"
    Write-Host "Project: $ProjectPath"
    Write-Host "Source dir: $SourceDir"
    Write-Host "Project wired before this run: $($Report.projectWired)"
    if (@($MissingBefore).Count -gt 0) {
        Write-Host "Missing project items:"
        foreach ($Missing in $MissingBefore) {
            Write-Host "- $Missing"
        }
    }
    if (-not $CheckOnly) {
        Write-Host "Added project items: $(@($AddedProjectItems).Count)"
        Write-Host "Added filter items: $(@($AddedFilterItems).Count)"
    }
    Write-Host ""
    Write-Host "Next: build with scripts\build-native-bridge.ps1, then install with scripts\doctor-native-bridge.ps1 after a successful artifact build."
}
