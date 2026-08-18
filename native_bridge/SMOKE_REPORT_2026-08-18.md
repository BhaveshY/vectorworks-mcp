# Vectorworks MCP consolidated live smoke report

> Historical baseline: this report records the revision 3 smoke run that drove
> the revision 4 implementation. It does not describe the current source tree.
> The revision 4 SDK bridge later built with zero warnings and zero errors, and
> 185 focused host/native contract tests passed. A read-only live probe verified
> the revision 4 capability fingerprint, the compact nine-tool MCP surface, and
> runtime Door and Window schemas. The final compound rollback, smart-BIM 2BHK,
> hosted opening, Undo/Redo, and file-operation acceptance run remains pending
> because Vectorworks 2024 crashed in `libcef.dll` before the first write was
> sent. See `CONTINUE_ON_ANOTHER_PC.md` for the current state and resume steps.

Date: 2026-08-18

Environment: Vectorworks 2024, existing native SDK bridge, blank/disposable document

Built bridge artifact SHA-256: `19A218D1D5287351A9BC7CB8B77547D9A8992B25CB790E0B4EC6E4A78BFB0B53` (the loaded binary hash was not independently readable while Vectorworks was open)
Observed runtime: native phase 4, capability revision 3, `cad_api_safe=true`, `transport_only=false`, `main_context_pump_ready=true`

## Executive verdict

The connector foundation is promising but this build is **not release-ready and not capability-complete**.

- The compact nine-tool MCP surface, native reads, ordinary geometry, mixed atomic commits/idempotency, and native file exports worked. DWG import mutated the document successfully, but its receipt and state-restoration contract are incomplete. Compound-object rollback did not work safely.
- Smart BIM creation is not production-safe yet. Space, Slab, and Roof each failed live, and Space/Slab left source-profile geometry behind.
- Door and Window could not be discovered through the advertised parametric-schema path, so hosted smart-object workflows remain unavailable.
- Document open changed the active document even though the tool returned an error. This is an unknown-state lifecycle defect and a release blocker.
- The capability report currently overstates several operations. A capability must not be advertised until its exact public request, SDK implementation, semantic readback, transaction behavior, and undo/redo lifecycle all pass.

No Python listener, modal dialog, mouse automation, menu command, schematic substitute, extrusion substitute, or compatibility fallback was used during this campaign.

## Scope and method

The pass exercised every grouped fast-native MCP family with representative variants. It was not exhaustive across every parameter, format, catalog item or model scale:

- `vw_status`
- `vw_read`
- `vw_catalog`
- `vw_apply`
- `vw_io`
- `vw_view`
- `vw_document`
- `vw_execute_operations`
- `vw_tool_safety`

Writes were grouped into atomic requests where supported. Exact UUID cleanup was used after each disposable fixture. Disruptive listener shutdown was intentionally excluded because it would require restarting Vectorworks and provides no additional CAD capability evidence.

## Capability matrix

