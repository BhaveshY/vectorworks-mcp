"""vw_script: run Python (vs.*) or VectorScript in-process as one undo step."""

from __future__ import annotations

from .. import coerce as C
from ..errors import VWError
from ..host import doc_brief, get_host


def handle(args: dict, ctx, warnings: list) -> dict:
    a = C.lower_keys(args)
    code = C.text(a.get("code") or a.get("script") or a.get("source"), "code", required=True, max_len=1_000_000)
    language = C.choice(a.get("language"), "language", ["python", "vectorscript"],
                        {"py": "python", "vs": "vectorscript", "pascal": "vectorscript"}, default="python")
    background = C.boolean(a.get("background", a.get("async", False)), "background")
    if language == "python" and C.boolean(a.get("lint", True), "lint"):
        # Imported lazily: the signature table is loaded on the first vw_script
        # call, never at server start-up.
        from .. import vs_lint
        issues = vs_lint.lint(code)
        errors = [i for i in issues if i.get("severity") == "error"]
        if errors:
            raise VWError("script_lint_failed", "vs.* call check failed: " + vs_lint.summary(errors),
                          retryable=True, commit_state="not_started", next_step=vs_lint.next_step(errors),
                          details={"issues": issues})
        for issue in issues:
            warnings.append(f"line {issue.get('line')}: {issue.get('function')} {issue.get('problem')}"[:300])
    host = get_host()
    target = host.session.target(a.get("document"), write=True)
    if not target.inst.supports("run_script"):
        raise host.bridge.missing_action(target.inst, "run_script")
    params = {"language": language, "code": code, "undo_label": str(a.get("undo_label") or "MCP script"),
              "transaction": True}
    reply = host.call(target, "run_script", params, kind="long" if background else "write", ctx=ctx)
    if not isinstance(reply, dict):
        return {"ok": True, "result": reply}
    if reply.get("ok") is False or reply.get("error"):
        raise VWError("script_error", str(reply.get("error") or "script failed")[:2000],
                      commit_state="rolled_back" if reply.get("rolled_back", True) else "unknown",
                      details={k: str(reply.get(k))[-4000:] for k in ("stdout", "stderr") if reply.get(k)})
    out = {"ok": True, "result": reply.get("result"), "rolled_back": bool(reply.get("rolled_back", False))}
    for key in ("stdout", "stderr"):
        if reply.get(key):
            out[key] = str(reply[key])[-8000:]
    if reply.get("duration_ms") is not None:
        out["duration_ms"] = reply["duration_ms"]
    if target.document:
        out["document"] = doc_brief(target.document)
    return out
