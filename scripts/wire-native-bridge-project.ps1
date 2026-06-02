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
    "NativeTransport.cpp",
    "VectorworksMCPBridge.cpp"
)
$HeaderFiles = @(
    "BridgeProtocol.hpp",
    "BridgeDispatcher.hpp",
    "CadRequestQueue.hpp",
    "NativeTransport.hpp"
)
$RequiredLinkDependencies = @(
    "Ws2_32.lib"
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

function Get-MsbuildChildElement {
    param(
        [System.Xml.XmlElement]$Parent,
        [string]$Name
    )
    foreach ($Child in @($Parent.ChildNodes)) {
        if ($Child -is [System.Xml.XmlElement] -and $Child.LocalName -eq $Name) {
            return $Child
        }
    }
    return $null
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

function Get-ProjectLinkDependencyEntries {
    param(
        [xml]$Document,
        [string[]]$Dependencies
    )
    $Missing = [System.Collections.Generic.List[string]]::new()
    $LinkNodes = @(Get-MsbuildNodes -Document $Document -Name "Link")
    if ($LinkNodes.Count -eq 0) {
        foreach ($Dependency in $Dependencies) {
            $Missing.Add("Link|$Dependency")
        }
        return @($Missing)
    }

    $Index = 0
    foreach ($Link in $LinkNodes) {
        $Index += 1
        $Additional = Get-MsbuildChildElement -Parent $Link -Name "AdditionalDependencies"
        $Values = @()
        if ($Additional) {
            $Values = @(([string]$Additional.InnerText).Split(";") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        }
        foreach ($Dependency in $Dependencies) {
            if ($Dependency -notin $Values) {
                $Missing.Add("Link[$Index]|$Dependency")
            }
        }
    }
    return @($Missing)
}

function Set-LinkDependencyText {
    param(
        [System.Xml.XmlElement]$AdditionalDependencies,
        [string[]]$Dependencies
    )
    $Values = @(([string]$AdditionalDependencies.InnerText).Split(";") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($Values.Count -eq 0) {
        $Values = @("%(AdditionalDependencies)")
    }
    foreach ($Dependency in $Dependencies) {
        if ($Dependency -notin $Values) {
            $MacroIndex = [Array]::IndexOf($Values, "%(AdditionalDependencies)")
            if ($MacroIndex -ge 0) {
                $Before = if ($MacroIndex -gt 0) { @($Values[0..($MacroIndex - 1)]) } else { @() }
                $After = @($Values[$MacroIndex..($Values.Count - 1)])
                $Values = @($Before + $Dependency + $After)
            } else {
                $Values = @($Values + $Dependency)
            }
        }
    }
    $AdditionalDependencies.InnerText = ($Values -join ";")
}

function Ensure-ProjectLinkDependencies {
    param(
        [xml]$Document,
        [string[]]$Dependencies
    )
    $Added = [System.Collections.Generic.List[string]]::new()
    $LinkNodes = @(Get-MsbuildNodes -Document $Document -Name "Link")
    if ($LinkNodes.Count -eq 0) {
        $ItemDefinitionGroup = New-MsbuildElement -Document $Document -Name "ItemDefinitionGroup"
        $Link = New-MsbuildElement -Document $Document -Name "Link"
        $Additional = New-MsbuildElement -Document $Document -Name "AdditionalDependencies"
        $Additional.InnerText = (($Dependencies + "%(AdditionalDependencies)") -join ";")
        [void]$Link.AppendChild($Additional)
        [void]$ItemDefinitionGroup.AppendChild($Link)
        [void]$Document.DocumentElement.AppendChild($ItemDefinitionGroup)
        foreach ($Dependency in $Dependencies) {
            $Added.Add("Link|$Dependency")
        }
        return @($Added)
    }

    $Index = 0
    foreach ($Link in $LinkNodes) {
        $Index += 1
        $Additional = Get-MsbuildChildElement -Parent $Link -Name "AdditionalDependencies"
        if (-not $Additional) {
            $Additional = New-MsbuildElement -Document $Document -Name "AdditionalDependencies"
            [void]$Link.AppendChild($Additional)
        }
        $Before = @(([string]$Additional.InnerText).Split(";") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        Set-LinkDependencyText -AdditionalDependencies $Additional -Dependencies $Dependencies
        $After = @(([string]$Additional.InnerText).Split(";") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        foreach ($Dependency in $Dependencies) {
            if ($Dependency -notin $Before -and $Dependency -in $After) {
                $Added.Add("Link[$Index]|$Dependency")
            }
        }
    }
    return @($Added)
}

function Get-MissingLanguageStandardEntries {
    param(
        [xml]$Document,
        [string]$Standard
    )
    $Missing = [System.Collections.Generic.List[string]]::new()
    $CompileNodes = @(Get-MsbuildNodes -Document $Document -Name "ClCompile" | Where-Object { $_.ParentNode.LocalName -eq "ItemDefinitionGroup" })
    if ($CompileNodes.Count -eq 0) {
        $Missing.Add("ClCompile|LanguageStandard=$Standard")
        return @($Missing)
    }

    $Index = 0
    foreach ($CompileNode in $CompileNodes) {
        $Index += 1
        $LanguageStandard = Get-MsbuildChildElement -Parent $CompileNode -Name "LanguageStandard"
        if (-not $LanguageStandard -or [string]$LanguageStandard.InnerText -ne $Standard) {
            $Missing.Add("ClCompile[$Index]|LanguageStandard=$Standard")
        }
    }
    return @($Missing)
}

function Ensure-LanguageStandard {
    param(
        [xml]$Document,
        [string]$Standard
    )
    $Added = [System.Collections.Generic.List[string]]::new()
    $CompileNodes = @(Get-MsbuildNodes -Document $Document -Name "ClCompile" | Where-Object { $_.ParentNode.LocalName -eq "ItemDefinitionGroup" })
    if ($CompileNodes.Count -eq 0) {
        $ItemDefinitionGroup = New-MsbuildElement -Document $Document -Name "ItemDefinitionGroup"
        $CompileNode = New-MsbuildElement -Document $Document -Name "ClCompile"
        $LanguageStandard = New-MsbuildElement -Document $Document -Name "LanguageStandard"
        $LanguageStandard.InnerText = $Standard
        [void]$CompileNode.AppendChild($LanguageStandard)
        [void]$ItemDefinitionGroup.AppendChild($CompileNode)
        [void]$Document.DocumentElement.AppendChild($ItemDefinitionGroup)
        $Added.Add("ClCompile|LanguageStandard=$Standard")
        return @($Added)
    }

    $Index = 0
    foreach ($CompileNode in $CompileNodes) {
        $Index += 1
        $LanguageStandard = Get-MsbuildChildElement -Parent $CompileNode -Name "LanguageStandard"
        if (-not $LanguageStandard) {
            $LanguageStandard = New-MsbuildElement -Document $Document -Name "LanguageStandard"
            [void]$CompileNode.AppendChild($LanguageStandard)
        }
        if ([string]$LanguageStandard.InnerText -ne $Standard) {
            $LanguageStandard.InnerText = $Standard
            $Added.Add("ClCompile[$Index]|LanguageStandard=$Standard")
        }
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
$MissingLinkDependenciesBefore = @(Get-ProjectLinkDependencyEntries -Document $ProjectDocument -Dependencies $RequiredLinkDependencies)
$MissingLanguageStandardBefore = @(Get-MissingLanguageStandardEntries -Document $ProjectDocument -Standard "stdcpp17")

$AddedProjectItems = @()
$AddedLinkDependencies = @()
$AddedLanguageStandards = @()
$ProjectChanged = $false
if (-not $CheckOnly) {
    $AddedProjectItems = @(Ensure-ProjectItems -Document $ProjectDocument -ExpectedItems $ExpectedItems)
    $AddedLinkDependencies = @(Ensure-ProjectLinkDependencies -Document $ProjectDocument -Dependencies $RequiredLinkDependencies)
    $AddedLanguageStandards = @(Ensure-LanguageStandard -Document $ProjectDocument -Standard "stdcpp17")
    $ProjectChanged = (@($AddedProjectItems).Count -gt 0) -or (@($AddedLinkDependencies).Count -gt 0) -or (@($AddedLanguageStandards).Count -gt 0)
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
    requiredLinkDependencies = @($RequiredLinkDependencies)
    missingProjectItems = @($MissingBefore)
    missingLinkDependencies = @($MissingLinkDependenciesBefore)
    missingLanguageStandards = @($MissingLanguageStandardBefore)
    linkDependenciesWired = @($MissingLinkDependenciesBefore).Count -eq 0
    languageStandardWired = @($MissingLanguageStandardBefore).Count -eq 0
    projectWired = (@($MissingBefore).Count -eq 0) -and (@($MissingLinkDependenciesBefore).Count -eq 0) -and (@($MissingLanguageStandardBefore).Count -eq 0)
    checkOnly = [bool]$CheckOnly
    projectChanged = [bool]$ProjectChanged
    filtersChanged = [bool]$FiltersChanged
    addedProjectItems = @($AddedProjectItems)
    addedLinkDependencies = @($AddedLinkDependencies)
    addedLanguageStandards = @($AddedLanguageStandards)
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
    if (@($MissingLinkDependenciesBefore).Count -gt 0) {
        Write-Host "Missing linker dependencies:"
        foreach ($Missing in $MissingLinkDependenciesBefore) {
            Write-Host "- $Missing"
        }
    }
    if (@($MissingLanguageStandardBefore).Count -gt 0) {
        Write-Host "Missing C++ language standard settings:"
        foreach ($Missing in $MissingLanguageStandardBefore) {
            Write-Host "- $Missing"
        }
    }
    if (-not $CheckOnly) {
        Write-Host "Added project items: $(@($AddedProjectItems).Count)"
        Write-Host "Added linker dependencies: $(@($AddedLinkDependencies).Count)"
        Write-Host "Added C++ language standard settings: $(@($AddedLanguageStandards).Count)"
        Write-Host "Added filter items: $(@($AddedFilterItems).Count)"
    }
    Write-Host ""
    Write-Host "Next: build with scripts\build-native-bridge.ps1, then install with scripts\doctor-native-bridge.ps1 after a successful artifact build."
}
