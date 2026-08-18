# Continue Vectorworks MCP on another PC

Use this runbook to resume the revision 4 native-bridge work in Codex. Start
from `main`. Do not restore the Python listener or add a second write path.

## Current state

The connector now exposes nine grouped MCP tools:

- `vw_status`
- `vw_read`
- `vw_catalog`
- `vw_apply`
- `vw_execute_operations`
- `vw_io`
- `vw_view`
- `vw_document`
- `vw_tool_safety`

`vw_apply` and `vw_execute_operations` use the same native atomic transaction
engine. Revision 4 adds authoritative capability negotiation, a runtime build
fingerprint, transaction-owned temporary objects, exact-UUID rollback cleanup,
and checked undo-event commits. It also adds SDK handlers for true Space, Slab,
and Roof objects, runtime Door and Window schema discovery, wall-host checks,
resources, symbols, worksheets, native file I/O, views, and document state.

The host no longer creates a hidden layer or a blank document to make a write
succeed. Legacy Python handlers that claimed fake BIM or export behavior remain
disabled.

## Evidence from the source PC

The SDK Release build completed with zero warnings and zero errors on
2026-08-18. The guarded doctor installed `ObjectExample.vlb` into the
Vectorworks 2024 user Plug-ins folder. The built and installed files had this
SHA-256:

```text
2ADC55CFF421CF694735F468F9B1C6C7C624B524E8EB3748BCFAA155C873E8EB
```

Before the last live attempt, the installed bridge reported:

- native SDK dispatch
- native phase 4
- capability revision 4
- capability fingerprint `4524e604f4d0fcf7`
- `cad_api_safe=true`
- `transport_only=false`
- `main_context_pump_ready=true`
- 31 native actions and 20 create kinds

Live runtime schema discovery also found the universal `Door` and `Window`
plug-ins. The Door schema returned 502 parameters and the Window schema returned
437 parameters. Both schemas exposed the required size fields.

The focused core test run passed 185 tests. Six focused packaging and
documentation checks also passed. `git diff --check` found no whitespace
errors. The complete historical suite still contains environment-dependent and
stale tests, including user-token ACL checks and tests that conflict with a live
listener port. Do not treat those unrelated failures as revision 4 regressions.

## Live blocker

The final consolidated acceptance run sent no write request. Its first
`vw_status(health)` call timed out because Vectorworks had exited.

Windows recorded a Vectorworks crash in `libcef.dll` with exception
`c0000005` or `c000041d`, at offset `0x406304e`. The event did not identify the
native bridge as the faulting module. A later Vectorworks restart reached
`Startup: Finished Setup`, but the bridge socket did not open. A recovery,
license, or plug-in approval prompt may have blocked startup.

Clear any visible Vectorworks prompt before the next live run. Do not use mouse
automation to bypass the prompt.

## Live work that remains unverified

Do not claim production acceptance until one live run proves all of these
items:

- A failed compound Space transaction rolls back both the final Space and its
  temporary boundary geometry.
- One atomic commit creates true Space, Slab, and Roof objects with semantic
  readback, then Undo removes them and Redo restores only those objects.
- Door and Window creation uses the discovered schema fingerprint, inserts into
  an exact wall UUID, and verifies wall hosting.
- A complete 2BHK plan uses true Spaces, walls, labels, dimensions, and native
  mutations without substitute geometry.
- Idempotent replay creates no duplicates, and reuse of the key with a changed
  plan is rejected before a write.
- Resource, symbol, worksheet, class, query, view, capture, export, save, and
  document-state operations return compact native receipts.

## Set up the second PC

Read `AGENT_INSTALL.md` before changing the machine. On Windows 11, clone the
current `main` branch and prepare the host:

```powershell
git clone https://github.com/BhaveshY/vectorworks-mcp.git $env:USERPROFILE\repos\vectorworks-mcp
Set-Location $env:USERPROFILE\repos\vectorworks-mcp
git switch main
git pull --ff-only origin main
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Json
py -3 .\plugins\vectorworks\bin\vectorworksctl doctor --repo-path $PWD --json
```

