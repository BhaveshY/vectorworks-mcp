[CmdletBinding()]
param(
    [switch]$RequireCompiler
)

# Compatibility shim. The compile-only native scaffold harness was removed in
# favour of live end-to-end validation (scripts\smoke-native-bridge.ps1 and
# scripts\run-native-full-acceptance.py against a running Vectorworks).
# This file stays only because older standalone plugin releases check that it
# exists before accepting a connector checkout. It performs no checks.
Write-Host "test-native-bridge-scaffold.ps1 is retired; run scripts\smoke-native-bridge.ps1 -Json against a running Vectorworks instead."
exit 0
