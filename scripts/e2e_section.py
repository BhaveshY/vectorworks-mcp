"""Live Patrick-style 2D section acceptance through the source MCP stdio server.

Requires a running idle Vectorworks. Creates ONLY a new test document and retains it.
Usage: py -3.12 scripts/e2e_section.py --output <new evidence directory>
Never opens/modifies user documents, reinstalls a bridge, or restarts Vectorworks.
"""
from __future__ import annotations
import argparse
import base64
import json
import os
from pathlib import Path
import sys
import time

from e2e_host import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--installed", action="store_true", help="test the installed launcher instead of source")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    command = ["cmd.exe", "/d", "/c", str(Path(os.environ["USERPROFILE"]) / ".vectorworks-mcp/bin/vectorworks-mcp.cmd")] if args.installed else None
    client = Client(sys.executable, os.environ.copy(), "section-live", command=command)
    calls, checks = [], []
    started = time.perf_counter()
    doc = None

    def call(tool, arguments=None, allow_error=False):
        a = dict(arguments or {})
        if doc and tool != "vw_status":
            a.setdefault("document", doc)
        t0 = time.perf_counter()
        data, raw = client.call(tool, a, timeout=180)
        calls.append({"tool": tool, "arguments": a, "ms": round((time.perf_counter()-t0)*1000), "result": data})
        for i, block in enumerate(raw.get("content") or []):
            if block.get("type") == "image":
                (output / f"{len(calls):03d}-{tool}-{i}.png").write_bytes(base64.b64decode(block["data"]))
        (output / "calls.json").write_text(json.dumps(calls, indent=2), encoding="utf-8")
        if not allow_error and not data.get("ok"):
            raise AssertionError(json.dumps(data))
        return data

    def check(name, condition):
        checks.append({"name": name, "pass": bool(condition)})
        print(("PASS " if condition else "FAIL ") + name, flush=True)
        if not condition:
            raise AssertionError(name)

    def geometry(target, limit=1000, cursor=None):
        a = {"what": "geometry", "target": target, "detail": True, "limit": limit}
        if cursor:
            a["cursor"] = cursor
        return call("vw_read", a)

    try:
        client.initialize()
        status = call("vw_status")
        check("bridge ready", status.get("ready"))
        fresh = call("vw_document", {"action": "new"})
        doc = fresh["document"]["fingerprint"]
        objects = []
        floor = [0, 3880, 6760, 9640, 12520, 15400]
        clear = [3500, 2500, 2500, 2500, 2500, 2500]
        levels = floor + [18900]
        new_levels = [0, 4380, 7760, 11140, 14520, 17900, 21900]
        sections, facades, label_ids = [], [], []

        def add(name, kind, **props):
            objects.append({"id": name, **({"name": name} if kind != "dimension" else {}), "type": kind, **props})
            sections.append(name)

        for i, (bottom, height) in enumerate(zip(floor, clear)):
            top = levels[i+1]
            # Clear heights stretch, slab/roof packages retain thickness.
            add(f"slab{i}", "polygon", points=[[0,bottom+height],[10000,bottom+height],[10000,top],[0,top]], fill_color="#C0C0C0")
            for x in (0, 9700):
                add(f"wall{i}_{x}", "polygon", points=[[x,bottom],[x+300,bottom],[x+300,bottom+height],[x,bottom+height]], fill_color="#909090")
            points = [[4000,bottom]]
            for step in range(1,21):
                x = 4000 + step*200
                points.extend([[x, bottom+(top-bottom)*(step-1)/20], [x,bottom+(top-bottom)*step/20]])
            add(f"stairs{i}", "polyline", points=points, closed=False)
            add(f"clear{i}", "dimension", start=[-1200,bottom], end=[-1200,bottom+height], offset=-300)
        for i, height in enumerate(levels):
            name = f"level{i}"
            add(name, "text", at=[10500,height], text=f"LEVEL {height/1000:+.2f}", size=8)
            label_ids.append(name)
            name = f"facade{i}"
            objects.append({"id":name,"name":name,"type":"line","start":[14500,height],"end":[24500,height]})
            facades.append(name)
        objects.append({"id":"facade_wall","name":"facade_wall","type":"rect","origin":[14500,0],"width":300,"height":18900})
        facades.append("facade_wall")
        add("total", "dimension", start=[-3000,710], end=[-3000,18900], offset=-600)
        # Unchanged basement and >50 vertices prove traversal is complete and bounded reads are explicit.
        add("basement", "line", start=[0,-3380], end=[10000,-3380])
        add("long_poly", "polyline", points=[[i*80,-4000+(i%2)*100] for i in range(61)], closed=False)
        created = call("vw_create", {"objects": objects, "preview": False})
        ids = created.get("ids") or {}
        if not ids:
            ids = {name: created["uuids"][i] for name,i in created["named"].items()}
        # Native dimension creation rejects name, and native set_properties cannot register its undo state.
        # Fixture-only naming uses the SDK script transaction, without recreating a dimension.
        names = {ids[o["id"]]:o["id"] for o in objects if o["type"] == "dimension"}
        call("vw_script", {"code":"for uid,name in %r.items():\n    vs.SetName(vs.GetObjectByUuid(uid),name)\nresult=True" % names})

        def group(names, name):
            r = call("vw_edit", {"changes":[{"op":"group", "targets":[ids[n] for n in names], "name":name}],"preview":False})
            return r["receipts"][0]["uuid"]

        section = group(sections, "Section geometry")
        facade = group(facades, "Adjacent elevation")
        ids["section"], ids["facade"] = section, facade
        root = group(["section", "facade"], "Patrick section acceptance")
        before = geometry(root)
        by_name = {r["name"]:r for r in before["data"]}
        check("recursive geometry includes both nested groups and all objects", len(before["data"]) == len(objects)+3)
        check("full vertex read exceeds former 50 point limit", len(by_name["long_poly"]["points"]) == 61)
        page = geometry(root, limit=7)
        page2 = geometry(root, limit=7, cursor=page["page"]["next_cursor"])
        check("geometry pagination has no duplicates", not ({r["uuid"] for r in page["data"]} & {r["uuid"] for r in page2["data"]}))
        compact = call("vw_read", {"what":"geometry","target":ids["long_poly"]})
        check("compact vertex truncation is explicit", compact["data"][0].get("vertices_truncated") and compact["data"][0]["vertex_count"] == 61)
        call("vw_capture", {"view":"objects","objects":[root],"max_px":1600})
        change = {"op":"section_heights", "target":root,
                  "expected_geometry":{root:before["revision"]},
                  "bands":[{"bottom":b,"top":b+h,"delta":500} for b,h in zip(floor,clear)],
                  "stairs":[{"target":ids[f"stairs{i}"],"bottom":b,"top":levels[i+1]} for i,b in enumerate(floor)],
                  "fixed_points":[{"target":ids["total"],"vertex":1}],
                  "labels":[{"target":ids[n],"elevation":levels[i],"datum":110690,
                             "template":"{level} = {absolute} m NN"} for i,n in enumerate(label_ids)]}
        dry = call("vw_edit", {"changes":[change],"dry_run":True})
        check("dry run compiles one script transaction", len(dry["script_operations"]) == 1 and not dry["native_operations"])
        # Missing labels and stale snapshots are rejected without touching geometry.
        invalid = dict(change, labels=[])
        rejected = call("vw_edit", {"changes":[invalid]}, allow_error=True)
        check("unbound annotations rejected before write", not rejected["ok"] and geometry(root)["revision"] == before["revision"])
        edited = call("vw_edit", {"changes":[change],"preview":False})
        check("section edit uses one undo transaction", edited["undo_steps"] == 1)
        after = geometry(root)
        new = {r["name"]:r for r in after["data"]}
        check("UUIDs, object types, parents and classes preserved", all(all(new[n][k] == old[k] for k in ("uuid","type_id","parent_uuid","class")) for n,old in by_name.items()))
        for i,(b,h) in enumerate(zip(floor,clear)):
            points = new[f"slab{i}"]["points"]
            check(f"storey {i}: slab package unchanged", abs(points[2][1]-points[1][1]-(levels[i+1]-b-h)) < .01)
            pts = new[f"wall{i}_0"]["points"]
            check(f"storey {i}: clear height +500mm", abs(pts[2][1]-pts[0][1]-(h+500)) < .01)
            check(f"storey {i}: real dimension recalculated", new[f"clear{i}"]["dimension_text"] != by_name[f"clear{i}"]["dimension_text"] and abs(new[f"clear{i}"]["points"][1][1]-new[f"clear{i}"]["points"][0][1]-(h+500)) < .01)
            pts = new[f"stairs{i}"]["points"]
            check(f"storey {i}: stair connects new floors", abs(pts[0][1]-new_levels[i]) < .01 and abs(pts[-1][1]-new_levels[i+1]) < .01)
            rises = [pts[j][1]-pts[j-1][1] for j in range(2,len(pts),2)]
            check(f"storey {i}: equal stair risers retained", max(rises)-min(rises) < .01)
        for i,height in enumerate(new_levels):
            check(f"level {i}: elevation aligned", abs(new[f"facade{i}"]["points"][0][1]-height)<.01)
            check(f"level {i}: annotation recalculated", new[f"level{i}"]["text"] == f"{height/1000:+.2f} = {(height+110690)/1000:.2f} m NN")
        check("total dimension retains 710mm datum and measures 21190mm", new["total"]["points"] == [[-3000.0,710.0],[-3000.0,21900.0]])
        check("basement and below-ground polyline unchanged", new["basement"] == by_name["basement"] and new["long_poly"] == by_name["long_poly"])
        replay = call("vw_edit", {"changes":[change]}, allow_error=True)
        check("replay is refused with unchanged geometry", not replay["ok"] and geometry(root)["revision"] == after["revision"])
        # A later rejected edit must roll back an earlier successful mutation in the SAME transaction.
        bad_points = [p[:] for p in new["long_poly"]["points"]]
        bad_points[20][1] -= 500
        failed = call("vw_edit", {"changes":[{"op":"reshape","target":ids["long_poly"],"points":bad_points},
                                               {"op":"reshape","target":ids["slab0"],"points":[[0,0],[1,1]]}]}, allow_error=True)
        check("failure after mutation rolls back all geometry", not failed["ok"] and geometry(root)["revision"] == after["revision"])
        # Standalone dimensions need a full undo swap before raw endpoint
        # selectors are written; grouped geometry alone did not reveal this.
        standalone = call("vw_create", {"objects":[{"type":"dimension","id":"standalone_dim",
                         "start":[30000,0],"end":[30000,3500],"offset":-500}],"preview":False})["ids"]["standalone_dim"]
        standalone_before = geometry(standalone)
        failed = call("vw_edit", {"changes":[{"op":"reshape","target":standalone,"start":[30000,0],"end":[30000,4000]},
                                               {"op":"reshape","target":ids["slab0"],"points":[[0,0],[1,1]]}]},allow_error=True)
        check("standalone dimension anchors/value/style roll back after a later failure",
              not failed["ok"] and geometry(standalone)["revision"] == standalone_before["revision"])
        unsupported = call("vw_create", {"objects":[{"type":"circle","id":"unsupported","at":[30000,-5000],"radius":100}],"preview":False})["ids"]["unsupported"]
        unsupported_before = geometry(unsupported)
        failed = call("vw_edit", {"changes":[{"op":"section_heights", "targets":[root,unsupported],
                   "expected_geometry":{root:after["revision"],unsupported:unsupported_before["revision"]},
                   "bands":change["bands"],"labels":change["labels"],"stairs":[],"fixed_points":[]}]},allow_error=True)
        check("unsupported member rejects entire section before mutation", not failed["ok"] and geometry(root)["revision"] == after["revision"])
        # Explicit reshape paths exercise primitives separately from the section operation.
        points = [p[:] for p in new["long_poly"]["points"]]
        points[30][1] -= 100
        r = call("vw_edit", {"changes":[{"op":"reshape","target":ids["long_poly"],"points":points}]})
        check("explicit polyline vertex reshape", geometry(ids["long_poly"])["data"][0]["points"] == points)
        call("vw_edit", {"changes":[{"op":"reshape","target":ids["long_poly"],"points":new["long_poly"]["points"]}]})
        call("vw_edit", {"changes":[{"op":"reshape","target":ids["total"],"start":[-3000,710],"end":[-3000,21900]}]})
        check("explicit dimension reshape preserves offset", geometry(ids["total"])["data"][0]["offset"] == by_name["total"]["offset"])
        wall = call("vw_create", {"objects":[{"type":"wall","id":"regression_wall","start":[30000,-8000],"end":[32000,-8000],"height":2800}],"preview":False})["ids"]["regression_wall"]
        call("vw_edit", {"changes":[{"op":"reshape","target":wall,"start":[30000,-8000],"end":[33000,-8000]}],"preview":False})
        check("existing wall endpoint reshape still works", geometry(wall)["data"][0]["points"] == [[30000,-8000],[33000,-8000]])
        call("vw_capture", {"view":"objects","objects":[root],"max_px":1600})
        saved = call("vw_document", {"action":"save_as","path":str(output / "Patrick-section-after.vwx")})
        doc = saved["document"]["fingerprint"]
        check("native VWX saved", (output / "Patrick-section-after.vwx").is_file())
        final_geometry = geometry(root)
        call("vw_document", {"action":"close"})
        doc = None
        reopened = call("vw_document", {"action":"open","path":str(output / "Patrick-section-after.vwx")})
        doc = reopened["document"]["fingerprint"]
        persisted = geometry(root)
        check("saved document reopened with identical section geometry", persisted["revision"] == final_geometry["revision"])
        (output / "geometry-before.json").write_text(json.dumps(before, indent=2),encoding="utf-8")
        (output / "geometry-after.json").write_text(json.dumps(persisted, indent=2),encoding="utf-8")
    finally:
        (output / "report.json").write_text(json.dumps({"checks":checks,"passed":sum(c["pass"] for c in checks),"route":"installed" if args.installed else "source","elapsed_s":round(time.perf_counter()-started,2),"calls":len(calls),"document":doc},indent=2),encoding="utf-8")
        client.close()
    print(json.dumps({"output":str(output),"checks":len(checks),"seconds":round(time.perf_counter()-started,2)}))


if __name__ == "__main__":
    main()
