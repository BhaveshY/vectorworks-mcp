"""Real Codex end-to-end runs against the installed Vectorworks MCP.

Each scenario runs `codex exec` (uses your OpenAI quota) with a prompt that only
touches documents the agent itself creates, then summarises which MCP tools were
called, which failed, and the model's final answer.

    py -3.12 scripts\\codex_e2e.py                 # all scenarios
    py -3.12 scripts\\codex_e2e.py luna_room       # one scenario
    py -3.12 scripts\\codex_e2e.py --list

Requirements: Codex CLI on PATH (or CODEX_EXE), Vectorworks running with the
bridge, and at least one other document open so closing the test document does
not leave Vectorworks without documents.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "test-artifacts" / "codex-e2e"
WORK = pathlib.Path(os.environ.get("TEMP", ".")) / "vw-codex-e2e"

SAFETY = (
    "Rules: use ONLY the vectorworks MCP tools (no shell commands, no computer use, no GUI automation). "
    "First create a NEW blank Vectorworks document with vw_document and work only in that document; "
    "never modify, save or close any other open document. At the end close the document you created "
    "WITHOUT saving (discard changes). "
)

SCENARIOS: dict[str, tuple[str, str]] = {
    "luna_room": ("gpt-5.6-luna", SAFETY +
        "In the new document draw a 6 m x 4 m rectangular room with 200 mm thick, 2700 mm high walls "
        "(lower-left inner corner at 0,0). Put a 900 mm wide door in the south wall and a 1200 mm wide window "
        "in the north wall, and add a Space named 'Office' filling the room. Then capture the plan view, look "
        "at the image, and report: the number of walls, doors, windows and spaces you created (read them back "
        "with vw_read, not from memory) and one sentence describing the captured image."),
    "sol_multidoc": ("gpt-6-astra", SAFETY +
        "Create TWO new blank documents A and B. In A: create design layers 'Ground' and 'First' (elevation "
        "3000 mm), a class 'Walls-Ext' with red pen colour, and a 10 x 8 m outline of 4 walls in that class on "
        "'Ground'. In B: draw a circle of radius 2 m and a text 'Plan B'. Switch back to A explicitly by "
        "document, rotate all of A's walls by 90 degrees around 0,0, then capture A in a right isometric view "
        "and B in top view. Export A's current view to PDF in the folder %TEMP%\\vw-codex-e2e as A.pdf. Report "
        "object counts per document from vw_read, the PDF path and size, and describe both images. Close both "
        "documents you created without saving."),
    "luna_script": ("gpt-5.6-luna", SAFETY +
        "In the new document use vw_create to draw a 5 x 5 m square of walls, then use vw_script (python) to "
        "count all walls in the document and return the count as `result`. Then run a script that creates a "
        "rectangle and then raises an error, and verify with vw_read that the rectangle was rolled back. "
        "Report both results."),
    "luna_offline": ("gpt-5.6-luna",
        "Use only the vectorworks MCP tools. Check whether Vectorworks is connected and tell me in one sentence "
        "what the user must do if it is not. Do not retry more than once."),
}


def codex_exe() -> str:
    exe = os.environ.get("CODEX_EXE") or shutil.which("codex")
    if not exe:
        cand = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
        exe = str(cand) if cand.exists() else ""
    if not exe:
        sys.exit("Codex CLI not found; set CODEX_EXE")
    return exe


def run(name: str, model: str, prompt: str) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    log = OUT / f"{name}.jsonl"
    cmd = [codex_exe(), "exec", "-m", model, "--skip-git-repo-check", "--ephemeral", "--json",
           "-C", str(WORK), "-c", 'model_reasoning_effort="medium"', prompt]
    started = time.time()
    with open(log, "w", encoding="utf-8") as handle:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, timeout=1800)
    calls: list[str] = []
    errors: list[str] = []
    final = ""
    for line in open(log, encoding="utf-8", errors="replace"):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") or {}
        kind = item.get("type")
        if kind == "mcp_tool_call" and event.get("type") == "item.completed":
            calls.append(f"{item.get('tool')}:{item.get('status')}")
            result = item.get("result") or {}
            if item.get("status") != "completed" or result.get("is_error") or item.get("error"):
                errors.append(json.dumps({k: item.get(k) for k in ("tool", "arguments", "error", "result")})[:900])
        elif kind == "command_execution" and event.get("type") == "item.completed":
            calls.append("SHELL:" + str(item.get("command"))[:80])
        elif kind == "agent_message":
            final = item.get("text", "")
    return {"name": name, "model": model, "exit": proc.returncode, "seconds": round(time.time() - started),
            "tool_calls": calls, "errors": errors, "final": final[-3000:], "log": str(log)}


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for key, (model, _) in SCENARIOS.items():
            print(f"{key:14} {model}")
        return 0
    names = [a for a in argv if not a.startswith("-")] or list(SCENARIOS)
    results = [run(n, *SCENARIOS[n]) for n in names]
    (OUT / "summary.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    for r in results:
        print(f"== {r['name']} ({r['model']}) exit={r['exit']} {r['seconds']}s calls={len(r['tool_calls'])} "
              f"errors={len(r['errors'])}")
        print("   ", " ".join(r["tool_calls"]))
        for err in r["errors"]:
            print("   ERR", err)
        print("   FINAL:", r["final"][:2000].replace("\n", " | "))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
