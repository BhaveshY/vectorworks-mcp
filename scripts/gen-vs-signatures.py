"""Regenerate vectorworks_mcp/data/vs_signatures.json from the SDK `vs.py` stubs.

The Vectorworks SDK ships an IDE help stub (SDKLib/Include/vs.py) in which every
`vs.*` function is declared with one `# TYPE - description` comment per Python
parameter plus a docstring holding the `Python:` and `VectorScript:` forms.
This script turns that into a compact table used by the host-side `vw_script`
lint (vectorworks_mcp/vs_lint.py) and by scripts/lint-vs-snippets.py:

    {"_meta": {...},
     "f": {"SetClFillFore": {"p": [["clasName", "COLOR"], ...],   # Python params
                             "r": "",                                # return (Python LHS), "" = procedure
                             "py": "vs.SetClFillFore(className, color)",
                             "v": 1}}}                               # optional: variadic / since-version

Stdlib only. Usage:
    py -3.12 scripts/gen-vs-signatures.py            # 2024 base + 2025 additions (default)
    py -3.12 scripts/gen-vs-signatures.py --check    # exit 1 if the JSON is stale
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "vectorworks_mcp" / "data" / "vs_signatures.json"
STUBS = {
    "2024": [ROOT / "third_party" / "VectorworksSDK" / "2024" / "SDK" / "SDKVW(750359)" / "SDKLib" / "Include" / "vs.py",
             ROOT / "third_party" / "VectorworksSDKExamples" / "VectorworksSDK" / "SDK2024" / "SDKLib" / "Include" / "vs.py"],
    "2025": [ROOT / "third_party" / "VectorworksSDKExamples" / "VectorworksSDK" / "SDK2025" / "SDKLib" / "Include" / "vs.py"],
}

# Functions that open a modal dialog or wait for a user click/drag. On the
# Vectorworks main thread they block the bridge until the user reacts, so the
# host lint rejects them (vw_script lint:false overrides).
INTERACTIVE = {
    "AlertCritical", "AlertCriticalHLink", "AlertInform", "AlertInformDontShowAgain", "AlertInformHLink",
    "AlertInformHLinkN", "AlertQuestion", "AlertQuestionDontShowAgain", "AlrtDialog", "AngDialog", "AngDialog3D",
    "DetailGraphicOptDlg", "DistDialog", "EditSymMarkersDlg", "FormatTextDialog", "IntDialog", "NonUndoableActionOK",
    "PtDialog", "PtDialog3D", "RealDialog", "StrDialog", "YNDialog",
    "GetLine", "GetLine3D", "GetPt", "GetPt3D", "GetPtL", "GetPtL3D", "GetRect", "GetRect3D", "TrackObject",
    "TrackObjectN", "RunTempTool", "CallTool",
    "RunLayoutDialog", "RunLayoutDialogN", "RunNamedDialog", "RunNamedDialogN", "ShowEditTileDialog",
    "ShowEditTileSettingsDialog", "ShowNewTileDialog",
    "GetFile", "GetFileN", "PutFile", "GetFolder", "StdRead", "StdReadLn",
    "DisplayOrganizationDialog", "ObjPropsEditDlg", "ShowWebDlg", "TBB_OpenTBBSelDlg",
}

_DEF = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)$")
_PARAM = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*,?\s*#\s*(.*?)\s+-(?:\s|$)")
_PYLINE = re.compile(r"^\s*Python:\s*(.*)$")
_VSLINE = re.compile(r"^\s*VectorScript:\s*(.*)$")
_CLASS = re.compile(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)")
_CONST = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _norm_type(text: str) -> str:
    t = " ".join(text.split()).upper()
    if t.startswith("IN/OUT "):
        t = t[7:]
    if t.startswith("REAL ("):
        t = "REAL"
    if t.startswith("DYNARRAY") and "CHAR" in t:
        return "STRING"
    if t == "BOOLEAN":
        return "BOOLEAN"
    return t or "ANY"


def parse_stub(path: Path) -> tuple[dict, list[str]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    funcs: dict[str, dict] = {}
    others: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = _DEF.match(line)
        if not m:
            c = _CLASS.match(line) or _CONST.match(line)
            if c:
                others.append(c.group(1))
            i += 1
            continue
        name, rest = m.group(1), m.group(2)
        params: list[list[str]] = []
        if not rest.strip().startswith(")"):
            i += 1
            while i < n and not lines[i].strip().startswith("):"):
                pm = _PARAM.match(lines[i])
                if pm:
                    params.append([pm.group(1), _norm_type(pm.group(2))])
                elif lines[i].strip() and not lines[i].strip().startswith("#"):
                    # parameter without a type comment
                    bare = lines[i].strip().rstrip(",").strip()
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", bare):
                        params.append([bare, "ANY"])
                i += 1
        i += 1
        py = vsl = ""
        while i < n and not _DEF.match(lines[i]):
            if not py:
                pm = _PYLINE.match(lines[i])
                if pm:
                    py = pm.group(1).strip()
            if not vsl:
                vm = _VSLINE.match(lines[i])
                if vm:
                    vsl = vm.group(1).strip()
            if py and vsl:
                break
            i += 1
        ret = ""
        if "= vs." in py:
            ret = py.split("= vs.", 1)[0].strip()
        entry: dict = {"p": params, "r": ret, "py": py or f"vs.{name}(...)"}
        if "..." in vsl:
            entry["v"] = 1
        if name in INTERACTIVE:
            entry["ui"] = 1
        funcs[name] = entry
    return funcs, others


def build() -> dict:
    merged: dict[str, dict] = {}
    names: set[str] = set()
    sources = {}
    for version in ("2024", "2025"):
        stub = next((p for p in STUBS[version] if p.is_file()), None)
        if stub is None:
            print(f"warning: no vs.py stub for {version}", file=sys.stderr)
            continue
        sources[version] = str(stub.relative_to(ROOT)).replace("\\", "/")
        funcs, others = parse_stub(stub)
        names.update(others)
        for name, entry in funcs.items():
            if name not in merged:
                if version != "2024" and merged:
                    entry["since"] = version
                merged[name] = entry
    return {
        "_meta": {
            "generator": "scripts/gen-vs-signatures.py",
            "sources": sources,
            "types": "Python parameter types from the stub comments; DYNARRAY of CHAR -> STRING. "
                     "COLOR = (r, g, b) tuple 0..65535; POINT = (x, y); POINT3D/VECTOR = (x, y, z).",
            "count": len(merged),
        },
        "f": dict(sorted(merged.items())),
        "names": sorted(n for n in names if n not in merged),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the checked-in JSON differs")
    args = parser.parse_args()
    if args.check and not all(any(p.is_file() for p in paths) for paths in STUBS.values()):
        # third_party/ is not in git (SDK licence), so clones and CI cannot regenerate the
        # JSON; comparing a partial rebuild would always report it as stale.
        print("skip: SDK vs.py stubs not present (third_party/); freshness is checked where the SDK is installed")
        return 0
    data = build()
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False, sort_keys=False) + "\n"
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if current != text:
            print(f"{OUT} is stale; run scripts/gen-vs-signatures.py", file=sys.stderr)
            return 1
        print(f"ok: {data['_meta']['count']} functions")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({data['_meta']['count']} functions, {len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
