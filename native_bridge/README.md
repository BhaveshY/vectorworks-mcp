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
- `foreground`: legacy diagnostic only. It can monopolize Vectorworks' UI and
  is blocked for real CAD handlers by host preflight.
- `background`: transport-only. It can bind a socket, but Vectorworks does not
  reliably service CAD work after the script returns.
- `win_timer`: transport-only. It can answer `ping`, but CAD handlers can
  deadlock because they run outside a valid Vectorworks script/plugin context.

The durable fix is a native Vectorworks SDK plug-in bridge. Network I/O may run
on a worker thread, but every Vectorworks document/API operation must be
marshaled back onto the Vectorworks main/plugin event context.

This is the main difference from a Revit-style connector. Revit add-ins have a
well-established loading and API-dispatch model, so a connector can lean on the
host application's external command/event pattern. Vectorworks Python is easier
to paste and test, but it is not an always-on integration surface: foreground
loops freeze the UI, background/timer loops are transport diagnostics, and real
document work must happen inside a valid script/dialog/plugin callback. The
native bridge exists to provide that non-modal plug-in callback path.

## Target Architecture

```text
Codex / Claude Code / MCP client <--stdio--> server.py
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

The local protocol is authenticated by default. The native bridge reads
`VW_MCP_AUTH_TOKEN`, then `VW_MCP_AUTH_TOKEN_FILE`, then the default
`%USERPROFILE%\.vectorworks-mcp\auth-token`; unauthenticated frames are rejected
unless `VW_MCP_INSECURE_NO_AUTH=1` is explicitly set for diagnostics.

Use `HANDLER_MATRIX.md` as the native implementation roadmap and
`PROTOCOL.md` as the authoritative wire contract (protocol v2, capability
revision 6). Validate end-to-end against a loaded bridge with
`python native_bridge\live_acceptance_v2.py` (creates its own untitled
documents) and `scripts\smoke-native-bridge.ps1 -Json`.

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
  Vectorworks' SDK examples list Vectorworks 2024 on Windows as requiring
  Visual Studio 2022 version 17.6.3 with toolset v143:
  https://github.com/VectorworksDeveloper/SDKExamples

The local machine may not have these installed. In that case the checker should
fail clearly and tell the next agent exactly what is missing.

## Bootstrap Helper

The bootstrap helper does not download the SDK unless explicitly requested:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-native-bridge.ps1
```

It can also perform the opt-in Windows setup path:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-native-bridge.ps1 -InstallVisualStudioBuildTools -DownloadSdk -CloneSdkExamples -PrepareSource
```

`-InstallVisualStudioBuildTools` uses winget package
`Microsoft.VisualStudio.2022.BuildTools` with the C++ workload. It may require
administrator approval or a reboot before MSBuild is available.

To download the official Windows SDK archive into a repo-local ignored folder:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-native-bridge.ps1 -DownloadSdk
```

Downloading is opt-in because the SDK archive is large and covered by
Vectorworks' SDK terms.

## 1.0 Layout and Build

Since 1.0 the plug-in project is version controlled:

```text
native_bridge/
  src/                      bridge sources (protocol v2, see PROTOCOL.md)
  project/
    VectorworksMCPBridge.vcxproj   MSBuild project (VWVersion=2024|2025)
    Source/ModuleMain.cpp          module entry (no tool/object registrations)
    Source/Prefix/                 precompiled header
    VectorworksMCPBridge.vwr/      resource folder packed by buildvwr
    generated/BuildInfo.h          written by the build script (git-ignored)
    out/<ver>/Release/             VectorworksMCPBridge.vlb + .vwr (git-ignored)
  live_acceptance_v2.py     live acceptance against an installed bridge
  PROTOCOL.md               wire protocol v2 (authoritative)
```

The project references the SDK through MSBuild properties instead of copying
it: `VWSDKRoot` defaults to
`third_party\VectorworksSDKExamples\VectorworksSDK\SDK<VWVersion>` and
`VWThirdParty` to `third_party\VectorworksSDKExamples\ThirdPartySource`
(libcurl, linked like the SDK sample). Get the SDK examples clone with
`scripts\prepare-native-bridge-source.ps1 -CloneSdkExamples` (the generated
`worktree/` is no longer needed for building).

Build (Visual Studio 2022 Build Tools, v143, x64):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-native-bridge.ps1 -VectorworksVersion 2024
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-native-bridge.ps1 -VectorworksVersion 2025
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-native-bridge.ps1 -VectorworksVersion all -Json
```

Output: `native_bridge\project\out\<ver>\Release\VectorworksMCPBridge.vlb`
and `.vwr` plus `build-info.json` (build id, sha256). The build id
(`<git short hash>[-dirty]-<UTC timestamp>`) is baked into the module and
reported by `ping.build_id` and the discovery file. `-SdkDir` overrides the SDK
root, `-Clean` rebuilds, `-Configuration Debug` builds a debug module.

Install by copying both files into
`%APPDATA%\Nemetschek\Vectorworks\<ver>\Plug-ins` while Vectorworks is
closed, and remove the pre-1.0 `ObjectExample.vlb/.vwr` (two bridges would
compete for the port range). Then run
`python native_bridge\live_acceptance_v2.py` with Vectorworks running.

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
