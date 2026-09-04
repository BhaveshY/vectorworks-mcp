# Vectorworks Documentation Workflow

This workflow adds native sheet-layer, viewport, and viewport-annotation
lifecycle support without adding top-level MCP tools. It requires the non-modal
SDK bridge at capability revision 5 and does not use the Python listener,
arbitrary scripts, or GUI automation.

## Exact target binding

Documentation reads and writes bind to the active saved document with all of:

- canonical saved file path;
- document fingerprint (saved path plus Vectorworks drawing-header UUID and
  in-memory drawing-header identity);
- active-document generation, incremented when the bridge observes a document
  switch;
- bridge process session ID;
- active layer UUID and name;
- dirty state for writes.

Use `vw_read(action="document")` to obtain `data.binding`. Pass the unchanged
object as `target_binding` to documentation reads and writes. Unsaved files,
document switches, Save As, bridge restarts, or a dirty-state change stop the
operation before mutation. A document generation is an active-document switch
guard, not a content revision counter. The same already-dirty document must not
be manually edited during a multi-call review; Vectorworks 2024 exposes no
reliable public content-revision counter for this path.

## Read lifecycle

All collection reads are native, bounded, and paged:

```json
{"action":"sheet_layers","limit":100,"cursor":"","target_binding":{"file_path":"C:\\Models\\A.vwx","document_fingerprint":"vw-doc-...","document_generation":3,"bridge_session_id":"vw-session-...","active_layer_uuid":"...","active_layer_name":"Design Layer-1","dirty":false}}
```

- `sheet_layers` returns sheet UUID/name/title/description, DPI, sheet size in
  millimetres, visibility, and exact viewport count.
- `viewports` additionally requires `sheet_layer_uuid="uuid:..."` and returns
  scale, crop identity/bounds, projection/view/render settings, placement in
  millimetres, dirty state, and complete design-layer/class visibility arrays.
- `viewport_annotations` additionally requires `viewport_uuid="uuid:..."`
  and returns each native annotation child, type, class, name, bounds, text,
  and marker metadata when applicable.

Follow `data.page.next_cursor` until it is `null`. Never infer a total from the
length of one page; `data.page.total` is the native total for that exact parent.

## Atomic create, update, and delete

Use `vw_apply` or `vw_execute_operations`. Both route a documentation-only plan
to one native `apply_documentation_operations` transaction. They do not
decompose or fall back.

Supported public operation types are:

- `create_sheet_layer`, `update_sheet_layer`, `delete_sheet_layer`;
- `create_viewport`, `update_viewport`, `delete_viewport`;
- `create_viewport_annotation`, `update_viewport_annotation`,
  `delete_viewport_annotation`.

Documentation creates require a unique `operation_id`; later operations in the
same plan may use `$operation_id` for their sheet or viewport parent. Existing
objects require exact `uuid:` references. A viewport create specifies its sheet
parent, source design-layer UUIDs, source class names, explicit visibility,
scale, projection, view/render modes, placement, and optional crop. Annotation
kinds are `text`, `dimension`, `marker`, and `redline`; every annotation has an
explicit class and exact sheet/viewport parent.

Destructive confirmations are exact:

- `DELETE_SHEET_LAYER_AND_CONTENTS`;
- `DELETE_VIEWPORT_AND_ANNOTATIONS`;
- `DELETE_VIEWPORT_ANNOTATION`.

The native handler verifies real SDK node types and parentage, performs semantic
readback, registers created objects with the shared undo transaction, restores
the starting active layer, and commits once. If a response is lost after send,
do not retry with a new key; inspect the target through bound reads first.
The live fixture also passes the same binding to `vw_io` PDF export and image
capture; the native bridge validates it before and after writing evidence. PDF
export covers the fixture sheet. Image capture is explicitly recorded as the
active view and is not presented as proof that the fixture sheet was active.

## Review every sheet safely

`scripts/review-all-sheets.py` is a read-only, resumable review runner. It:

1. preflights the revision-5 manifest;
2. records the exact target binding and starting view state;
3. pages every sheet, then every viewport and annotation under that sheet;
4. writes an atomic checkpoint after each completed sheet;
5. rechecks document binding and view state after each sheet and at completion;
6. emits a JSON report with `mutations_attempted: 0` and `state_unchanged`.

Example:

```powershell
py -3 .\scripts\review-all-sheets.py `
  --output C:\Temp\vw-review.json `
  --checkpoint C:\Temp\vw-review.checkpoint.json
```

Add `--resume` only with that checkpoint. Resume fails if the saved path,
fingerprint, active-document generation, bridge session, dirty state, or
starting view differs. The runner inspects sheet contents through native data;
it never activates sheets, so there is no UI state to unwind.

## External reference evidence

Online values are agent research, not Vectorworks API facts. Keep them separate
from model observations and pass a JSON array through `--external-evidence`.
Each entry must contain exactly:

```json
{
  "check_id": "door-rating-01",
  "extracted_text": "90 min",
  "source": {
    "kind": "viewport_annotation",
    "object_uuid": "annotation-uuid",
    "sheet_layer_uuid": "sheet-uuid",
    "viewport_uuid": "viewport-uuid"
  },
  "authoritative_url": "https://authority.example/requirement",
  "observed_value": "90 min",
  "expected_value": "90 min",
  "observed_at": "2026-09-04T10:00:00Z",
  "confidence": 0.98
}
```

`source.kind` is `sheet_layer`, `viewport`, or `viewport_annotation`; a sheet
uses `viewport_uuid: null`. The review runner validates HTTPS provenance,
timezone-bearing timestamp, confidence, exact value comparison, and whether the
cited typed identity tuple was actually observed. With no evidence input,
`evidence_checks_passed` is `null`, not a passing claim. It does not browse or
convert untrusted web text into instructions.

## Parallel review boundary

Vectorworks' official help documents multiple files as tabs inside one
application (up to eight), with one active document, and warns when a file is
already open elsewhere. It does not document an isolation contract for two
same-version application processes. See [Managing multiple documents](https://app-help.vectorworks.net/2025/eng/VW2025_Guide/Start/Managing_multiple_documents.htm)
and [Opening a file](https://app-help.vectorworks.net/2026/eng/VW2026_Guide/Start/Opening_a_file.htm).

Parallel Vectorworks processes are therefore not assumed safe. A separate process is a
candidate only when it has its own saved file copy, native bridge process,
loopback port, auth-token file, and bridge session ID. Before use, prove with a
disposable file that each process answers on only its assigned port, reports a
different bridge session ID and document fingerprint, and that stopping one
does not affect the other. Without that live evidence, review serially in the
single active Vectorworks process. Do not use Python dialog listeners for
parallelism. Read-only review does not switch sheets or views, but it is a
cooperative snapshot: do not edit the same active document until the run ends.

## Acceptance status

Automated host tests and no-Vectorworks checks prove schemas, validation,
routing, and source-level SDK contracts. They do not prove a compiled bridge in
Vectorworks. Production acceptance additionally requires an SDK build, install,
restart, a disposable-document create/read/update/delete smoke, manual undo
confirmation, persistence after restart, and verified PDF/image outputs. Record
the Vectorworks build, bridge commit and artifact hash, output hashes, exact
commands, and any missing evidence. Until those live checks pass, describe the
feature as implemented but live-unverified.