| Area | Result | Evidence and limitation |
|---|---|---|
| Native readiness | Pass | Phase 4, safe main-context pump, non-modal native dispatch, capability revision 3. |
| Status/context | Pass | Health and compact drawing context returned through the grouped surface. |
| Document/layer/summary reads | Pass | Document info, layers, summary, selection read and object query returned. |
| Object query filter | Fail | `object_type="rect"` returned all six objects. Source audit also shows the grouped `layer` filter is ignored. |
| Classes catalog | Partial | Empty/small-document listing routed successfully, but query filtering and item pagination were not proven; class mutation is not reachable through the grouped production surface. |
| Symbols catalog | Partial | Empty listing routed successfully. Symbol creation is reachable through `vw_apply(object_type="symbol")`, but no symbol resource fixture existed, so insertion/readback was not tested. |
| Worksheets catalog | Partial | Empty listing routed successfully; worksheet item readback/paging and grouped writes were not tested. |
| Resources catalog | Partial | Empty listing routed successfully; resource item readback/paging and mutation were not tested. |
| Parametric schema catalog | Fail | The host maps `query` to native `query`, while native requires `plugin_name`. A missing query produces `plugin_name is required`; `Door`, `Window`, and `Space` queries fail. Generic parametric create also requires a descriptor fingerprint that this broken discovery path cannot supply. |
| Mixed atomic geometry | Pass | One request created a wall, polygon, text, linear dimension and rectangle, then translated, duplicated and set name/class properties. Six final objects matched expected types/names/bounds. |
| Mixed atomic latency | Pass | The eight-public-operation/nine-wire-operation transaction completed in about 165 ms. |
| Idempotent replay | Pass | Identical key and plan replayed without duplicates. Same key with a different plan was rejected before writes. |
| Primitive aliases | Partial | Line, oval, circle, arc and polyline created and were cleaned. Circle reads as `oval`; polyline reads as `polygon` although the capability descriptor claims a polyline node. |
| Transaction-local references | Partial | Create emits local refs and the property path used one, but delete cannot target `$operation_id`; it requires an external UUID/name/handle. Other local-ref mutation variants remain unproven. |
| Object transform | Pass, limited | Explicit-ref translation worked. Rotation, scale and pivot were not tested. |
| Object duplicate | Pass, limited | Explicit-ref duplication with offset worked and returned a distinct object. Other variants were not tested. |
| Object delete | Pass | Exact UUID deletion worked for cleanup. |
| Smart Space | Fail, P0 | A valid Space was created, then `AddAfterSwapObject` returned false. `UndoAndRemove` removed the Space while the temporary source polyline survived. The false return is ambiguous evidence of SDK-managed compound-object undo, not proof that creation itself failed. This blocks named/dimensioned room programs and the requested 2BHK smart floor plan. |
| True Slab | Fail, P0 | `SetComponentWidth` rejected the unstyled slab thickness and the temporary profile survived. Source audit identifies likely one-based component indexing as the defect; live retest must confirm the diagnosis. |
| True Roof | Fail, P0 | Native roof creation reached transaction registration but failed when `AddAfterSwapObject` returned false. No substitute geometry was created. |
| Door/Window smart objects | Unavailable, P0 | The public schema path could not resolve queries for `Door` or `Window`. This proves the discovery path is inadequate; it does not prove those built-in definitions are globally absent. Hosted placement and wall-host semantic verification therefore could not be exercised. |
| View get | Pass | Standard view, projection and render mode were read. |
| View set | Partial | Reapplying standard view/projection worked. Supplying the unchanged render mode failed because the setter's false return is treated as rejection without semantic readback. |
| PNG export | Pass | Native non-modal image export created a verified non-empty file. |
| View capture | Pass | Native capture created a verified non-empty PNG. |
| Grouped `vw_view(capture)` | Pass | The token-efficient grouped duplicate entry point created a verified PNG. |
| PDF export | Pass | Native non-modal PDF export created a verified non-empty file. |
| VWX export | Pass | Native Vectorworks export created a verified non-empty file. |
| Grouped `vw_document(export)` | Pass | The grouped document export entry point created a verified PNG. |
| DWG export | Pass | Native silent DWG export created a verified non-empty file. |
| DWG import | Partial | Import succeeded and added a layer plus 14 objects, but the receipt omitted created object/layer IDs and did not report or restore active-layer/visibility changes. |
| Overwrite protection | Pass | Existing output was rejected by default; exact replace confirmation succeeded. |
| Document save | Pass | Save to an explicit path succeeded. |
| Document open | Fail, P0 | The call reported a path mismatch, but still changed the active document. The observed active path became `C:\Program Files\Vectorworks 2024\drawing_v2024.vwx` instead of the requested workspace path. A failure response must never conceal a committed document switch. |
| New document | Unavailable | The public grouped option exists, but the bridge correctly returns `capability_unavailable`; no dialog or workaround was attempted. |
| Listener stop | Not run | Deliberately excluded because it is disruptive, closes the bridge, and forces a Vectorworks restart. It does not validate CAD capability. |
| Grouped safety metadata | Partial | Metadata exists, but mixed tools such as `vw_io`, `vw_document` and `vw_view` use coarse tool-level hints. Import/open/set variants need exact may-write/state-change/retry semantics. |

## Not tested or unavailable in this pass

These are intentionally not counted as passes:

- Box/rectangle and dimension aliases.
- Generic parametric creation, which is currently blocked by the broken schema/fingerprint contract.
- Symbol insertion with a real document resource.
- Worksheet cell read/write and class create/update/delete.
- Selection select/clear/delete through the grouped surface.
- Fill/pen color, line weight and opacity property variants.
- Rotation, scale and pivot transforms.
- Wall/Slab/Roof style-resource variants.
- JPG/TIFF export and non-DWG import; only DWG import is currently advertised by the native bridge.
- Catalog pagination/field projection and large-model summaries.
- A deliberate simple-object rollback, manual Undo/Redo, dirty-document confirmation flows and large 100–250-operation throughput.
- Hosted Door/Window, successful Space/Slab/Roof, `new_document`, listener stop and the final true-Space 2BHK acceptance plan.

## Performance observations

