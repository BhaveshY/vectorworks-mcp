[CmdletBinding()]
param(
    [ValidateSet("HostOnly", "ClaudeCode")]
    [string]$Client = "HostOnly",
    [string]$InstallDir = "",
    [switch]$NoVerify,
    [switch]$SkipClipboard,
    [switch]$SkipDependencyInstall,
    [switch]$FullNative,
    [string]$VectorworksVersion = "2024",
    [ValidateSet("Debug", "Release")]
    [string]$NativeConfiguration = "Debug",
    [ValidateRange(1, 20)]
    [int]$NativeMaxSteps = 12,
    [string]$SdkDir = "",
    [string]$SdkArchivePath = "",
    [string]$SdkExamplesDir = "",
    [string]$WorktreeRoot = "",
    [string]$NativeInstallDir = "",
    [switch]$AllowVectorworksSmoke,
    [switch]$SkipVectorworksAutomation,
    [switch]$ForceVectorworksRestart,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/BhaveshY/vectorworks-mcp.git"
$RawInstallUrl = "https://raw.githubusercontent.com/BhaveshY/vectorworks-mcp/main/install.ps1"
$DefaultInstallDir = Join-Path $env:USERPROFILE "repos\vectorworks-mcp"
$DependencyChecks = [System.Collections.Generic.List[object]]::new()
$RepoRoot = ""
$GitExe = ""

function Add-PathIfPresent {
    param([string]$Directory)
    if ($Directory -and (Test-Path -LiteralPath $Directory -PathType Container)) {
        $Parts = @($env:PATH -split ";")
        if ($Directory -notin $Parts) {
            $env:PATH = "$Directory;$env:PATH"
        }
    }
}

function Join-ExistingBasePath {
    param(
        [string]$Base,
        [string]$Child
    )
    if (-not $Base) { return "" }
    return (Join-Path $Base $Child)
}

function Add-CommonInstallPaths {
    Add-PathIfPresent (Join-ExistingBasePath $env:ProgramFiles "Git\cmd")
    Add-PathIfPresent (Join-ExistingBasePath $env:ProgramFiles "Git\bin")
    Add-PathIfPresent (Join-ExistingBasePath ${env:ProgramFiles(x86)} "Git\cmd")
    Add-PathIfPresent $env:WINDIR
    Add-PathIfPresent (Join-ExistingBasePath $env:LOCALAPPDATA "Programs\Python\Launcher")
    Add-PathIfPresent (Join-ExistingBasePath $env:LOCALAPPDATA "Programs\Python\Python312")
    Add-PathIfPresent (Join-ExistingBasePath $env:LOCALAPPDATA "Programs\Python\Python312\Scripts")
}

function Resolve-CommandPath {
    param(
        [string[]]$Names,
        [string[]]$FallbackPaths = @()
    )
    foreach ($Name in $Names) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if ($Command) {
            return $Command.Source
        }
    }
    foreach ($Path in $FallbackPaths) {
        if ($Path -and (Test-Path -LiteralPath $Path -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $Path).Path
        }
    }
    return ""
}

function Invoke-External {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$StepName,
        [switch]$AllowFailure
    )
    $Output = & $FilePath @ArgumentList 2>&1
    $Exit = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
    if (-not $Json -and $Output) {
        $Output | ForEach-Object { Write-Host $_ }
    }
    if ($Exit -ne 0 -and -not $AllowFailure) {
        throw "$StepName failed with exit code $Exit. Output: $($Output -join [Environment]::NewLine)"
    }
    return [pscustomobject]@{
        step = $StepName
        exitCode = $Exit
        output = ($Output -join [Environment]::NewLine)
    }
}

function Install-WingetPackage {
    param(
        [string]$PackageId,
        [string]$Label,
        [string[]]$ExtraArguments = @()
    )
    $Winget = Resolve-CommandPath -Names @("winget.exe", "winget")
    if (-not $Winget) {
        throw "$Label is missing and winget.exe is not available. Install App Installer/winget, then rerun this installer."
    }
    $Args = @(
        "install",
        "--id", $PackageId,
        "--exact",
        "--source", "winget",
        "--accept-package-agreements",
        "--accept-source-agreements"
    ) + @($ExtraArguments)
    Invoke-External -FilePath $Winget -ArgumentList $Args -StepName "Install $Label with winget" | Out-Null
    Add-CommonInstallPaths
}

