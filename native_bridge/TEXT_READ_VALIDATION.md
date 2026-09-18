# Native text read validation

Validated on 2026-09-18 with Vectorworks Architect 2024 (29.0.8), Windows,
and the fast-native MCP profile. All writes were confined to a disposable
drawing. The source drawing's SHA-256 remained unchanged.

## Installed artifact

- SDK Release x64 build: zero warnings and errors.
- Installed `ObjectExample.vlb` matched the built SHA-256:
  `6F9C07D580D91ECDC02D865CB2F7ECDA57A49237BC9A1FDF5DE71E0C83B2ABBB`.
- Native phase 4, capability revision 5, `object_read_features: ["text_content"]`.
- Native health, phase-0 stop/port release, phase-1/2 read/write smoke,
  and phase-4 atomic create/delete passed.
- Phase-2 dimension fixtures now omit unsupported names and use a verified
  UUID for native transactional cleanup. This smoke requires `apply_operations`,
  consistent with the production phase-4 requirement.

## Text acceptance

- Exact German, Japanese, Greek and emoji text was read back through MCP.
- Carriage returns, quotes, tabs, backslashes and a 2,100-character text passed.
- Two-page reads, named queries and mixed text/non-text results passed.
- Reads preserved selection, view, document binding and dirty state.
- A background Cua GUI edit inserted `LIVE_EDIT_20260918`; the next MCP read
  returned the changed text with the same object UUID. No creation-data cache
  or OCR was used. Background Ctrl+A did not select all in this editor, so the
  verification used the observed inserted marker rather than assuming replacement.
- The currently connected Codex MCP also returned native text content.
- Test objects left by failed smoke development attempts were deleted by exact
  UUID, with subsequent readback confirming cleanup; the fixture was saved.

## Latency

Thirty warm named text reads through a persistent candidate MCP stdio session,
after one warm-up read, on the small fixture:

| Measurement | Median | 95th percentile | Maximum |
| --- | ---: | ---: | ---: |
| MCP stdio round trip | 1.953 ms | 3.289 ms | 3.403 ms |
| Server-reported total | 0.393 ms | 0.812 ms | 0.828 ms |

Every measured read used one native request attempt. These timings exclude
server startup and model/tool orchestration; they are not a large-project
benchmark or a before/after speedup claim. No Python dialog or GUI read fallback
was enabled. Unsupported capabilities continue to fail explicitly.

A second fresh session against the installed host also passed: 30 reads,
2.403 ms median and 3.502 ms 95th-percentile MCP round trip. The installed host
files were backed up and their hashes verified against the candidate.

Rich-text formatting, OCR and recursive symbol/plug-in text extraction are
outside this change. Empty-string behavior is covered by the protocol tests;
the native create helper rejects empty text fixtures.
