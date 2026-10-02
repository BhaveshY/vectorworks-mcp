# Vectorworks MCP 0.7.0 stabilization

Core drawing operations and the experimental documentation workflow have
separate release gates. Core native acceptance passed on Vectorworks 2024
29.0.8 (787494). Documentation writes are unavailable in this release because
annotation Undo/Redo did not restore the expected state consistently. Both the
capability manifest and native dispatch enforce this restriction. Sheet,
viewport, and annotation reads remain available.

## Core fixes

- Unauthenticated stop requests cannot stop the listener.
- Finished client threads are joined and reclaimed during normal operation.
- Idempotent core replay binds to the drawing header and bridge session rather
  than the active layer, avoiding a document-identity change on layer switches.
- Exact-name queries use native lookup, including doors and windows hosted in
  walls. Name/class filters run before pagination rather than after a capped
  scan of top-level objects.
- FastMCP 4.0.3, MCP 2.2.0, and Pydantic 2.13.5 are pinned. The complete runtime
  dependency lock has hashes, is used by bootstrap, and has a CI drift check.
- The stdio test actually executes its async body. Modern MCP 2026-07-28 and
  legacy 2025-11-25 clients share the same validated tool contract.
- Native compiler regressions are mandatory in CI.

## Evidence

The Windows/Python 3.12 suite ran 311 tests: 310 passed and one machine-state
test was intentionally skipped because native installation was already ready.
Focused documentation/native contracts also passed after the release gate.
The real Vectorworks 2024 SDK Release build and native scaffold regressions
passed, including rejected-stop behavior and client-handle reclamation.

The disposable full native acceptance run passed all 20 advertised creation
types, all five core apply operations, hosted Door/Window readback, idempotent
replay, compound rollback, and PNG/PDF/DWG/VWX export/open workflows. The report
is `VW_MCP_P4_20260908-093848-4967f345-report.json` in the sibling stabilization
evidence directory. This core result remains applicable to the final candidate:
subsequent CAD changes were confined to documentation handlers and their gate.

The normal registered Codex launcher was tested with MCP 2026-07-28, returning
nine grouped tools and a healthy native bridge. A client restart is required to
reload an already-running MCP process after installation/configuration changes.

## Documentation findings and save recovery

Live investigation fixed redundant sheet renaming, the title/description field
mapping, and the default viewport view type. Creation/read/update/deletion then
passed, but native Undo/Redo failed for annotation edits/deletion. The attempted
SDK-managed Undo ownership allowance was removed; transaction safeguards remain.

The experimental fixture developed a transient save failure after documentation
and Undo work. Both MCP save and Vectorworks' own Save command failed, despite
ample free disk space. A fresh core fixture accepted a rectangle and normal save
using the same host and connector. Save As preserved the affected document;
reopening that copy allowed a new core edit and normal save. Thus the failure
was localized to that in-memory experimental document state, not a general core
save or disk-capacity failure. The precise SDK operation that caused the state
is not isolated. Documentation writes remain blocked rather than assuming that
Save As makes the experimental workflow safe.

Relevant local evidence: `vw-core-normal-save.log`,
`vw-preserved-edit.log`, `vw-preserved-normal-save.log`,
`vectorworks-documentation-ownership.log`, and the lifecycle readback JSON files.

## Limits and rollback

Vectorworks 2025 was not live-tested; Python 3.10 was not exercised locally.
Do not infer those host/runtime results from a successful 2024/Python 3.12 run.
No broad speedup claim is made. Recorded timings are fixture-specific.

The original dirty main checkout was preserved. Changes are on
`codex/stabilize-mcp-2026-09-08`, based on the documentation feature branch.
Before reverting the native plug-in, save work and close Vectorworks normally;
restore the backed-up ObjectExample.vlb/.vwr together. Restore the matching host
runner/client configuration too. The earlier bridge retains the transport bugs
fixed here, so rollback is a recovery option, not the preferred daily runtime.