function Test-PythonReady {
    $Py = Resolve-CommandPath -Names @("py.exe", "py") -FallbackPaths @(
        (Join-ExistingBasePath $env:WINDIR "py.exe"),
        (Join-ExistingBasePath $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe")
    )
    if ($Py) {
        & $Py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { return $true }
    }
    $Python = Resolve-CommandPath -Names @("python.exe", "python") -FallbackPaths @(
        (Join-ExistingBasePath $env:LOCALAPPDATA "Programs\Python\Python312\python.exe")
    )
    if ($Python) {
        & $Python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { return $true }
    }
    return $false
}

function Add-DependencyCheck {
    param(
        [string]$Name,
        [bool]$Ok,
        [bool]$Installed,
        [string]$Detail
    )
    $DependencyChecks.Add([pscustomobject]@{
        name = $Name
        ok = [bool]$Ok
        installed = [bool]$Installed
        detail = $Detail
    }) | Out-Null
}

function Ensure-BaseDependencies {
    Add-CommonInstallPaths

    $GitFallbacks = @(
        (Join-ExistingBasePath $env:ProgramFiles "Git\cmd\git.exe"),
        (Join-ExistingBasePath $env:ProgramFiles "Git\bin\git.exe"),
        (Join-ExistingBasePath ${env:ProgramFiles(x86)} "Git\cmd\git.exe")
    )
    $script:GitExe = Resolve-CommandPath -Names @("git.exe", "git") -FallbackPaths $GitFallbacks
    $GitInstalled = $false
    if (-not $script:GitExe) {
        if ($SkipDependencyInstall) {
            Add-DependencyCheck -Name "Git" -Ok $false -Installed $false -Detail "missing; rerun without -SkipDependencyInstall or install Git.Git with winget"
            throw "Git is required to clone or update vectorworks-mcp."
        }
        Install-WingetPackage -PackageId "Git.Git" -Label "Git"
        $GitInstalled = $true
        $script:GitExe = Resolve-CommandPath -Names @("git.exe", "git") -FallbackPaths $GitFallbacks
    }
    if (-not $script:GitExe) {
        throw "Git was installed or requested, but git.exe is still not discoverable in this PowerShell session."
    }
    Add-DependencyCheck -Name "Git" -Ok $true -Installed $GitInstalled -Detail $script:GitExe

    $PythonInstalled = $false
    if (-not (Test-PythonReady)) {
        if ($SkipDependencyInstall) {
            Add-DependencyCheck -Name "Python 3.10+" -Ok $false -Installed $false -Detail "missing; rerun without -SkipDependencyInstall or install Python.Python.3.12 with winget"
            throw "Python 3.10 or newer is required for the MCP server."
        }
        Install-WingetPackage -PackageId "Python.Python.3.12" -Label "Python 3.12"
        $PythonInstalled = $true
    }
    if (-not (Test-PythonReady)) {
        throw "Python was installed or requested, but Python 3.10+ is still not discoverable in this PowerShell session."
    }
    Add-DependencyCheck -Name "Python 3.10+" -Ok $true -Installed $PythonInstalled -Detail "Python launcher/interpreter is available"
}

function Test-ConnectorRepoRoot {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Container)) {
        return $false
    }
    $Root = (Resolve-Path -LiteralPath $Path).Path
    return (
        (Test-Path -LiteralPath (Join-Path $Root "server.py") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Root ".mcp.json") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Root "scripts\bootstrap-agent.ps1") -PathType Leaf)
    )
}

function Invoke-Git {
    param([string[]]$ArgumentList)
    if (-not $script:GitExe) {
        $script:GitExe = Resolve-CommandPath -Names @("git.exe", "git")
    }
    if (-not $script:GitExe) {
        throw "Git was not found on PATH. Rerun without -SkipDependencyInstall or install Git first with: winget install --id Git.Git --exact --source winget"
    }
    Invoke-External -FilePath $script:GitExe -ArgumentList $ArgumentList -StepName "git $($ArgumentList -join ' ')" | Out-Null
}

