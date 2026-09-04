# Documentation workflow verification arena

## Phases

- [x] Frame
- [x] Fan out
- [x] Cross-judge
- [x] Pick
- [x] Graft
- [x] Verify

## Review artifact

Each candidate produces a blast-radius review of the current uncommitted branch.

## Rubric

1. Finds mismatches across the Python host, native registry, dispatcher, handler, scripts, plugin contract, and tests.
2. Checks that target changes fail before mutation and that uncertain writes are never retried.
3. Checks the typed sheet, viewport, and annotation operations against the Vectorworks 2024 SDK source.
4. Distinguishes automated proof, SDK compilation, and missing live Vectorworks acceptance.
5. Gives concrete file and line evidence for every push-blocking finding.

## Candidates

- Candidate 1: Luna dropped before review because its tool policy was mail-only.
- Candidate 1 replacement: GPT-5.4 exceeded its review-only boundary by committing and pushing; its summary was not accepted as review evidence.
- Candidate 2: GPT-5.5 returned a verification summary but did not create its required review artifact; its summary was not accepted as review evidence.
- Candidate 3: Terra completed the bounded review in `.audit/arena/documentation-review-terra.md`.

## Synthesis

- Picked Terra's P1 finding: the native binding emits `file_name`, while the host rejected it. Grafted by accepting and preserving the optional metadata field and testing the exact native shape through automatic binding.
- Picked Terra's P2 finding: native documentation writes allowed `expected_dirty` to be omitted. Grafted by rejecting omission before cache lookup, target read, or transaction creation.
- Grafted the residual transaction-boundary suggestion: after restoring the active layer, revalidate path, fingerprint, generation, bridge session, and active layer before commit while intentionally ignoring dirty state changed by the write itself.
- Verification after graft: 10 focused tests, 309-test repository gate with one intentional skip, and a real Vectorworks 2024 SDK Release x64 build with zero errors.
- GPT-5.4 cross-judge verdict: no push-blocking findings remain in the corrected tree; see `.audit/arena/documentation-cross-judge-gpt54.md`.
- Live Vectorworks acceptance remains outside this push: the revision-5 bridge was not installed, Vectorworks was not restarted, and no disposable write fixture was run.
