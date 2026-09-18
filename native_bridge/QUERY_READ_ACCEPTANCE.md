# Native query acceptance — 2026-09-18

Verified in Vectorworks Architect 2024 on Windows with the SDK Release bridge
SHA-256 `109688CECD756A5E0EB5CCE7C422DD255607BF1393D6D4EEE846E306B16EBB22`.
The bridge advertised `text_content`, `paged_object_reads`, and
`bound_apply_operations` under capability fingerprint `7d1d14645f09f243`.

## Live checks

- A disposable saved drawing began with three Unicode/multiline text objects
  and one rectangle. A mismatched document fingerprint rejected a bound create
  without changing the object count.
- Created 1,100 additional text objects in bound atomic batches. Replaying the
  first identical bound plan returned its receipt without duplicate objects,
  including when the original clean document had become dirty.
- Transaction lookup returned a committed historical receipt for that key.
  A never-submitted key returned unknown with `retry_safe=false`.
- Read all 1,104 objects through 128-item pages with no duplicate UUIDs and
  exact expected Unicode text for every added object.
- For each original object, requesting each field separately matched its
  corresponding full-record value. Actual previously GUI-edited text remained
  readable after the bridge update.
- Deleted only the 1,100 added UUIDs in atomic batches. Full records for the
  original four objects matched the baseline after cleanup; saved the fixture.
- Phase-2 native read/write smoke passed. Phase-0 stop confirmed port release.

## Query benchmark

`scripts/benchmark-native-queries.py` compares the legacy prefix read with the
new native page on the same unchanged 1,104-object drawing. It checks equivalent
UUID/name results on every iteration, discards one warmup, and measures 30
samples. Offset 800, limit 100; added text objects contain about 500 characters.
Measurements include native TCP requests, JSON decode, and CAD preflight on a
warm connection, not an LLM or end-to-end agent task.

| Read path | Median | p95 | Response bytes |
|---|---:|---:|---:|
| Legacy prefix then project/slice | 43.636 ms | 94.336 ms | 938,311 |
| Native page and projection | 1.709 ms | 11.715 ms | 11,860 |

This workload was approximately 25.5 times faster at the median with 98.7%
fewer response bytes. It is not a claim that every operation is faster by this
factor. A separate 30-sample MCP stdio exact-name text read had median 5.317 ms
and p95 11.151 ms, with one request attempt per read.

## Limits

Offset paging still traverses earlier matches; it avoids serializing them.
Pages are live rather than snapshot-isolated and should be collected without
concurrent drawing edits. Binding checks do not provide a per-edit revision
counter. Transaction receipts are historical, bounded to 128 retained general
transactions, and cleared on bridge restart. They do not verify current state
after Undo. Missing receipts never authorize automatic write retry.

All existing tools and full-record reads remain available. No new protocol
version, Python fallback, GUI fallback, persistent receipt ledger, or new BIM
object type is introduced by this change.

Repository verification: 327 Python tests ran successfully with one conditional
skip; no-Vectorworks verification and compiled C++ scaffold smoke passed; SDK
build completed with zero warnings and zero errors.