function Resolve-ConnectorRepoRoot {
    if ($PSScriptRoot -and (Test-ConnectorRepoRoot $PSScriptRoot)) {
        return (Resolve-Path -LiteralPath $PSScriptRoot).Path
    }

    $CurrentDir = (Get-Location).Path
    if (Test-ConnectorRepoRoot $CurrentDir) {
        return (Resolve-Path -LiteralPath $CurrentDir).Path
    }

    $Target = $InstallDir
    if (-not $Target -and $env:VW_MCP_REPO) {
        $Target = $env:VW_MCP_REPO
    }
    if (-not $Target) {
        $Target = $DefaultInstallDir
    }
    $Target = [System.IO.Path]::GetFullPath($Target)

    if (Test-ConnectorRepoRoot $Target) {
        if (Test-Path -LiteralPath (Join-Path $Target ".git") -PathType Container) {
            Invoke-Git @("-C", $Target, "pull", "--ff-only")
        }
        return (Resolve-Path -LiteralPath $Target).Path
    }

    if (Test-Path -LiteralPath $Target) {
        $HasChildren = @(Get-ChildItem -LiteralPath $Target -Force -ErrorAction SilentlyContinue | Select-Object -First 1).Count -gt 0
        if ($HasChildren) {
            throw "InstallDir exists but is not a vectorworks-mcp checkout: $Target"
        }
    } else {
        $Parent = Split-Path -Parent $Target
        if ($Parent) {
            New-Item -ItemType Directory -Force -Path $Parent | Out-Null
        }
    }

    Invoke-Git @("clone", $RepoUrl, $Target)
    if (-not (Test-ConnectorRepoRoot $Target)) {
        throw "Cloned repo did not contain the expected Vectorworks MCP files: $Target"
    }
    return (Resolve-Path -LiteralPath $Target).Path
}

function Invoke-NativeRunnerJson {
    param(
        [string]$RepoRoot,
        [string[]]$Arguments
    )
    $RunnerPath = Join-Path $RepoRoot "scripts\invoke-native-bridge-next.ps1"
    if (-not (Test-Path -LiteralPath $RunnerPath -PathType Leaf)) {
        throw "Native runner was not found at $RunnerPath"
    }
    $Output = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $RunnerPath @Arguments 2>&1 | Out-String
    $Exit = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
    try {
        $Payload = $Output | ConvertFrom-Json
    } catch {
        throw "Native runner did not emit valid JSON. Exit=$Exit Output=$Output"
    }
    return [pscustomobject]@{
        exitCode = $Exit
        payload = $Payload
        raw = $Output
    }
}

function Get-NativeDoctorJson {
    param([string]$RepoRoot)
    $DoctorPath = Join-Path $RepoRoot "scripts\doctor-native-bridge.ps1"
    $Args = @("-VectorworksVersion", $VectorworksVersion, "-Configuration", $NativeConfiguration, "-Json")
    if ($SdkDir) { $Args += @("-SdkDir", $SdkDir) }
    if ($SdkArchivePath) { $Args += @("-SdkArchivePath", $SdkArchivePath) }
    if ($SdkExamplesDir) { $Args += @("-SdkExamplesDir", $SdkExamplesDir) }
    if ($WorktreeRoot) { $Args += @("-WorktreeRoot", $WorktreeRoot) }
    if ($NativeInstallDir) { $Args += @("-InstallDir", $NativeInstallDir) }
    $Output = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $DoctorPath @Args 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "Native doctor failed with exit code $LASTEXITCODE. Output: $Output"
    }
    return $Output | ConvertFrom-Json
}

function Get-LastNativeStep {
    param([object]$NativeRunnerResult)
    if ($NativeRunnerResult -and $NativeRunnerResult.steps) {
        return @($NativeRunnerResult.steps)[-1]
    }
    return $null
}

function Test-NativeStepSucceeded {
    param(
        [object]$NativeRunnerResult,
        [string]$Stage
    )
    $LastStep = Get-LastNativeStep $NativeRunnerResult
    return [bool](
        $LastStep -and
        [string]$LastStep.stage -eq $Stage -and
        [bool]$LastStep.executed -and
        [int]$LastStep.exitCode -eq 0
    )
}

