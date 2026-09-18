"""Read-only comparison of native page projection versus legacy prefix reads.

Run with the repo virtualenv while a suitable disposable drawing is open.
The drawing needs at least offset + limit objects. No objects are changed.
"""

import argparse
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def run(offset, limit, samples):
    def request(action, params):
        start = time.perf_counter()
        raw = server._send(action, params, require_cad_safe=True)
        result = json.loads(raw)
        return result, (time.perf_counter() - start) * 1000, len(raw.encode("utf-8"))

    before, _, _ = request("get_document_info", {})
    timings = {"legacy_prefix": [], "native_page": []}
    sizes = {}
    for index in range(samples + 1):
        legacy, old_ms, old_bytes = request("get_objects", {"limit": offset + limit + 1})
        page, new_ms, new_bytes = request("query_objects", {
            "mode": "objects", "offset": offset, "limit": limit,
            "field_count": 2, "field_1": "uuid", "field_2": "name",
        })
        expected = [{key: row[key] for key in ("uuid", "name") if key in row}
                    for row in legacy[offset:offset + limit]]
        if len(expected) != limit or page["items"] != expected:
            raise RuntimeError("Drawing is too small, changed, or query results differ")
        if index:
            timings["legacy_prefix"].append(old_ms)
            timings["native_page"].append(new_ms)
        sizes = {"legacy_prefix": old_bytes, "native_page": new_bytes}
    after, _, _ = request("get_document_info", {})
    if before != after:
        raise RuntimeError("Document state changed during benchmark")
    return {"offset": offset, "limit": limit, "samples": samples,
            "scope": "native TCP request, JSON decode, and CAD preflight; warm connection",
            "equivalent_results": True,
            "measurements": {name: {
                "median_ms": round(statistics.median(values), 3),
                "p95_ms": round(sorted(values)[min(len(values) - 1, int(len(values) * .95))], 3),
                "response_bytes": sizes[name],
            } for name, values in timings.items()}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offset", type=int, default=800)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--samples", type=int, default=30)
    args = parser.parse_args()
    if args.offset < 0 or args.limit < 1 or args.offset + args.limit + 1 > 1000 or args.samples < 2:
        parser.error("Require offset >= 0, limit >= 1, offset + limit + 1 <= 1000, samples >= 2")
    try:
        print(json.dumps(run(args.offset, args.limit, args.samples), indent=2))
    finally:
        server._close()
