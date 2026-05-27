# Native Vectorworks Bridge

This directory is the long-term implementation track for a stable, always-on
Vectorworks integration.

## Why This Exists

The current Python listener is useful for setup and one-off agent sessions, but
Vectorworks 2024 does not provide a pure-Python background execution context
that is both non-modal and safe for real `vs.*` document operations.

The tested Python modes behave like this:

- `dialog`: safe for real CAD handlers because requests run from a Vectorworks
  dialog timer callback. It is modal, so it is an agent-control session.
- `background`: transport-only. It can bind a socket, but Vectorworks does not
  reliably service CAD work after the script returns.
- `win_timer`: transport-only. It can answer `ping`, but CAD handlers can
  deadlock because they run outside a valid Vectorworks script/plugin context.

The durable fix is a native Vectorworks SDK plug-in bridge. Network I/O may run
on a worker thread, but every Vectorworks document/API operation must be
marshaled back onto the Vectorworks main/plugin event context.

## Target Architecture

```text
Claude Code <--stdio--> server.py
                         |
                         v
                 TCP length-prefixed JSON
                         |
                         v
            Native Vectorworks SDK plug-in bridge
                         |
                         v
            Vectorworks SDK main/event context
```

The host MCP server can keep using the same TCP protocol it uses today. The
native bridge should replace `vw_listener.py` for always-on production use, not
replace the host MCP tool surface.

## Build Prerequisites

Run the prerequisite checker from the repo root:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\check-native-bridge-prereqs.ps1
```

Required for Vectorworks 2024 on Windows:

- Vectorworks 2024 installed.
- Vectorworks 2024 SDK for Windows from the official SDK page:
  https://www.vectorworks.net/en-US/support/custom/sdk/sdkdown
- Visual Studio 2022 Build Tools with the Desktop development with C++ workload.

The local machine may not have these installed. In that case the checker should
fail clearly and tell the next agent exactly what is missing.

## Bootstrap Helper

The bootstrap helper does not download the SDK unless explicitly requested:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-native-bridge.ps1
```

To download the official Windows SDK archive into a repo-local ignored folder:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-native-bridge.ps1 -DownloadSdk
```

Downloading is opt-in because the SDK archive is large and covered by
Vectorworks' SDK terms.

## Implementation Rules

- Do not run `vs.*` or SDK document APIs from a socket worker thread.
- Do not re-enable Python `background` or `win_timer` modes for real CAD
  handlers.
- Keep `ping` independent of document state so transport diagnostics stay fast.
- Use the same request and response protocol documented in `PROTOCOL.md`.
- Keep the Python `dialog` launcher as a fallback agent-session mode until the
  native bridge is compiled and installed.

## Acceptance Criteria

The native bridge is not considered complete until all of these pass:

- `vw_ping` responds repeatedly without a modal Vectorworks dialog.
- `vw_get_document_info` and `vw_get_layers` complete repeatedly without
  freezing Vectorworks.
- Vectorworks remains manually usable while the bridge is idle.
- `vw_stop_listener` or an equivalent stop command releases port `9877`.
- The no-Vectorworks unit suite still passes.
- A real Vectorworks 2024 smoke test documents the exact Vectorworks update and
  bridge build used.