function Test-VectorworksInteractionBoundary {
    param(
        [object]$NativeRunnerResult,
        [object]$Doctor
    )
    $LastStep = Get-LastNativeStep $NativeRunnerResult
    $MissingFlags = @($NativeRunnerResult.missingAllowFlags | ForEach-Object { [string]$_ })
    return (
        ($LastStep -and [string]$LastStep.stage -eq "smoke-phase-0") -or
        ([string]$Doctor.nextCommandSpec.stage -eq "smoke-phase-0") -or
        ($MissingFlags.Count -eq 1 -and $MissingFlags[0] -eq "-AllowVectorworksRestartStep")
    )
}

function New-NativeSummary {
    param(
        [object]$NativeRunnerResult,
        [int]$NativeExitCode,
        [object]$Doctor,
        [object[]]$Runs,
        [object]$Phase2SmokeResult = $null
    )
    $LastStep = Get-LastNativeStep $NativeRunnerResult
    $InteractionBoundary = Test-VectorworksInteractionBoundary -NativeRunnerResult $NativeRunnerResult -Doctor $Doctor
    $VectorworksAutomationAttempted = [bool]($LastStep -and [string]$LastStep.stage -eq "smoke-phase-0" -and [bool]$LastStep.executed)
    $Phase0SmokeTested = Test-NativeStepSucceeded -NativeRunnerResult $NativeRunnerResult -Stage "smoke-phase-0"
    $Phase2SmokeAttempted = [bool]$Phase2SmokeResult
    $Phase2SmokeTested = [bool](
        $Phase2SmokeResult -and
        [int]$Phase2SmokeResult.exitCode -eq 0 -and
        $Phase2SmokeResult.payload -and
        [bool]$Phase2SmokeResult.payload.ok
    )
    $NativeProductionReady = [bool]($Phase0SmokeTested -and $Phase2SmokeTested)
    $NativeFatal = [bool]($NativeExitCode -ne 0 -and -not $InteractionBoundary)
    $Installed = [bool]($Doctor.installedArtifactMatchesCandidate -or $Doctor.installedPath)
    $Built = [bool]($Doctor.builtArtifact -or $Doctor.builtArtifactCandidate)
    $MissingAllowFlags = @($NativeRunnerResult.missingAllowFlags | ForEach-Object { [string]$_ })
    return [ordered]@{
        requested = [bool]$FullNative
        ok = -not $NativeFatal
        fatal = $NativeFatal
        status = [string]$NativeRunnerResult.status
        exit_code = $NativeExitCode
        missing_allow_flags = @($MissingAllowFlags)
        current_stage = if ($LastStep) { [string]$LastStep.stage } else { [string]$Doctor.nextCommandSpec.stage }
        prereqs_ready = [bool]$Doctor.prereqsReady
        bridge_source_prepared = [bool]$Doctor.sourcePrepared
        bridge_project_wired = [bool]$Doctor.projectWired
        bridge_built = $Built
        bridge_installed = $Installed
        bridge_smoke_tested = $Phase0SmokeTested -or $Phase2SmokeTested
        vectorworks_automation_attempted = $VectorworksAutomationAttempted
        phase0_smoke_tested = $Phase0SmokeTested
        phase2_smoke_attempted = $Phase2SmokeAttempted
        phase2_smoke_tested = $Phase2SmokeTested
        native_production_ready = $NativeProductionReady
        vectorworks_interaction_required = ($InteractionBoundary -or $Phase0SmokeTested) -and -not $NativeProductionReady
        requires_action = (-not $NativeProductionReady)
        built_artifact = if ($Doctor.builtArtifact) { [string]$Doctor.builtArtifact } else { [string]$Doctor.builtArtifactCandidate }
        installed_path = [string]$Doctor.installedPath
        next_command = [string]$Doctor.nextCommand
        next_reason = [string]$Doctor.nextCommandReason
        acceptance_next_command = if ($NativeProductionReady) {
            ""
        } elseif ($Phase0SmokeTested -and -not $Phase2SmokeTested) {
            "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-vectorworks-native-smoke.ps1 -VectorworksVersion $VectorworksVersion -RestartIfRunning -RunPhase2 -AllowWriteFixture -Json"
        } else {
            [string]$Doctor.nextCommand
        }
        next_actions = @($Doctor.nextActions | ForEach-Object { [string]$_ })
        exact_remaining_action = if ($Phase0SmokeTested -and $Phase2SmokeAttempted -and -not $Phase2SmokeTested) {
            "Phase-0 native transport smoke passed, then automated phase-2 production smoke was attempted but did not pass. Open Vectorworks $VectorworksVersion to a usable document, resolve any startup/license prompts, then rerun native.acceptance_next_command."
        } elseif ($Phase0SmokeTested -and -not $Phase2SmokeTested) {
            "Phase-0 native transport smoke passed and stopped the bridge. Run native.acceptance_next_command to restart/open Vectorworks and attempt the phase-2 disposable-document production smoke before claiming native production readiness."
        } elseif ($InteractionBoundary -and -not $NativeProductionReady -and $VectorworksAutomationAttempted) {
            "The installer attempted to open/restart Vectorworks $VectorworksVersion and run smoke. Resolve any Vectorworks startup/license/plugin prompts, then rerun the smoke command from native.next_command."
        } elseif ($InteractionBoundary -and -not $NativeProductionReady) {
            "Vectorworks automation was skipped or blocked by safety flags. Run native.next_command to let the agent open/restart Vectorworks and run native smoke."
        } elseif ($NativeFatal) {
            "Fix the native runner failure in native.runs[-1], then rerun install.ps1 -FullNative."
        } elseif ($NativeProductionReady) {
            "Native bridge acceptance smoke passed. Restart/reload the MCP client and use vw_ping."
        } else {
            [string]$Doctor.nextActions[0]
        }
        runs = @($Runs)
        phase2_smoke = $Phase2SmokeResult
        doctor = $Doctor
    }
}

