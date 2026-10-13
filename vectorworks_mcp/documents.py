"""Document pinning and targeting (contract 2.2).

The host remembers the last document the model observed. Writes carry
``expect_document_fingerprint`` for it (v2 bridges check it on the main
thread). A ``document`` argument (fingerprint, file name or path) is resolved
through ``list_documents`` and sent with ``activate_document: true``.

Pre-1.0 bridges only know the active document: the host compares the
fingerprint from ``get_document_info`` itself and, for ``apply_operations``,
sends the rev-5 target binding so the check happens on the main thread.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

from .bridge import BridgeClient, Instance
from .errors import VWError


def normalise_doc(raw: Any) -> Optional[dict]:
    """One document shape: {fingerprint, name, path, dirty, active?}."""
    if not isinstance(raw, dict):
        return None
    fp = raw.get("fingerprint") or raw.get("document_fingerprint")
    binding = raw.get("binding") if isinstance(raw.get("binding"), dict) else {}
    fp = fp or binding.get("document_fingerprint")
    name = raw.get("name") or raw.get("filename") or raw.get("file_name") or binding.get("file_name")
    path = raw.get("path") if "path" in raw else raw.get("filepath", raw.get("file_path", binding.get("file_path")))
    if not fp and not name:
        return None
    doc: dict[str, Any] = {"fingerprint": fp, "name": name or (ntpath.basename(path) if path else "Untitled"),
                           "path": path or ""}
    if isinstance(raw.get("dirty"), bool):
        doc["dirty"] = raw["dirty"]
    if isinstance(raw.get("active"), bool):
        doc["active"] = raw["active"]
    return doc


def _matches(doc: dict, hint: str) -> bool:
    if not doc or not hint:
        return False
    h = hint.strip().lower().replace("/", "\\")
    path = str(doc.get("path") or "").lower()
    name = str(doc.get("name") or "").lower()
    stem = name.rsplit(".", 1)[0]
    return h in {str(doc.get("fingerprint") or "").lower(), name, stem, path} or (bool(path) and path.endswith("\\" + h))


@dataclass
class Target:
    inst: Instance
    params: dict = field(default_factory=dict)   # merged into the native request params
    document: Optional[dict] = None
    warnings: list = field(default_factory=list)


class Session:
    def __init__(self, bridge: BridgeClient) -> None:
        self.bridge = bridge
        self.pinned: Optional[dict] = None   # {"fingerprint","name","path","instance"}
        self._lock = threading.Lock()
        self._plans: "OrderedDict[str, str]" = OrderedDict()
        self.descriptor_cache: dict[tuple, str] = {}

    # ----- pinning -----------------------------------------------------
    def observe(self, inst: Instance, doc: Optional[dict]) -> None:
        if doc and doc.get("fingerprint"):
            with self._lock:
                self.pinned = {"fingerprint": doc["fingerprint"], "name": doc.get("name"), "path": doc.get("path"),
                               "instance": inst.key}

    def pinned_for(self, inst: Instance) -> Optional[dict]:
        pin = self.pinned
        if pin and pin.get("instance") == inst.key:
            return pin
        return None

    # ----- documents ---------------------------------------------------
    def active_document(self, inst: Instance, *, fresh: bool = False) -> Optional[dict]:
        if inst.v2:
            status = self.bridge.ping(inst, max_age=0 if fresh else 1.0)
            return normalise_doc(status.get("active_document"))
        info = self.bridge.call(inst, "get_document_info", {}, kind="read")
        return normalise_doc(info)

    def list_documents(self, inst: Instance) -> list[dict]:
        if inst.supports("list_documents"):
            data = self.bridge.call(inst, "list_documents", {}, kind="read")
            items = data.get("documents") if isinstance(data, dict) else data
            return [d for d in (normalise_doc(x) for x in (items or [])) if d]
        doc = self.active_document(inst)
        if doc:
            doc["active"] = True
            return [doc]
        return []

    def resolve(self, inst: Instance, hint: str) -> dict:
        docs = self.list_documents(inst)
        matches = [d for d in docs if _matches(d, hint)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise VWError("document_not_found", f"No open document matches {hint!r}.",
                          details={"open_documents": [{"name": d["name"], "fingerprint": d["fingerprint"]} for d in docs]})
        raise VWError("validation_error", f"{hint!r} matches {len(matches)} open documents; pass the fingerprint.",
                      field="document", example=matches[0]["fingerprint"],
                      details={"matches": [{"name": d["name"], "path": d["path"], "fingerprint": d["fingerprint"]} for d in matches]})

    def target(self, document: Optional[str] = None, *, write: bool = False, need_document: bool = True,
               instance: Optional[str] = None, bind_apply: bool = False) -> Target:
        hint = (document or "").strip() or None
        pin = self.pinned

        def doc_match(active: dict) -> bool:
            key = hint or (pin or {}).get("fingerprint")
            return bool(key) and _matches(normalise_doc(active) or {}, key)

        inst = self.bridge.select(instance, document_match=doc_match if (hint or pin) else None)
        t = Target(inst=inst)
        if not need_document:
            return t
        if inst.v2:
            if hint:
                doc = self.resolve(inst, hint)
                t.params = {"expect_document_fingerprint": doc["fingerprint"], "activate_document": True}
                t.document = doc
                self.observe(inst, doc)
                return t
            active = self.active_document(inst)
            mine = self.pinned_for(inst)
            if write:
                if mine is None:
                    if active is None:
                        raise VWError("no_active_document", "No document is open in Vectorworks.")
                    self.observe(inst, active)
                    mine = self.pinned_for(inst)
                t.params = {"expect_document_fingerprint": mine["fingerprint"]}
                t.document = active if active and active.get("fingerprint") == mine["fingerprint"] else {
                    "fingerprint": mine["fingerprint"], "name": mine.get("name"), "path": mine.get("path")}
            else:
                t.document = active
                if mine and active and active.get("fingerprint") != mine["fingerprint"]:
                    t.warnings.append(f"Reading the active document '{active.get('name')}', not '{mine.get('name')}' "
                                      "you worked on before. Pass document=... to choose.")
                if mine is None and active:
                    self.observe(inst, active)
            return t
        # ---- pre-1.0 bridge: only the active document exists for us ----
        info = self.bridge.call(inst, "get_document_info", {}, kind="read")
        active = normalise_doc(info)
        if active is None:
            raise VWError("no_active_document", "No document is open in Vectorworks.")
        if hint and not _matches(active, hint):
            raise VWError("upgrade_required",
                          f"{hint!r} is not the active document and this pre-1.0 bridge cannot switch documents. "
                          f"Ask the user to bring it to the front, or upgrade the bridge.",
                          details={"active_document": active})
        mine = self.pinned_for(inst)
        if write and mine and not hint and mine["fingerprint"] != active["fingerprint"]:
            raise VWError("document_changed",
                          f"The active document is now '{active['name']}', not '{mine.get('name')}'.",
                          details={"active_document": active, "expected_document": mine})
        if hint or mine is None or write:
            self.observe(inst, active)
        t.document = active
        binding = info.get("binding") if isinstance(info, dict) else None
        if write and bind_apply and isinstance(binding, dict) and binding.get("file_path") and \
                "bound_apply_operations" in (inst.status.get("object_read_features") or []):
            t.params = _rev5_binding_params(binding)
        return t

    # ----- idempotency -------------------------------------------------
    def idempotency_key(self, given: Optional[str], plan: Any, target: Target) -> str:
        if given:
            return given
        fp = (target.document or {}).get("fingerprint") or ""
        bucket = int(time.time() // 60)
        digest = hashlib.sha256(json.dumps([plan, fp, bucket], sort_keys=True, default=str).encode()).hexdigest()
        key = "auto-" + digest[:24]
        burned = getattr(self, "_burned", set())
        while key in burned:  # its transaction was compensated (deleted again): a resend must not replay it
            key = "auto-" + hashlib.sha256((key + "+").encode()).hexdigest()[:24]
        return key

    def burn(self, key: str) -> None:
        """Never derive this auto key again (its committed objects were deleted by a compensating transaction)."""
        if not hasattr(self, "_burned"):
            self._burned: set[str] = set()
        self._burned.add(key)

    def check_plan(self, key: str, plan_hash: str) -> None:
        with self._lock:
            known = self._plans.get(key)
            if known is not None and known != plan_hash:
                raise VWError("validation_error", "idempotency_key was already used for a different plan.",
                              field="idempotency_key", example="plan-" + plan_hash[:8])

    def remember_plan(self, key: str, plan_hash: str) -> None:
        with self._lock:
            self._plans[key] = plan_hash
            while len(self._plans) > 256:
                self._plans.popitem(last=False)


def plan_hash(plan: Any) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _rev5_binding_params(binding: dict) -> dict:
    params = {
        "expected_file_path": binding.get("file_path", ""),
        "expected_document_fingerprint": binding.get("document_fingerprint", ""),
        "expected_document_generation": binding.get("document_generation", 1),
        "expected_bridge_session_id": binding.get("bridge_session_id", ""),
        "expected_active_layer_uuid": binding.get("active_layer_uuid", ""),
        "expected_active_layer_name": binding.get("active_layer_name", ""),
    }
    if isinstance(binding.get("dirty"), bool):
        params["expected_dirty"] = binding["dirty"]
        params["expected_dirty_is_set"] = True
    return params


def rev5_read_binding(info: Any) -> dict:
    """Binding params for rev-5 paged documentation reads (get_sheet_layers ...)."""
    binding = info.get("binding") if isinstance(info, dict) else None
    if not isinstance(binding, dict) or not binding.get("file_path"):
        raise VWError("unsupported", "This pre-1.0 bridge can read sheets/viewports only for saved documents. "
                      "Ask the user to save the document first, or upgrade the bridge.")
    params = _rev5_binding_params(binding)
    params.pop("expected_dirty", None)
    params.pop("expected_dirty_is_set", None)
    return params
