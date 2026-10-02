"""Lint every vs.* call the host ships against the SDK stub signatures.

Checks (with vectorworks_mcp/vs_lint.py, the same lint vw_script runs):
  * every scripts_lib snippet as it is sent (prelude + body, via scripts_lib.load);
  * Python literals embedded in host code that are sent through run_script
    (string constants in vectorworks_mcp/**/*.py that call vs.*);
  * vw_script examples in the skill / guide (code="..." arguments);
  * that data/vs_signatures.json is current (scripts/gen-vs-signatures.py --check).

Python generated inside the native bridge (native_bridge/src/*.cpp) is built by
C++ string concatenation and audited by hand: native_bridge/VS_CALL_AUDIT.md.

Stdlib only. Exit code 0 = clean.   Usage: py -3.12 scripts/lint-vs-snippets.py
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vectorworks_mcp import scripts_lib, vs_lint  # noqa: E402


def lint_text(label: str, code: str, failures: list[str]) -> int:
    issues = vs_lint.lint(code)
    for item in issues:
        failures.append(f"{label}:{item.get('line', 0)}: {item['problem']}"
                        + (f"  [{item['signature']}]" if item.get("signature") else ""))
    return len(re.findall(r"\bvs\.[A-Za-z_]\w*\s*\(", code))


def embedded_strings(path: Path):
    """String constants (incl. %-format templates) in host code that look like vs.* scripts."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(r"\bvs\.[A-Z]\w*\(", node.value):
            text = node.value
            # %r / %s placeholders -> a harmless literal so the template parses.
            text = re.sub(r"%[rsd]", "[]", text)
            try:
                ast.parse(text)
            except SyntaxError:
                continue  # prose / docstrings mentioning vs.* calls
            yield node.lineno, text


def doc_examples(path: Path):
    text = path.read_text(encoding="utf-8")
    for match in re.finditer(r'vw_script\((?:[^()"]|"(?:[^"\\]|\\.)*")*?code="((?:[^"\\]|\\.)*)"', text):
        try:
            code = ast.literal_eval('"' + match.group(1) + '"')
        except (SyntaxError, ValueError):
            continue
        yield text.count("\n", 0, match.start()) + 1, code


def main() -> int:
    failures: list[str] = []
    calls = 0
    for name in scripts_lib.SNIPPETS:
        calls += lint_text(f"scripts_lib/{name}.py (+prelude)", scripts_lib.build(name, {}), failures)
    host_dir = ROOT / "vectorworks_mcp"
    for path in sorted(host_dir.rglob("*.py")):
        if "scripts_lib" in path.parts or path.name == "vs_lint.py" or "testing" in path.parts:
            continue
        for line, code in embedded_strings(path):
            calls += lint_text(f"{path.relative_to(ROOT)}:{line} (embedded)", code, failures)
    for doc in [ROOT / "plugins" / "vectorworks" / "skills" / "vectorworks" / "SKILL.md", host_dir / "guide.md"]:
        if doc.is_file():
            for line, code in doc_examples(doc):
                calls += lint_text(f"{doc.relative_to(ROOT)}:{line} (example)", code, failures)
    stale = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen-vs-signatures.py"), "--check"],
                           capture_output=True, text=True)
    if stale.returncode != 0:
        failures.append("vectorworks_mcp/data/vs_signatures.json is stale: " + stale.stderr.strip())
    for line in failures:
        print("FAIL " + line)
    print(f"{'FAILED' if failures else 'ok'}: {calls} vs.* calls checked, {len(failures)} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