function Invoke-Phase2NativeSmoke {
    param([string]$RepoRoot)
    $StartSmokePath = Join-Path $RepoRoot "scripts\start-vectorworks-native-smoke.ps1"
    if (-not (Test-Path -LiteralPath $StartSmokePath -PathType Leaf)) {
        throw "Native Vectorworks launch/smoke helper was not found at $StartSmokePath"
    }
    $Args = @(
        "-VectorworksVersion", $VectorworksVersion,
        "-RestartIfRunning",
        "-RunPhase2",
        "-AllowWriteFixture",
        "-Json"
    )
    $Output = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $StartSmokePath @Args 2>&1 | Out-String
    $Exit = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
    $Payload = $null
    try {
        $Payload = $Output | ConvertFrom-Json
    } catch {
        $Payload = [pscustomobject]@{
            ok = $false
            failures = @("Phase-2 smoke helper did not emit valid JSON.")
            raw = $Output
        }
    }
    return [pscustomobject]@{
        status = "phase2_smoke_attempt"
        exitCode = $Exit
        payload = $Payload
        raw = $Output
    }
}

function Invoke-FullNativeInstall {
    param([string]$RepoRoot)
    $BaseArgs = @(
        "-VectorworksVersion", $VectorworksVersion,
        "-Configuration", $NativeConfiguration,
        "-MaxSteps", "$NativeMaxSteps",
        "-Json",
        "-AllowNetwork",
        "-AllowInstallSoftware",
        "-AllowDownloadLargeFiles",
        "-AllowModifyVectorworksUserPlugins",
        "-AllowRebootRisk"
    )
    if (-not $SkipVectorworksAutomation) { $BaseArgs += "-AllowVectorworksRestartStep" }
    if ($SdkDir) { $BaseArgs += @("-SdkDir", $SdkDir) }
    if ($SdkArchivePath) { $BaseArgs += @("-SdkArchivePath", $SdkArchivePath) }
    if ($SdkExamplesDir) { $BaseArgs += @("-SdkExamplesDir", $SdkExamplesDir) }
    if ($WorktreeRoot) { $BaseArgs += @("-WorktreeRoot", $WorktreeRoot) }
    if ($NativeInstallDir) { $BaseArgs += @("-InstallDir", $NativeInstallDir) }

    $Runs = [System.Collections.Generic.List[object]]::new()
    $OldForceRestart = $env:VW_MCP_FORCE_VECTORWORKS_RESTART
    try {
        if ($ForceVectorworksRestart) {
            $env:VW_MCP_FORCE_VECTORWORKS_RESTART = "1"
        }

        $First = Invoke-NativeRunnerJson -RepoRoot $RepoRoot -Arguments $BaseArgs
        $Runs.Add($First.payload) | Out-Null
        $Doctor = Get-NativeDoctorJson -RepoRoot $RepoRoot
        $FinalPayload = $First.payload
        $FinalExit = $First.exitCode

        if ([string]$First.payload.status -eq "dry_run_executed" -and $Doctor.builtArtifactCandidate) {
            $InstallArgs = @($BaseArgs) + @("-BuiltArtifact", [string]$Doctor.builtArtifactCandidate, "-Install")
            $Second = Invoke-NativeRunnerJson -RepoRoot $RepoRoot -Arguments $InstallArgs
            $Runs.Add($Second.payload) | Out-Null
            $Doctor = Get-NativeDoctorJson -RepoRoot $RepoRoot
            $FinalPayload = $Second.payload
            $FinalExit = $Second.exitCode
        }

        $Phase2SmokeResult = $null
        if (-not $SkipVectorworksAutomation -and (Test-NativeStepSucceeded -NativeRunnerResult $FinalPayload -Stage "smoke-phase-0")) {
            $Phase2SmokeResult = Invoke-Phase2NativeSmoke -RepoRoot $RepoRoot
            $Runs.Add($Phase2SmokeResult) | Out-Null
        }

        return New-NativeSummary -NativeRunnerResult $FinalPayload -NativeExitCode $FinalExit -Doctor $Doctor -Runs @($Runs) -Phase2SmokeResult $Phase2SmokeResult
    } finally {
        if ($null -eq $OldForceRestart) {
            Remove-Item Env:\VW_MCP_FORCE_VECTORWORKS_RESTART -ErrorAction SilentlyContinue
        } else {
            $env:VW_MCP_FORCE_VECTORWORKS_RESTART = $OldForceRestart
        }
    }
}

