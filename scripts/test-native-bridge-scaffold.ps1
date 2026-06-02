[CmdletBinding()]
param(
    [switch]$RequireCompiler
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$NativeSrc = Join-Path $RepoRoot "native_bridge\src"
$NativeTests = Join-Path $RepoRoot "native_bridge\tests"
$Harness = Join-Path $NativeTests "native_scaffold_smoke.cpp"
$ProtocolSource = Join-Path $NativeSrc "BridgeProtocol.cpp"
$TransportSource = Join-Path $NativeSrc "NativeTransport.cpp"
$BridgeSource = Join-Path $NativeSrc "VectorworksMCPBridge.cpp"
$RequiredPaths = @(
    (Join-Path $NativeSrc "BridgeProtocol.hpp"),
    $ProtocolSource,
    (Join-Path $NativeSrc "NativeTransport.hpp"),
    $TransportSource,
    (Join-Path $NativeSrc "BridgeDispatcher.hpp"),
    (Join-Path $NativeSrc "CadRequestQueue.hpp"),
    $BridgeSource,
    $Harness
)

foreach ($Path in $RequiredPaths) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Native bridge scaffold test input was not found at $Path"
    }
}

function Resolve-CxxCompiler {
    foreach ($Name in @("cl.exe", "clang++.exe", "g++.exe", "c++.exe")) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if ($Command) {
            return $Command
        }
    }
    return $null
}

$Compiler = Resolve-CxxCompiler
if (-not $Compiler) {
    $Message = "No C++ compiler found; skipping SDK-free native bridge scaffold compile smoke. Checked cl.exe, clang++.exe, g++.exe, and c++.exe."
    if ($RequireCompiler) {
        throw $Message
    }
    Write-Warning $Message
    exit 0
}

$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("vectorworks-mcp-native-scaffold-{0}" -f $PID)
$ObjDir = Join-Path $TempRoot "obj"
$ExePath = Join-Path $TempRoot "native_scaffold_smoke.exe"
New-Item -ItemType Directory -Force -Path $ObjDir | Out-Null

try {
    if ($Compiler.Name -ieq "cl.exe") {
        $FoArg = "/Fo{0}{1}" -f $ObjDir, [System.IO.Path]::DirectorySeparatorChar
        & $Compiler.Source /nologo /std:c++17 /EHsc "/I$NativeSrc" $FoArg "/Fe:$ExePath" $Harness $ProtocolSource $TransportSource $BridgeSource Ws2_32.lib
    } else {
        $LinkArgs = @()
        if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
            $LinkArgs += "-lws2_32"
        }
        & $Compiler.Source -std=c++17 -I $NativeSrc $Harness $ProtocolSource $TransportSource $BridgeSource -o $ExePath @LinkArgs
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Native bridge scaffold compile failed with exit code $LASTEXITCODE"
    }

    & $ExePath
    if ($LASTEXITCODE -ne 0) {
        throw "Native bridge scaffold smoke failed with exit code $LASTEXITCODE"
    }
} finally {
    Remove-Item -LiteralPath $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
