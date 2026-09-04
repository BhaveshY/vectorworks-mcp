"""Live revision-5 documentation acceptance against a saved disposable VWX.

The fixture is intentionally opt-in and uses only grouped MCP tools. It creates
a dedicated sheet, real viewport, and native annotation children, then reads,
updates, and partially deletes by exact UUID. Restart persistence and Undo are
reported as explicit follow-up gates because the connector does not expose a
command or arbitrary-script escape hatch for either UI lifecycle action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
DELETE_SHEET = "DELETE_SHEET_LAYER_AND_CONTENTS"
DELETE_ANNOTATION = "DELETE_VIEWPORT_ANNOTATION"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the native documentation lifecycle fixture.")
    parser.add_argument("--source-document", required=True, type=Path, help="Saved disposable .vwx active in Vectorworks.")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--allow-write-fixture", required=True, action="store_true")
    parser.add_argument("--disposable-document-confirmation", required=True, choices=["DISPOSABLE_DOCUMENT"])
    parser.add_argument("--cleanup", action="store_true", help="Delete the dedicated fixture sheet after evidence export.")
    parser.add_argument("--auth-token-file", type=Path, default=Path.home() / ".vectorworks-mcp" / "auth-token")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9877)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    source = args.source_document.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    token = args.auth_token_file.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".vwx":
        raise ValueError("--source-document must be an existing .vwx file")
    if not token.is_file():
        raise ValueError(f"authentication token file does not exist: {token}")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("--host must be loopback")
    output.mkdir(parents=True, exist_ok=True)
    return source, output, token


def binding_from(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    binding = data.get("binding") if isinstance(data, dict) else None
    if not isinstance(binding, dict):
        raise RuntimeError("document read omitted revision-5 target binding")
    keys = (
        "file_path", "document_fingerprint", "document_generation", "bridge_session_id", "dirty",
        "active_layer_uuid", "active_layer_name",
    )
    result = {key: binding.get(key) for key in keys}
    if not all(result[key] for key in (
        "file_path", "document_fingerprint", "bridge_session_id", "active_layer_uuid", "active_layer_name"
    )):
        raise RuntimeError("active document is not a saved, bound target")
    return result


def receipt_uuids(payload: dict[str, Any]) -> dict[str, str]:
    verification = payload.get("verification")
    receipts = verification.get("receipts") if isinstance(verification, dict) else None
    if not isinstance(receipts, list):
        raise RuntimeError("documentation transaction omitted receipts")
    result: dict[str, str] = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or receipt.get("verified") is not True or not receipt.get("uuid"):
            raise RuntimeError("documentation transaction returned an unverified receipt")
        local_ref = receipt.get("local_ref")
        if local_ref:
            result[str(local_ref)] = str(receipt["uuid"])
    return result


def file_evidence(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"expected output was not written: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": str(path), "size_bytes": path.stat().st_size, "sha256": digest}


async def run(args: argparse.Namespace, source: Path, output: Path, token: Path) -> dict[str, Any]:
    run_id = uuid.uuid4().hex[:10]
    prefix = f"VW_MCP_DOC_{run_id}"
    report: dict[str, Any] = {
        "schema": "vectorworks.documentation-acceptance.v1",
        "run_id": run_id,
        "source_document": str(source),
        "fixture_prefix": prefix,
        "started_unix": time.time(),
        "cleanup_requested": args.cleanup,
        "live_gates": {},
    }
    env = os.environ.copy()
    env.update({
        "VW_MCP_HOST": args.host,
        "VW_MCP_PORT": str(args.port),
        "VW_MCP_TOOL_PROFILE": "fast-native",
        "VW_MCP_AUTH_TOKEN_FILE": str(token),
    })
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], cwd=ROOT, env=env)
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=args.timeout_seconds)) as session:
                await session.initialize()

                async def call(tool: str, arguments: dict[str, Any], *, expect_ok: bool = True) -> dict[str, Any]:
                    result = await session.call_tool(tool, arguments)
                    payload = result.structuredContent or {}
                    ok = isinstance(payload, dict) and not result.isError and payload.get("ok") is not False
                    if ok != expect_ok:
                        raise RuntimeError(f"unexpected {tool} result: {json.dumps(payload, ensure_ascii=False)[:4000]}")
                    return payload

                health = await call("vw_status", {"action": "health"})
                bridge = health.get("bridge") if isinstance(health.get("bridge"), dict) else {}
                if bridge.get("native_bridge") is not True or int(bridge.get("capability_revision") or 0) < 5:
                    raise RuntimeError("fixture requires native SDK bridge capability revision 5 or newer")
                report["bridge"] = bridge

                document = await call("vw_read", {"action": "document"})
                binding = binding_from(document)
                if Path(binding["file_path"]).resolve() != source:
                    raise RuntimeError("active Vectorworks file does not match --source-document")
                initial_view = (await call("vw_view", {"action": "get"})).get("data")
                layers_payload = await call("vw_read", {"action": "layers", "limit": 200})
                layers = layers_payload.get("data") if isinstance(layers_payload.get("data"), list) else []
                design = next((item for item in layers if isinstance(item, dict) and item.get("kind") == "design" and item.get("uuid")), None)
                if not design:
                    raise RuntimeError("fixture requires one native design layer UUID")
                classes_payload = await call("vw_catalog", {"action": "classes", "limit": 200})
                classes_data = classes_payload.get("data")
                classes = (
                    classes_data
                    if isinstance(classes_data, list)
                    else classes_data.get("classes", []) if isinstance(classes_data, dict) else []
                )
                class_name = "None"
                for item in classes:
                    candidate = item.get("name") if isinstance(item, dict) else item
                    if isinstance(candidate, str) and candidate:
                        class_name = candidate
                        if candidate == "None":
                            break

                create_operations = [
                    {"type": "create_sheet_layer", "operation_id": "sheet", "params": {
                        "name": f"{prefix}-A101", "title": "Documentation Acceptance", "description": prefix,
                        "dpi": 144, "sheet_width": 420, "sheet_height": 297,
                    }},
                    {"type": "create_viewport", "operation_id": "viewport", "params": {
                        "sheet_layer_ref": "$sheet", "name": f"{prefix}-PLAN", "scale": 50,
                        "x": 210, "y": 148.5, "projection_type": 0, "view_type": 0,
                        "render_type": 0, "foreground_render_type": 0,
                        "source_layers": [{"ref": f"uuid:{design['uuid']}", "visibility": "normal"}],
                        "source_classes": [{"name": class_name, "visibility": "normal"}],
                        "crop_points": [[10, 10], [390, 10], [390, 277], [10, 277]],
                    }},
                    {"type": "create_viewport_annotation", "operation_id": "text", "params": {
                        "sheet_layer_ref": "$sheet", "viewport_ref": "$viewport", "annotation_kind": "text",
                        "class_name": class_name, "name": f"{prefix}-TEXT", "text": "REVISION 5 DOCUMENTATION FIXTURE",
                        "x1": 25, "y1": 25,
                    }},
                    {"type": "create_viewport_annotation", "operation_id": "dimension", "params": {
                        "sheet_layer_ref": "$sheet", "viewport_ref": "$viewport", "annotation_kind": "dimension",
                        "class_name": class_name, "x1": 25, "y1": 45, "x2": 125, "y2": 45, "offset": 10,
                    }},
                    {"type": "create_viewport_annotation", "operation_id": "marker", "params": {
                        "sheet_layer_ref": "$sheet", "viewport_ref": "$viewport", "annotation_kind": "marker",
                        "class_name": class_name, "x1": 25, "y1": 65, "x2": 125, "y2": 65,
                        "marker_style": 1, "marker_size": 12, "marker_angle": 15,
                    }},
                    {"type": "create_viewport_annotation", "operation_id": "redline", "params": {
                        "sheet_layer_ref": "$sheet", "viewport_ref": "$viewport", "annotation_kind": "redline",
                        "class_name": class_name, "points": [[25, 85], [125, 85], [125, 125], [25, 125]],
                    }},
                    {"type": "create_viewport_annotation", "operation_id": "temporary", "params": {
                        "sheet_layer_ref": "$sheet", "viewport_ref": "$viewport", "annotation_kind": "text",
                        "class_name": class_name, "text": "DELETE LIFECYCLE", "x1": 25, "y1": 145,
                    }},
                ]
                created = await call("vw_apply", {
                    "operations": create_operations,
                    "idempotency_key": f"{prefix}-create",
                    "coordinate_units": "mm",
                    "target_binding": binding,
                })
                ids = receipt_uuids(created)
                required_ids = {"sheet", "viewport", "text", "dimension", "marker", "redline", "temporary"}
                if set(ids) != required_ids:
                    raise RuntimeError(f"fixture receipts omitted local identities: {sorted(required_ids - set(ids))}")
                report["created_uuids"] = ids

                binding = binding_from(await call("vw_read", {"action": "document"}))
                sheets = await call("vw_read", {"action": "sheet_layers", "limit": 200, "target_binding": binding})
                viewports = await call("vw_read", {
                    "action": "viewports", "sheet_layer_uuid": f"uuid:{ids['sheet']}",
                    "limit": 200, "target_binding": binding,
                })
                annotations = await call("vw_read", {
                    "action": "viewport_annotations", "sheet_layer_uuid": f"uuid:{ids['sheet']}",
                    "viewport_uuid": f"uuid:{ids['viewport']}", "limit": 200, "target_binding": binding,
                })
                report["create_readback"] = {"sheets": sheets.get("data"), "viewports": viewports.get("data"), "annotations": annotations.get("data")}

                updated = await call("vw_apply", {
                    "operations": [
                        {"type": "update_sheet_layer", "params": {"target": f"uuid:{ids['sheet']}", "title": "Documentation Acceptance Updated"}},
                        {"type": "update_viewport", "params": {"target": f"uuid:{ids['viewport']}", "sheet_layer_ref": f"uuid:{ids['sheet']}", "scale": 25, "x": 205, "y": 145}},
                        {"type": "update_viewport_annotation", "params": {
                            "target": f"uuid:{ids['text']}", "sheet_layer_ref": f"uuid:{ids['sheet']}",
                            "viewport_ref": f"uuid:{ids['viewport']}", "text": "REVISION 5 UPDATED IN PLACE", "dx": 5, "dy": 5,
                        }},
                    ],
                    "idempotency_key": f"{prefix}-update",
                    "coordinate_units": "mm",
                    "target_binding": binding,
                })
                report["update_receipts"] = updated.get("verification")

                binding = binding_from(await call("vw_read", {"action": "document"}))
                rejected_delete = await call("vw_apply", {
                    "operations": [{"type": "delete_viewport_annotation", "params": {
                        "target": f"uuid:{ids['temporary']}", "sheet_layer_ref": f"uuid:{ids['sheet']}",
                        "viewport_ref": f"uuid:{ids['viewport']}", "confirm": "wrong",
                    }}],
                    "idempotency_key": f"{prefix}-reject-delete",
                    "target_binding": binding,
                }, expect_ok=False)
                if rejected_delete.get("writes_started") is not False:
                    raise RuntimeError("invalid delete confirmation was not rejected before dispatch")
                deleted = await call("vw_apply", {
                    "operations": [{"type": "delete_viewport_annotation", "params": {
                        "target": f"uuid:{ids['temporary']}", "sheet_layer_ref": f"uuid:{ids['sheet']}",
                        "viewport_ref": f"uuid:{ids['viewport']}", "confirm": DELETE_ANNOTATION,
                    }}],
                    "idempotency_key": f"{prefix}-delete",
                    "target_binding": binding,
                })
                report["delete_receipts"] = deleted.get("verification")

                binding = binding_from(await call("vw_read", {"action": "document"}))
                pdf = output / f"{prefix}.pdf"
                capture = output / f"{prefix}-active-view.png"
                await call("vw_io", {
                    "action": "export", "file_path": str(pdf), "format": "pdf",
                    "options": {"current_view_only": False}, "target_binding": binding,
                })
                await call("vw_io", {
                    "action": "capture", "file_path": str(capture), "format": "png",
                    "options": {"fit_to_objects": False}, "target_binding": binding,
                })
                report["outputs"] = {
                    "pdf_all_pages": file_evidence(pdf),
                    "active_view_capture": file_evidence(capture),
                    "capture_scope": "active view; inspect PDF for the fixture sheet",
                }
                final_view = (await call("vw_view", {"action": "get"})).get("data")
                report["live_gates"].update({
                    "create_read_update_delete": True,
                    "view_state_restored": final_view == initial_view,
                    "pdf_and_capture_written": True,
                    "undo": "manual_confirmation_required",
                    "restart_persistence": "rerun bound reads after Vectorworks restart and compare created_uuids",
                })

                if args.cleanup:
                    binding = binding_from(await call("vw_read", {"action": "document"}))
                    await call("vw_apply", {
                        "operations": [{"type": "delete_sheet_layer", "params": {
                            "target": f"uuid:{ids['sheet']}", "confirm": DELETE_SHEET,
                        }}],
                        "idempotency_key": f"{prefix}-cleanup",
                        "target_binding": binding,
                    })
                    report["cleanup_completed"] = True
                else:
                    report["cleanup_completed"] = False
                    report["fixture_sheet_uuid"] = ids["sheet"]

    report["elapsed_ms"] = round((time.time() - report["started_unix"]) * 1000, 3)
    report_path = output / f"{prefix}-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    try:
        args = parse_args()
        source, output, token = validate_args(args)
        report = anyio.run(run, args, source, output, token)
        print(json.dumps({"ok": True, "report": report["report_path"], "live_gates": report["live_gates"]}, separators=(",", ":"), sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