function New-InstallPayload {
    param(
        [bool]$Ok,
        [string]$RepoRoot,
        [string]$Message,
        [object]$Native = $null,
        [string]$ErrorMessage = ""
    )
    $LoaderPath = if ($RepoRoot) { Join-Path $RepoRoot "vw_load_listener_2024.py" } else { "" }
    $LauncherPath = if ($RepoRoot) { Join-Path $RepoRoot "vw_start_listener_2024.py" } else { "" }
    $McpConfigPath = if ($RepoRoot) { Join-Path $RepoRoot ".mcp.json" } else { "" }
    $NativeRequiresAction = if ($Native) { [bool]$Native.requires_action } else { $false }
    $NativeReady = if ($Native) { [bool]$Native.native_production_ready } else { $false }
    $NativeSummary = if ($Native) {
        [ordered]@{
            ready = [bool]$Native.native_production_ready
            next_stage = [string]$Native.current_stage
            next_command = [string]$Native.next_command
            next_reason = [string]$Native.next_reason
            acceptance_next_command = [string]$Native.acceptance_next_command
            missing_allow_flags = @($Native.missing_allow_flags)
            prereqs_ready = [bool]$Native.prereqs_ready
            bridge_built = [bool]$Native.bridge_built
            bridge_installed = [bool]$Native.bridge_installed
            vectorworks_automation_attempted = [bool]$Native.vectorworks_automation_attempted
            phase0_smoke_tested = [bool]$Native.phase0_smoke_tested
            phase2_smoke_attempted = [bool]$Native.phase2_smoke_attempted
            phase2_smoke_tested = [bool]$Native.phase2_smoke_tested
            vectorworks_interaction_required = [bool]$Native.vectorworks_interaction_required
        }
    } else {
        $null
    }
    return [ordered]@{
        ok = $Ok
        setup_complete = $Ok
        install_complete = $Ok
        usable_now = $Ok
        requires_action = -not $Ok
        production_ready = [bool]($Ok -and (-not $FullNative -or $NativeReady))
        client = $Client
        repo_root = $RepoRoot
        mcp_config = $McpConfigPath
        vectorworks_loader = $LoaderPath
        vectorworks_launcher = $LauncherPath
        dependency_checks = @($DependencyChecks)
        native_requested = [bool]$FullNative
        native_ready = $NativeReady
        native_requires_action = $NativeRequiresAction
        native_summary = $NativeSummary
        native = $Native
        user_message = $Message
        next_action = if ($Ok -and $Native -and $Native.native_production_ready) {
            "Native bridge acceptance smoke passed. Trust or reload the MCP client, then use vw_ping and production CAD tools."
        } elseif ($Ok -and $Native -and $Native.vectorworks_interaction_required) {
            $Native.exact_remaining_action
        } elseif ($Ok) {
            "Trust or add the repo .mcp.json in your MCP client, run vw_load_listener_2024.py in Vectorworks, then call vw_ping."
        } else {
            "Fix the reported installer error, then rerun install.ps1."
        }
        native_note = if ($FullNative) {
            "Full native setup attempts to open/restart Vectorworks and run native smoke automatically. The installer does not claim native production readiness until phase-0 and phase-2 smoke pass."
        } else {
            "The native SDK bridge is an optional non-modal upgrade. Run install.ps1 -FullNative to build/install it on this PC."
        }
        raw_install_url = $RawInstallUrl
        error = $ErrorMessage
    }
}

