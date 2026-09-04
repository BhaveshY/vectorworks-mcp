# Revision-5 documentation workflow — independent review

Reviewed `604c371` (`Add revision 5 documentation workflow`) read-only. Scope:
host routing/normalisation, native binding and transaction code, capability/build
registration, workflow/acceptance documentation, and contract tests. No live
Vectorworks or SDK build was available, so SDK execution and undo semantics are
not claimed as verified.

## Findings

### [P1] Native document bindings cannot be passed through unchanged, and automatic documentation reads fail

`DocumentBindingJson` includes `file_name` in every native binding
([DocumentationHandlers.cpp:975-983](../../native_bridge/src/DocumentationHandlers.cpp)),
but the host's target-binding allowlist omits that field and rejects every
unknown key ([server.py:4343-4354](../../server.py)). This breaks the documented
call contract to pass `vw_read(action="document").data.binding` unchanged
([DOCUMENTATION_WORKFLOW.md:21-24](../../DOCUMENTATION_WORKFLOW.md)). It also
breaks the no-binding convenience path: `vw_read(sheet_layers)` first obtains
the native `binding` and feeds it straight into `_normalise_target_binding`
([server.py:7036-7044](../../server.py)), which will reject the real native
payload's `file_name` before `get_sheet_layers` is sent.

Impact: all automatic revision-5 sheet/viewports/annotation reads fail against
the actual bridge; callers following the documented unchanged-binding path also
fail. This was hidden because the test fixture omits `file_name`
([tests/test_documentation_workflow_contract.py:13-21](../../tests/test_documentation_workflow_contract.py))
and its auto-bind stub returns that reduced fixture
([tests/test_documentation_workflow_contract.py:47-66](../../tests/test_documentation_workflow_contract.py)).

Recommended fix: make the wire/boundary schema consistent. Either omit
`file_name` from native `DocumentBindingJson`, or allow it as a recognised
read-only binding field (then normalise it away before equality/wire checks if
it is intentionally non-binding). Add an end-to-end host contract test using
the exact native binding shape, for both automatic and explicitly supplied
bindings.

### [P2] The native write handler does not itself require the dirty-state guard

The native parser sets `ExpectedTargetBinding::hasDirty` only if the caller
sends `expected_dirty` ([VectorworksMCPBridge.cpp:1374-1388](../../native_bridge/src/VectorworksMCPBridge.cpp)).
`ValidateTargetBinding` compares dirty state only when that optional flag is
true ([DocumentationHandlers.cpp:1013-1015](../../native_bridge/src/DocumentationHandlers.cpp)),
and `ApplyOperations` invokes it without first requiring `hasDirty`
([DocumentationHandlers.cpp:1116-1142](../../native_bridge/src/DocumentationHandlers.cpp)).
Thus a direct/native-protocol `apply_documentation_operations` request can
write with a fully populated file/fingerprint/generation/session/layer binding
but no dirty-state check, contrary to the stated native mutation invariant.

The MCP host currently supplies `expected_dirty` for documentation plans
([server.py:5047-5053](../../server.py), [server.py:5151-5165](../../server.py)),
so normal host traffic is protected. However, the production mutation boundary
should be fail-closed on its own; this also prevents a future adapter or raw
authenticated bridge caller from silently weakening the binding.

Recommended fix: reject documentation writes in `ApplyOperations` when
`expected.hasDirty` is false before the idempotency-cache lookup and before any
transaction is created. Keep reads optional if desired. Add a native-handler
contract/smoke case omitting `expected_dirty` and assert no mutation/undo event.

## Residual risks and test gaps

- The new Python tests exercise host normalisation and source-token presence,
  but not the actual native JSON emitted by `DocumentBindingJson`; the P1 gap
  demonstrates why source-level coverage is insufficient.
- No SDK-backed compile, installed-bridge smoke, transaction rollback failure,
  manual undo, save/restart persistence, or revision-5 live acceptance evidence
  was available in this review. The workflow documentation correctly labels
  those as separate production gates ([DOCUMENTATION_WORKFLOW.md:160-167](../../DOCUMENTATION_WORKFLOW.md)).
- Native writes validate the binding before entering the transaction, but do
  not validate the expected binding again after the operation loop; they only
  read a final binding for the receipt ([DocumentationHandlers.cpp:1142-1148](../../native_bridge/src/DocumentationHandlers.cpp),
  [DocumentationHandlers.cpp:1391-1425](../../native_bridge/src/DocumentationHandlers.cpp)).
  Main-context serialization may make this safe in practice, but a live test
  should prove no document/layer switch can interleave with a long transaction
  and should cover the claimed fail-closed behavior if it can.