- Mixed atomic drawing: approximately 165 ms for nine wire operations.
- Exact idempotent replay: no duplicate writes; observed end-to-end latency was approximately 469 ms, dominated by queue wait rather than geometry work.
- Native image export: approximately 2.5 s on the first run.
- Native view capture: approximately 39 ms.
- Native PDF export: approximately 465 ms.
- Native VWX export: approximately 520 ms.
- Native DWG export: approximately 756 ms.
- Native DWG import: approximately 1.2 s.

This small atomic plan is fast. Large 100–250-operation floor plans and model-size scaling were not tested. The immediate work is correctness, truthful capability negotiation, and compact receipts—not adding alternative execution paths.

## Release blockers

### P0 — must be fixed before another install or 2BHK acceptance run

1. **Compound-object transaction ownership**
   Space and Roof cannot share the simple-object assumption that `AddAfterSwapObject(false)` always means creation failed. The current Space ordering logs a UUID, throws on registration, then lets the handler guard delete the Space before coordinator undo. Ownership must move to the coordinator before registration is attempted.

2. **Temporary geometry cleanup**
   Space and Slab source profiles must be tracked separately from final BIM objects and removed by exact UUID on commit. On failure, run SDK undo first, then delete and verify any surviving profile/final object by UUID. Never delete a guarded final object before `UndoAndRemove` has processed possible SDK-managed undo state.

3. **Transaction commit truth**
   The Boolean result of `EndUndoEvent()` is currently ignored in the native transaction path. A failed commit must never be reported as success; the bridge must check it and return an explicit commit-state result.

4. **Document lifecycle truth**
   `open_document` must either reject before changing state or return the actual committed active path/document identity. A returned error after a document switch is an unknown commit state.

5. **Capability truthfulness**
   Space, Slab, Roof, parametric discovery and other runtime-gated actions must not appear production-ready until their complete public path passes live semantic and undo/redo checks.

6. **Smart-room path**
   The room-program use case requires a real Space object with verified boundary, name, number, area telemetry and stable undo/redo. Rectangles are not an acceptable automatic fallback.

7. **Advertised Slab/Roof correctness**
   Fix Slab component indexing and profile ownership, and prove Roof registration/undo semantics. Until then, remove their production availability rather than leaving known-failing create kinds advertised.

### P1 — required for a complete, agent-friendly production surface

- Implement reliable built-in PIO discovery using universal SDK metadata, then dedicated Door/Window adapters with explicit wall refs and verified wall hosting.
- Repair object query filtering.
- Make render-mode setting semantic: verify requested state after the SDK call instead of treating an unchanged-state false return as failure.
- Permit safe transaction-local refs for duplicate/delete where the transaction semantics allow it.
- Return import mutation receipts containing document revision, created layers/object UUIDs, and view/layer-state changes.
- Make safety/idempotency metadata action-specific instead of assigning one hint to a mixed grouped tool.
- Preserve the original operation error when compensating rollback cleanup succeeds; attach rollback diagnostics and supersede it only when cleanup integrity also fails.
- Compound-object receipts must include final UUID, semantic node type, registration mode, profile-cleanup result and relevant BIM readback.
- Do not label native action failures broadly retryable. Post-dispatch lifecycle failures such as `open_document` must be non-retryable `unknown_commit_state` responses with actual document identity.
- Either enforce accepted I/O/document idempotency keys natively or remove the misleading fields/hints.
- Align runtime object-kind descriptors with actual readback aliases such as circle/oval and polyline/polygon.
- Expose the already-implemented resource, symbol, worksheet, class and selection variants through compact grouped actions, gated by exact native capabilities.

### P2 — efficiency and maintainability

- Generate host tool availability, native action availability and object-kind descriptors from one registry.
- Keep a stable small grouped MCP surface, but make the actions comprehensive. Grouping is for token efficiency, not capability removal.
- Return compact structured receipts by default and paginate large catalogs; do not dump full schemas or full drawings unless requested.
- Update the bundled work skill after the runtime contract stabilizes. Its current text still describes an older operation set and older fast-native surface.

## Integrated repair plan

The next cycle should change all related areas first, then perform exactly one rebuild/install and one consolidated live acceptance pass.

### 1. Make the capability contract authoritative

- Define availability per action and per variant: `available`, `unavailable` with reason, or `experimental` excluded from production.
- Derive grouped-tool routing, safety metadata, capability reports and native dispatch from the same registry.
- Do not advertise a create kind merely because a handler compiles.
- Make the host require the minimum native `capability_revision` for the contract it dispatches; reject stale or malformed revisions before writes.
- Advertise a runtime build fingerprint and verify it against the installed artifact before live acceptance.
- Keep the nine grouped tools, but expose complete actions within them rather than hiding capabilities or adding dozens of token-heavy top-level tools.