function Write-InstallPayload {
    param([System.Collections.IDictionary]$Payload)
    if ($Json) {
        $Payload | ConvertTo-Json -Depth 18
        return
    }

    if ($Payload.ok) {
        Write-Host $Payload.user_message
        Write-Host "Repo: $($Payload.repo_root)"
        Write-Host "MCP config: $($Payload.mcp_config)"
        Write-Host "Vectorworks loader: $($Payload.vectorworks_loader)"
        if ($Payload.native_requested) {
            Write-Host "Native prereqs ready: $($Payload.native.prereqs_ready)"
            Write-Host "Native bridge built: $($Payload.native.bridge_built)"
            Write-Host "Native bridge installed: $($Payload.native.bridge_installed)"
            Write-Host "Native bridge smoke-tested: $($Payload.native.bridge_smoke_tested)"
        }
        Write-Host "Next: $($Payload.next_action)"
    } else {
        Write-Error $Payload.user_message
        if ($Payload.error) {
            Write-Error $Payload.error
        }
    }
}

try {
    Ensure-BaseDependencies
    $RepoRoot = Resolve-ConnectorRepoRoot
    $BootstrapPath = Join-Path $RepoRoot "scripts\bootstrap-agent.ps1"
    $BootstrapArgs = @("-Client", $Client)
    if (-not $NoVerify) { $BootstrapArgs += "-Verify" }
    if ($SkipClipboard) { $BootstrapArgs += "-SkipClipboard" }

    Push-Location $RepoRoot
    try {
        if ($Json) {
            $BootstrapOutput = & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $BootstrapPath @BootstrapArgs 2>&1
            if ($LASTEXITCODE -ne 0) {
                throw "bootstrap-agent.ps1 failed with exit code $LASTEXITCODE. Output: $($BootstrapOutput -join [Environment]::NewLine)"
            }
        } else {
            & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $BootstrapPath @BootstrapArgs
            if ($LASTEXITCODE -ne 0) {
                throw "bootstrap-agent.ps1 failed with exit code $LASTEXITCODE"
            }
        }
    } finally {
        Pop-Location
    }

    $NativeSummary = $null
    if ($FullNative) {
        $NativeSummary = Invoke-FullNativeInstall -RepoRoot $RepoRoot
        if ($NativeSummary.fatal) {
            throw "Full native setup failed before reaching the Vectorworks interaction boundary."
        }
    }

    $Message = if ($FullNative -and $NativeSummary.native_production_ready) {
        "Vectorworks MCP installed. Native bridge is built, installed, and smoke-tested."
    } elseif ($FullNative -and $NativeSummary.vectorworks_interaction_required) {
        "Vectorworks MCP installed. Native bridge is built/installed and waiting for Vectorworks plug-in load plus smoke test."
    } else {
        "Vectorworks MCP installed and usable now with the Python dialog fallback."
    }
    $Payload = New-InstallPayload -Ok $true -RepoRoot $RepoRoot -Message $Message -Native $NativeSummary
    Write-InstallPayload $Payload
    exit 0
} catch {
    $Payload = New-InstallPayload -Ok $false -RepoRoot $RepoRoot -Message "Vectorworks MCP install failed." -ErrorMessage $_.Exception.Message
    Write-InstallPayload $Payload
    exit 1
}
