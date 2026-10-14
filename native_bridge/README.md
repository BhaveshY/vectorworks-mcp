# Native Vectorworks bridge (VectorworksMCPBridge)

A Vectorworks SDK plug-in (`VectorworksMCPBridge.vlb` + `.vwr`) that gives the MCP host a
non-modal, always-on connection to Vectorworks 2024/2025:

- **Transport** — loopback TCP, 4-byte big-endian length + UTF-8 JSON, per-user token
  (`%USERPROFILE%\.vectorworks-mcp\auth-token`). Exclusive bind on the first free port in
  9877-9886; each instance writes `%USERPROFILE%\.vectorworks-mcp\instances\<pid>.json`.
- **Threading** — sockets on worker threads; every document/API call runs on the Vectorworks
  main thread through a message-only window pump, gated by the UI state (idle / modal /
  tracking / loading / no_document). Long work becomes a job (`job_status`).
- **Dialogs** — the Home screen, the update notice and the autosave "name and save" prompt are
  dismissed when an agent request is blocked; message boxes and report dialogs a handler
  itself raises (OK/Close/Done only, e.g. the IFC Export Report) are acknowledged with
  `BM_CLICK` and returned as text; every other dialog is reported, never touched.
- **Scripting** — `run_script` runs Python through `ScriptContext_Begin/RunEx` (or VectorScript)
  as one undo step with rollback; access violations are contained (`script_crashed`).

The wire contract is [PROTOCOL.md](PROTOCOL.md) (protocol v2, capability revision 6). The
`vs.*` calls the bridge and the host snippets use are audited in [VS_CALL_AUDIT.md](VS_CALL_AUDIT.md).

## Layout

| Path | Content |
|---|---|
| `src/` | bridge sources (transport, scheduler, UI gate, dialog watch, documents, handlers, scripting) |
| `project/` | Visual Studio 2022 project (`VectorworksMCPBridge.vcxproj`, `Source/ModuleMain.cpp`, `.vwr`) |
| `project/out/<ver>/Release/` | build output (`.vlb`, `.vwr`, `build-info.json`) — not committed |
| `live_acceptance_v2.py` | live acceptance against a running Vectorworks (creates and discards its own documents) |

## Build

Requires Visual Studio 2022 Build Tools (v143, x64) and the Vectorworks SDK examples clone in
`third_party/VectorworksSDKExamples/VectorworksSDK/SDK2024` / `SDK2025`
(check with `scripts\check-native-bridge-prereqs.ps1`).

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-native-bridge.ps1 -VectorworksVersion all -Json
```

## Install and test

Vectorworks must be closed to replace the plug-in (the DLL stays loaded):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -FullNative     # build + install + register
py -3.12 native_bridge\live_acceptance_v2.py                                    # live checks (--skip-ifc / --skip-pdf to shorten)
```

On a test PC, `scripts\dev-restart-vectorworks.ps1` restarts Vectorworks (refuses if saved
documents are open), presses the licence Login button and waits for the bridge.
