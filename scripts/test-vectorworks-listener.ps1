[CmdletBinding()]
param(
    [string]$HostName = "",
    [ValidateRange(1, 65535)]
    [int]$Port = 0,
    [ValidateRange(1, 120)]
    [int]$TimeoutSeconds = 5
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not $HostName) {
    $HostName = if ($env:VW_MCP_HOST) { $env:VW_MCP_HOST } else { "127.0.0.1" }
}
if ($Port -eq 0) {
    $Port = if ($env:VW_MCP_PORT) { [int]$env:VW_MCP_PORT } else { 9877 }
}

if (Test-Path $VenvPython) {
    $PythonCommand = $VenvPython
    $PythonArgs = @()
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonCommand = "py"
    $PythonArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonCommand = "python"
    $PythonArgs = @()
} else {
    throw "Python was not found. Run scripts\bootstrap-agent.ps1 first or install Python 3."
}

$Code = @'
import json
import socket
import struct
import sys

host = sys.argv[1]
port = int(sys.argv[2])
timeout = float(sys.argv[3])
request = {"id": "manual-ping", "action": "ping", "params": {}}
payload = json.dumps(request).encode("utf-8")

def read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("listener closed the connection before sending a full response")
        data.extend(chunk)
    return bytes(data)

try:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(struct.pack(">I", len(payload)) + payload)
        size = struct.unpack(">I", read_exact(sock, 4))[0]
        response = json.loads(read_exact(sock, size).decode("utf-8"))
except Exception as exc:
    print("ERROR: could not reach Vectorworks listener at {0}:{1}: {2}".format(host, port, exc), file=sys.stderr)
    sys.exit(1)

print(json.dumps(response, indent=2, sort_keys=True))
if not response.get("success"):
    sys.exit(2)
'@

$TempScript = Join-Path ([System.IO.Path]::GetTempPath()) "vw_listener_ping_$PID.py"
try {
    Set-Content -LiteralPath $TempScript -Value $Code -Encoding UTF8
    & $PythonCommand @PythonArgs $TempScript $HostName $Port $TimeoutSeconds
    exit $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $TempScript -Force -ErrorAction SilentlyContinue
}
