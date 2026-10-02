"""The single error envelope (contract 2.4).

Every failure becomes ``{"ok": false, "error": {code, message, retryable,
next_step, commit_state?, ...}}`` and the MCP result carries
``isError = (ok is False)``.
"""

from __future__ import annotations

import os
from typing import Any, Optional

INSTALL_COMMAND = os.environ.get("VW_MCP_INSTALL_COMMAND") or (
    "vectorworksctl repair  (or, in the Vectorworks MCP folder: powershell -NoProfile -ExecutionPolicy Bypass "
    "-File .\\install.ps1), then restart Vectorworks"
)

# code -> (default retryable, next_step)
NEXT_STEPS: dict[str, tuple[bool, str]] = {
    "validation_error": (True, "Nothing was sent to Vectorworks. Fix the field named in error.field (see error.example) and call again."),
    "bridge_unreachable": (True, "Vectorworks is not running or the MCP bridge plug-in is not loaded. Ask the user to start Vectorworks 2024/2025 with a document open, then call vw_status once. Do not loop."),
    "vectorworks_busy": (True, "Vectorworks is busy (dialog open, mouse drag, render or file loading). Ask the user to close dialogs / finish the action, wait a few seconds and retry once."),
    "host_busy": (True, "Another request from this assistant is still running in Vectorworks. Wait for it to finish, then retry."),
    "auth_failed": (False, "Host and bridge auth tokens differ. Ask the user to rerun the installer (repairs the token) and restart Vectorworks."),
    "upgrade_required": (False, "The running Vectorworks bridge is older than this host. Ask the user to run: " + INSTALL_COMMAND + ". Until then use the other tools."),
    "unsupported": (False, "Vectorworks cannot do this through the bridge. Use vw_script for a custom vs.* script, or ask the user to do it manually."),
    "multiple_instances": (True, "Several Vectorworks instances are running. Call vw_status(instance=<pid or port>) to pin one, or pass document=<name>."),
    "document_changed": (True, "The active document is not the one you were working on. Call vw_read(what='documents') and either pass document=<name> to target the original or re-read to continue in the active one."),
    "document_not_found": (True, "No open document matches. Call vw_read(what='documents') for the open documents, or vw_document(action='open', path=...)."),
    "no_active_document": (True, "No document is open. Ask the user to open one, or call vw_document(action='new') / vw_document(action='open', path=...)."),
    "still_running": (True, "The operation is still running inside Vectorworks. Call vw_status(job_id=<job_id>) in a few seconds; do not resend the request."),
    "timeout_not_started": (True, "Vectorworks did not start the request in time; nothing changed. Retry once when Vectorworks is idle."),
    "unknown_commit_state": (False, "Do not resend. Call vw_status(idempotency_key=<key>) once; if still unknown, inspect the drawing with vw_read before deciding."),
    "native_rejected": (True, "Vectorworks rejected the request and rolled back; the document is unchanged. Fix the item named in the message and call again."),
    "native_error": (False, "Vectorworks reported an error. Read the message, fix the inputs, and do not repeat the identical call."),
    "stale_reference": (True, "An object reference is no longer valid. Re-read the objects with vw_read(what='query') and use the returned uuid."),
    "not_found": (True, "Nothing matched. Check the name/uuid with vw_read(what='query' or 'layers' or 'classes')."),
    "script_disabled": (False, "In-process scripting is disabled in this Vectorworks (VW_MCP_ALLOW_SCRIPT=0). Ask the user to enable it or do the step manually."),
    "script_error": (True, "The script raised an error and was rolled back. Fix the script (see stderr/traceback) and run again."),
    "confirmation_required": (True, "This action discards or deletes data. Ask the user, then repeat the call with the confirmation value shown in error.example."),
    "job_not_found": (False, "The job id is unknown (finished more than 15 minutes ago, or Vectorworks restarted). Inspect the drawing with vw_read instead."),
    "protocol_error": (True, "The bridge sent an invalid reply. Call vw_status once; if it persists ask the user to restart Vectorworks."),
    "too_many_clients": (True, "Too many MCP clients are connected to Vectorworks. Close other assistant sessions and retry."),
    "path_error": (True, "Fix the file path (absolute path, existing folder) and call again."),
    "configuration_error": (False, "The MCP host configuration is invalid; see vw_status for details and ask the user to rerun the installer."),
    "internal_error": (False, "The MCP host hit an unexpected error (logged under ~/.vectorworks-mcp/logs). Report it; try a simpler request."),
}

# Native error_code -> host code.
NATIVE_CODE_MAP = {
    "vectorworks_busy": "vectorworks_busy",
    "timeout_not_started": "timeout_not_started",
    "still_running": "still_running",
    "document_changed": "document_changed",
    "no_active_document": "no_active_document",
    "stale_reference": "stale_reference",
    "unsupported": "unsupported",
    "script_disabled": "script_disabled",
    "too_many_clients": "too_many_clients",
    "auth_failed": "auth_failed",
    "not_found": "not_found",
    "invalid_request": "native_rejected",
    "invalid_path": "path_error",
    "input_not_found": "path_error",
    "confirmation_required": "confirmation_required",
    "document_dirty": "confirmation_required",
    "interface_unavailable": "unsupported",
    "unknown_action": "upgrade_required",
}


class VWError(Exception):
    """Raised anywhere inside a tool; converted into the envelope at the edge."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: Optional[bool] = None,
        next_step: Optional[str] = None,
        commit_state: Optional[str] = None,
        field: Optional[str] = None,
        example: Any = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.next_step = next_step
        self.commit_state = commit_state
        self.field = field
        self.example = example
        self.details = details

    def envelope(self) -> dict[str, Any]:
        return error_envelope(
            self.code, self.message, retryable=self.retryable, next_step=self.next_step,
            commit_state=self.commit_state, field=self.field, example=self.example, details=self.details,
        )


def error_envelope(
    code: str,
    message: str,
    *,
    retryable: Optional[bool] = None,
    next_step: Optional[str] = None,
    commit_state: Optional[str] = None,
    field: Optional[str] = None,
    example: Any = None,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    default_retry, default_next = NEXT_STEPS.get(code, (False, NEXT_STEPS["native_error"][1]))
    error: dict[str, Any] = {
        "code": code,
        "message": str(message)[:2000],
        "retryable": default_retry if retryable is None else bool(retryable),
        "next_step": next_step or default_next,
    }
    if commit_state:
        error["commit_state"] = commit_state
    elif code in {"validation_error", "bridge_unreachable", "auth_failed", "upgrade_required", "multiple_instances",
                  "document_changed", "document_not_found", "no_active_document", "timeout_not_started",
                  "vectorworks_busy", "host_busy", "confirmation_required", "path_error", "too_many_clients"}:
        error["commit_state"] = "not_started"
    if field:
        error["field"] = field
    if example is not None:
        error["example"] = example
    if details:
        error["details"] = details
    return {"ok": False, "error": error}


def validation(message: str, field: str = "", example: Any = None) -> VWError:
    return VWError("validation_error", message, field=field or None, example=example)
