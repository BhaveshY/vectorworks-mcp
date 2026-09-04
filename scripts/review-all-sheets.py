"""Read-only, checkpointed review of every sheet, viewport, and annotation.

The runner uses only the nine-tool fast-native MCP surface. It never activates a
sheet, changes view state, mutates the document, or invokes arbitrary code. A
saved document and the revision-5 native documentation actions are required.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_ACTIONS = {
    "get_document_info",
    "get_sheet_layers",
    "get_viewports",
    "get_viewport_annotations",
    "get_view",
}
REQUIRED_EVIDENCE_FIELDS = {
    "check_id",
    "extracted_text",
    "source",
    "authoritative_url",
    "observed_value",
    "expected_value",
    "observed_at",
    "confidence",
}
REQUIRED_SOURCE_FIELDS = {"kind", "object_uuid", "sheet_layer_uuid", "viewport_uuid"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect every Vectorworks sheet without mutating document or view state.")
    parser.add_argument("--output", required=True, type=Path, help="Final JSON review report.")
    parser.add_argument("--checkpoint", required=True, type=Path, help="Atomic resume checkpoint JSON.")
    parser.add_argument("--resume", action="store_true", help="Resume only if the saved target binding still matches exactly.")
    parser.add_argument("--external-evidence", type=Path, help="Optional JSON array of source-bound authoritative checks.")
    parser.add_argument("--auth-token-file", type=Path, default=Path.home() / ".vectorworks-mcp" / "auth-token")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9877)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-sheets", type=int, default=10000)
    parser.add_argument("--max-viewports-per-sheet", type=int, default=10000)
    parser.add_argument("--max-annotations-per-viewport", type=int, default=10000)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("--host must be loopback")
    if not 1 <= args.port <= 65535:
        raise ValueError("--port must be between 1 and 65535")
    if not 1 <= args.page_size <= 200:
        raise ValueError("--page-size must be between 1 and 200")
    for key in ("max_sheets", "max_viewports_per_sheet", "max_annotations_per_viewport"):
        if not 1 <= getattr(args, key) <= 100000:
            raise ValueError(f"--{key.replace('_', '-')} must be between 1 and 100000")
    token_file = args.auth_token_file.expanduser().resolve()
    if not token_file.is_file():
        raise ValueError(f"authentication token file does not exist: {token_file}")
    output = args.output.expanduser().resolve()
    checkpoint = args.checkpoint.expanduser().resolve()
    if output == checkpoint:
        raise ValueError("--output and --checkpoint must be different files")
    output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    return output, checkpoint, token_file


def write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def exact_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("document binding is missing")
    binding = {key: value.get(key) for key in (
        "file_path", "document_fingerprint", "document_generation", "bridge_session_id", "dirty",
        "active_layer_uuid", "active_layer_name",
    )}
    if not all(isinstance(binding[key], str) and binding[key] for key in (
        "file_path", "document_fingerprint", "bridge_session_id", "active_layer_uuid", "active_layer_name"
    )):
        raise RuntimeError("document binding does not identify a saved file and bridge session")
    if isinstance(binding["document_generation"], bool) or not isinstance(binding["document_generation"], int):
        raise RuntimeError("document binding has no valid generation")
    if not isinstance(binding["dirty"], bool):
        raise RuntimeError("document binding has no dirty-state observation")
    return binding


def document_binding(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("vw_read document returned no data")
    return exact_binding(data.get("binding"))


def validate_external_evidence(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("--external-evidence must contain a JSON array")
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        label = f"external evidence item {index}"
        if not isinstance(item, dict) or set(item) != REQUIRED_EVIDENCE_FIELDS:
            raise ValueError(f"{label} must contain exactly {sorted(REQUIRED_EVIDENCE_FIELDS)}")
        source = item.get("source")
        if not isinstance(source, dict) or set(source) != REQUIRED_SOURCE_FIELDS:
            raise ValueError(f"{label}.source must contain exactly {sorted(REQUIRED_SOURCE_FIELDS)}")
        kind = source.get("kind")
        if kind not in {"sheet_layer", "viewport", "viewport_annotation"}:
            raise ValueError(f"{label}.source.kind must be sheet_layer, viewport, or viewport_annotation")
        if not isinstance(source.get("object_uuid"), str) or not source["object_uuid"]:
            raise ValueError(f"{label}.source.object_uuid must be a non-empty string")
        if not isinstance(source.get("sheet_layer_uuid"), str) or not source["sheet_layer_uuid"]:
            raise ValueError(f"{label}.source.sheet_layer_uuid must be a non-empty string")
        viewport_uuid = source.get("viewport_uuid")
        if kind == "sheet_layer":
            if viewport_uuid is not None or source["object_uuid"] != source["sheet_layer_uuid"]:
                raise ValueError(f"{label}.source sheet-layer identity is inconsistent")
        elif not isinstance(viewport_uuid, str) or not viewport_uuid:
            raise ValueError(f"{label}.source.viewport_uuid must be a non-empty string")
        elif kind == "viewport" and source["object_uuid"] != viewport_uuid:
            raise ValueError(f"{label}.source viewport identity is inconsistent")
        check_id = item.get("check_id")
        if not isinstance(check_id, str) or not check_id or check_id in seen:
            raise ValueError(f"{label}.check_id must be unique and non-empty")
        seen.add(check_id)
        url = item.get("authoritative_url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"{label}.authoritative_url must be an HTTPS URL")
        confidence = item.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            raise ValueError(f"{label}.confidence must be a number from 0 to 1")
        extracted_text = item.get("extracted_text")
        if not isinstance(extracted_text, str) or not extracted_text.strip():
            raise ValueError(f"{label}.extracted_text must be non-empty text")
        try:
            observed_at = datetime.fromisoformat(str(item["observed_at"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label}.observed_at must be an ISO-8601 timestamp") from exc
        if observed_at.tzinfo is None:
            raise ValueError(f"{label}.observed_at must include a timezone")
        checked = dict(item)
        checked["matches"] = item["observed_value"] == item["expected_value"]
        checks.append(checked)
    return checks


async def run(args: argparse.Namespace, output: Path, checkpoint_path: Path, token_file: Path) -> dict[str, Any]:
    started_clock = time.perf_counter()
    evidence = validate_external_evidence(args.external_evidence)
    env = os.environ.copy()
    env.update({
        "VW_MCP_HOST": args.host,
        "VW_MCP_PORT": str(args.port),
        "VW_MCP_TOOL_PROFILE": "fast-native",
        "VW_MCP_AUTH_TOKEN_FILE": str(token_file),
    })
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], cwd=ROOT, env=env)
    report: dict[str, Any] = {
        "schema": "vectorworks.documentation-review.v1",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "mutations_attempted": 0,
        "sheets": [],
        "external_evidence": evidence,
    }
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=args.timeout_seconds)) as session:
                await session.initialize()

                async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    result = await session.call_tool(tool, arguments)
                    payload = result.structuredContent or {}
                    if result.isError or not isinstance(payload, dict) or payload.get("ok") is False:
                        raise RuntimeError(f"{tool} failed: {json.dumps(payload, ensure_ascii=False)[:4000]}")
                    return payload

                health = await call("vw_status", {"action": "health"})
                bridge = health.get("bridge") if isinstance(health.get("bridge"), dict) else {}
                if bridge.get("native_bridge") is not True or int(bridge.get("capability_revision") or 0) < 5:
                    raise RuntimeError("review-all-sheets requires native SDK bridge capability revision 5 or newer")
                catalog = await call("vw_catalog", {"action": "capabilities"})
                catalog_data = catalog.get("data") if isinstance(catalog.get("data"), dict) else {}
                actions = set(catalog_data.get("implemented_actions") or [])
                if not REQUIRED_ACTIONS <= actions:
                    raise RuntimeError(f"native bridge is missing documentation read actions: {sorted(REQUIRED_ACTIONS - actions)}")

                initial_document = await call("vw_read", {"action": "document"})
                binding = document_binding(initial_document)
                initial_view = (await call("vw_view", {"action": "get"})).get("data")
                report["target_binding"] = binding
                report["initial_view"] = initial_view

                completed: set[str] = set()
                if args.resume:
                    if not checkpoint_path.is_file():
                        raise RuntimeError("--resume requires an existing checkpoint")
                    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                    if checkpoint.get("target_binding") != binding or checkpoint.get("initial_view") != initial_view:
                        raise RuntimeError("checkpoint target binding or initial view no longer matches")
                    prior_sheets = checkpoint.get("sheets")
                    if not isinstance(prior_sheets, list):
                        raise RuntimeError("checkpoint sheets are malformed")
                    report["sheets"] = prior_sheets
                    completed = {str(item.get("uuid")) for item in prior_sheets if isinstance(item, dict)}

                async def paged(action: str, *, sheet: str = "", viewport: str = "", maximum: int) -> list[dict[str, Any]]:
                    items: list[dict[str, Any]] = []
                    cursor = ""
                    while True:
                        arguments: dict[str, Any] = {
                            "action": action,
                            "limit": args.page_size,
                            "cursor": cursor,
                            "target_binding": binding,
                        }
                        if sheet:
                            arguments["sheet_layer_uuid"] = f"uuid:{sheet}"
                        if viewport:
                            arguments["viewport_uuid"] = f"uuid:{viewport}"
                        payload = await call("vw_read", arguments)
                        data = payload.get("data")
                        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                            raise RuntimeError(f"{action} returned a malformed page")
                        items.extend(item for item in data["items"] if isinstance(item, dict))
                        if len(items) > maximum:
                            raise RuntimeError(f"{action} exceeded its explicit review bound of {maximum}")
                        page = data.get("page") if isinstance(data.get("page"), dict) else {}
                        next_cursor = page.get("next_cursor")
                        if next_cursor is None:
                            return items
                        if not isinstance(next_cursor, str) or next_cursor == cursor:
                            raise RuntimeError(f"{action} returned an invalid next cursor")
                        cursor = next_cursor

                sheets = await paged("sheet_layers", maximum=args.max_sheets)
                for sheet in sheets:
                    sheet_uuid = str(sheet.get("uuid") or "")
                    if not sheet_uuid or sheet_uuid in completed:
                        continue
                    sheet_review = dict(sheet)
                    sheet_review["viewports"] = []
                    viewports = await paged(
                        "viewports", sheet=sheet_uuid, maximum=args.max_viewports_per_sheet
                    )
                    for viewport in viewports:
                        viewport_review = dict(viewport)
                        viewport_uuid = str(viewport.get("uuid") or "")
                        if not viewport_uuid:
                            raise RuntimeError("viewport readback omitted stable UUID")
                        viewport_review["annotations"] = await paged(
                            "viewport_annotations",
                            sheet=sheet_uuid,
                            viewport=viewport_uuid,
                            maximum=args.max_annotations_per_viewport,
                        )
                        sheet_review["viewports"].append(viewport_review)
                    report["sheets"].append(sheet_review)
                    current_binding = document_binding(await call("vw_read", {"action": "document"}))
                    current_view = (await call("vw_view", {"action": "get"})).get("data")
                    if current_binding != binding or current_view != initial_view:
                        raise RuntimeError("document binding or view state changed during review; checkpoint retained")
                    write_json_atomic(checkpoint_path, {
                        "schema": report["schema"],
                        "target_binding": binding,
                        "initial_view": initial_view,
                        "sheets": report["sheets"],
                    })

                final_binding = document_binding(await call("vw_read", {"action": "document"}))
                final_view = (await call("vw_view", {"action": "get"})).get("data")
                identities = {
                    ("viewport_annotation", str(annotation.get("uuid")), str(sheet.get("uuid")), str(viewport.get("uuid")))
                    for sheet in report["sheets"]
                    for viewport in sheet.get("viewports", [])
                    for annotation in viewport.get("annotations", [])
                    if isinstance(annotation, dict)
                }
                identities.update(
                    ("viewport", str(viewport.get("uuid")), str(sheet.get("uuid")), str(viewport.get("uuid")))
                    for sheet in report["sheets"]
                    for viewport in sheet.get("viewports", [])
                    if isinstance(viewport, dict)
                )
                identities.update(
                    ("sheet_layer", str(sheet.get("uuid")), str(sheet.get("uuid")), None)
                    for sheet in report["sheets"]
                    if isinstance(sheet, dict)
                )
                for check in evidence:
                    source = check["source"]
                    check["source_resolved"] = (
                        source["kind"], source["object_uuid"], source["sheet_layer_uuid"], source["viewport_uuid"]
                    ) in identities
                report["final_binding"] = final_binding
                report["final_view"] = final_view
                report["state_unchanged"] = final_binding == binding and final_view == initial_view
                report["sheet_count"] = len(report["sheets"])
                report["viewport_count"] = sum(len(sheet.get("viewports", [])) for sheet in report["sheets"])
                report["annotation_count"] = sum(
                    len(viewport.get("annotations", []))
                    for sheet in report["sheets"] for viewport in sheet.get("viewports", [])
                )
                report["evidence_checks_passed"] = (
                    all(check["matches"] and check["source_resolved"] for check in evidence)
                    if evidence else None
                )
                report["completed_at"] = datetime.now(timezone.utc).isoformat()
                report["elapsed_ms"] = round((time.perf_counter() - started_clock) * 1000, 3)
                if not report["state_unchanged"]:
                    raise RuntimeError("final document binding or view state differs from the start")
    write_json_atomic(output, report)
    return report


def main() -> int:
    try:
        args = parse_args()
        output, checkpoint, token_file = validate_args(args)
        report = anyio.run(run, args, output, checkpoint, token_file)
        print(json.dumps({
            "ok": True,
            "output": str(output),
            "sheet_count": report["sheet_count"],
            "viewport_count": report["viewport_count"],
            "annotation_count": report["annotation_count"],
            "state_unchanged": report["state_unchanged"],
        }, separators=(",", ":"), sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