### 2. Introduce a transaction-owned compound-object lifecycle

- Maintain separate ledgers for final created objects and temporary SDK input geometry.
- Capture exact UUIDs immediately; handles are diagnostic only after reparenting/undo.
- Adopt Space/profile ownership into the transaction coordinator before attempting undo registration.
- Treat `AddAfterSwapObject(false)` as SDK-managed only for a specifically proven object family, never globally.
- On success, delete temporary profiles by UUID, verify absence, reverify final semantic objects, then commit.
- On failure, preserve the original error, run `UndoAndRemove`, remove exact-UUID survivors, verify zero residue, and report rollback-integrity failure if cleanup is incomplete.

### 3. Correct and verify each smart BIM implementation

- **Space:** verify parametric node, boundary using Net/Gross polygon geometry, name, room ID, positive area telemetry, and lifecycle.
- **Slab:** use zero-based component indexing, verify true slab node, thickness/elevation/style and profile cleanup.
- **Roof:** verify true roof-container node, footprint/edge count/pitch/eave settings and compound undo registration. Roof does not use a temporary source profile in the current implementation.
- **Door/Window:** discover universal plugin names and parameter schemas at runtime, require an exact wall ref, create insertion-enabled, and verify the resulting wall relationship. Unknown/localized fields are hard errors.
- No extrusion, primitive, schematic, menu, Python, mouse, or dialog fallback.

### 4. Repair document and I/O state contracts

- Add expected document ID/revision guards to lifecycle commands.
- Canonicalize and compare requested/actual paths before reporting success.
- If the SDK switches documents despite a postcondition mismatch, return a structured `unknown_commit_state` containing actual document identity and path.
- Give import a mutation receipt and preserve or explicitly report active layer, visibility and view changes.
- Keep current staged-file/overwrite-confirmation protections.

### 5. Finish the compact full-capability MCP surface

- `vw_read`: correct filters and exact-ref lookup.
- `vw_catalog`: paginated classes, symbols, worksheets, resources and universal parametric schemas.
- `vw_apply`: one canonical atomic union for geometry, BIM smart objects, properties, transforms, duplicate/delete, classes/layers and worksheet writes where undo-safe.
- `vw_io`, `vw_view`, `vw_document`: variant-specific capability and safety contracts.
- Dedicated smart-object inputs may be schemas within `vw_apply`; they must compile to the same transaction engine, not alternative write paths.

### 6. One final acceptance gate after one rebuild/install

Run only these high-value checks in one disposable blank document:

1. Verify the installed runtime artifact hash exactly matches the reported build, then check native readiness and negotiated capability truth.
2. Compact read/catalog/filter matrix.
3. One mixed atomic transaction covering established geometry and mutation operations.
4. One deliberate compound-object rollback proving zero final objects and zero temporary profiles.
5. One combined Space/Slab/Roof commit, Undo once and Redo once: Undo leaves no final objects/profiles; Redo restores only the three semantic BIM objects and no profiles.
6. Verify true Space, Slab and Roof node/readback semantics after Redo.
7. Hosted Door and Window only if runtime discovery and wall-host verification advertise them.
8. Resource/symbol/worksheet/class grouped actions.
9. View no-op/set and native I/O overwrite/import receipts.
10. Run document save/open last and in isolation, because it can replace the active document context.
11. One complete 2BHK atomic plan using true Spaces plus walls/labels/dimensions; verify room names, dimensions, object kinds and zero temporary geometry.

If any advertised capability fails this gate, remove its production availability rather than substituting another geometry type or execution path.

## Static checks versus live evidence

The native SDK project built with zero warnings/errors and the focused native contract suite previously passed 50 tests. The live campaign still found multiple release blockers. Static/source-string tests must therefore be reduced to contract-shape checks, while the small live gate above becomes the authority for semantic object creation, lifecycle and rollback.

## Cleanup state

- All disposable geometry in the final active test document was removed by exact UUID; that document's observed final summary contained zero objects. This does not prove the unexpectedly opened document and the original document were both clean.
- Smoke artifacts remain under `.tmp/mcp-smoke-20260818`. The directory contains an active Vectorworks `.lck` file, so it was intentionally not deleted while Vectorworks is open. Remove it only after the related document is closed and the lock disappears.
- The failed document-open test observed `C:\Program Files\Vectorworks 2024\drawing_v2024.vwx` as the active path. Do not delete or overwrite that file while Vectorworks has it open; close/discard the document first, then inspect it separately.