The normal installer prepares the host and prints the guarded native plan. It
does not authorize Visual Studio installation, SDK downloads, writes to the
Vectorworks Plug-ins folder, or a Vectorworks restart.

If the user authorizes those side effects, run the full native path:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -FullNative -Json
```

If setup stops between native stages, run the command from the doctor's
`nextCommandSpec`. The bundled helper exposes the same guarded step:

```powershell
py -3 .\plugins\vectorworks\bin\vectorworksctl native-next --repo-path $PWD --json --allow-network --allow-install-software --allow-download-large-files --allow-reboot-risk
```

Pass only the allow flags that the user approved. If the doctor reports an SDK
archive candidate, pass its full path with `--sdk-archive-path` and reuse it.

After installation, open Vectorworks with a disposable document that already
has a design layer. Restart Codex after MCP registration or trust changes.

The repository includes an MCP-only acceptance runner. Keep the disposable
document active, then run:

```powershell
.\.venv\Scripts\python.exe .\scripts\run-native-full-acceptance.py `
  --source-document C:\path\to\disposable.vwx `
  --output-dir C:\path\to\acceptance-output `
  --allow-write-fixture
```

The runner rejects a nonempty source by default. Add `--open-document` only
after saving or closing the current document. The runner never supplies a
dirty-document replacement override.

## Run one live acceptance cycle

Run the checks below once, in order. Keep document open, save, and export tests
at the end because they can change document state.

1. Run `vw_status` and the CAD preflight. Require native SDK dispatch, phase 4,
   revision 4, `cad_api_safe=true`, `transport_only=false`,
   `main_context_pump_ready=true`, and `apply_operations`.
2. Confirm that all nine grouped tools load, read `vw_tool_safety`, and verify
   that the runtime fingerprint matches the installed build.
3. Read the drawing summary and query a small object set with filters.
4. Discover Door and Window schemas and retain their exact fingerprints.
5. Send one deliberate compound-object failure. Verify zero final objects and
   zero temporary profiles by UUID.
6. Send one atomic `vw_apply` plan that covers established geometry,
   properties, transforms, duplication, and deletion. Replay it through
   `vw_execute_operations` with the same idempotency key.
7. Create true Space, Slab, and Roof objects. Verify their semantic types and
   fields, then run one Undo and one Redo.
8. Create a wall-hosted Door and Window only when their exact advertised
   capabilities and schema fingerprints are present. Verify the host wall UUID.
9. Exercise resources, symbols, worksheets, classes, views, capture, and native
   I/O through their grouped actions.
10. Verify that the replay created no duplicates. Reuse the key with a changed
    plan and require rejection before dispatch.
11. Create one atomic 2BHK plan with true Spaces. Verify room names, dimensions,
    object kinds, bounds, and zero temporary geometry.
12. Save and export last. Record the active document identity before and after
    each document operation.

If an advertised operation fails, fix or withdraw that capability. Do not
replace it with a primitive, extrusion, schematic object, menu command, Python
script, dialog, or mouse action.

## Runtime rules

- Use only the compiled native SDK bridge for normal work.
- Require phase 4 and `apply_operations` before CAD writes.
- Keep Vectorworks usable by the person while Codex works.
- Do not enable the Python dialog fallback without explicit consent. It blocks
  manual Vectorworks use and is not production completion.
- Do not route CAD calls through Python background or timer modes. They are
  transport diagnostics only.
- Do not decompose an atomic native plan into legacy batch calls after a
  failure.
- Do not advertise an action unless the native bridge implements it and live
  semantic readback confirms it.

Keep `native_bridge/HANDLER_MATRIX.md`, the grouped tool map, the bundled work
skill, and capability revision tests aligned when the contract changes.
