# ARGS = {"ops": [{"op": ..., "targets": [...], ...}]}   (op engine: _ops.py)
done = []
for item in ARGS["ops"]:
    run_op(item, done)

result = {"applied": len(done), "receipts": done}
if IMPORTED:
    result["imported_resources"] = IMPORTED
if WARNINGS:
    result["warnings"] = WARNINGS
