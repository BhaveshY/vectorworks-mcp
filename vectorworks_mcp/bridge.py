"""Bridge client: discovery, persistent per-instance connections, budgets,
async jobs, auth re-read, busy handling and the pre-1.0 fallback (contract 2.1).

Two persistent sockets per instance:
- ``main``: main-thread CAD requests, serialised by a lock acquired with a timeout;
- ``control``: socket-thread actions (ping, capabilities, job_status, cancel_job)
  so health and job polling never queue behind a long CAD call.
"""

from __future__ import annotations

import json
import logging
import os
import select
import socket
import struct
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import config
from .errors import NATIVE_CODE_MAP, VWError

log = logging.getLogger("vectorworks_mcp.bridge")

MAX_FRAME = 16 * 1024 * 1024
UNREACHABLE_CACHE_S = 2.0
STATUS_TTL_S = 3.0
SOCKET_THREAD_ACTIONS = {"ping", "capabilities", "job_status", "cancel_job", "stop"}
# Actions that do not need the main-thread lock on v2 bridges.

# Native actions every pre-1.0 (rev <= 5) bridge is known to lack.
V2_ONLY = {"list_documents", "activate_document", "new_document", "close_document", "job_status", "cancel_job",
           "layers", "saved_views", "stories", "run_script", "export_ifc", "import_ifc", "group", "records"}


class TransportError(Exception):
    def __init__(self, message: str, *, sent: bool, timeout: bool = False, refused: bool = False) -> None:
        super().__init__(message)
        self.sent = sent
        self.timeout = timeout
        self.refused = refused


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < n:
        chunk = sock.recv(n - len(chunks))
        if not chunk:
            raise ConnectionError("connection closed by Vectorworks bridge")
        chunks.extend(chunk)
    return bytes(chunks)


class Connection:
    def __init__(self, host: str, port: int, budgets: config.Budgets, label: str) -> None:
        self.host = host
        self.port = port
        self.budgets = budgets
        self.label = label
        self.sock: Optional[socket.socket] = None
        self.last_used = 0.0

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _stale(self) -> bool:
        if self.sock is None:
            return True
        if time.monotonic() - self.last_used > self.budgets.idle_close:
            return True
        try:
            readable, _, _ = select.select([self.sock], [], [], 0)
        except (OSError, ValueError):
            return True
        if readable:  # unsolicited data or EOF on an idle socket -> unusable
            return True
        return False

    def ensure(self, fresh_after_s: Optional[float] = None) -> None:
        if fresh_after_s is not None and self.sock is not None and time.monotonic() - self.last_used > fresh_after_s:
            self.close()
        if self._stale():
            self.close()
            try:
                sock = socket.create_connection((self.host, self.port), timeout=self.budgets.connect)
            except socket.timeout as exc:
                raise TransportError(f"connect to {self.host}:{self.port} timed out", sent=False, refused=True) from exc
            except OSError as exc:
                raise TransportError(f"connect to {self.host}:{self.port} failed: {exc}", sent=False, refused=True) from exc
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass
            self.sock = sock
            self.last_used = time.monotonic()

    def roundtrip(self, payload: dict, timeout_s: float, fresh_after_s: Optional[float] = None) -> dict:
        self.ensure(fresh_after_s)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_FRAME:
            raise VWError("validation_error", f"request is {len(body)} bytes; the bridge limit is 16 MiB. Split the call.")
        sock = self.sock
        assert sock is not None
        sock.settimeout(max(0.2, timeout_s))
        try:
            sock.sendall(struct.pack(">I", len(body)) + body)
        except OSError as exc:
            self.close()
            raise TransportError(f"send failed: {exc}", sent=False) from exc
        try:
            header = _recv_exact(sock, 4)
            size = struct.unpack(">I", header)[0]
            if size > MAX_FRAME:
                raise ConnectionError(f"bridge frame of {size} bytes exceeds 16 MiB")
            data = _recv_exact(sock, size)
        except socket.timeout as exc:
            self.close()
            raise TransportError(f"no reply within {timeout_s:.0f} s", sent=True, timeout=True) from exc
        except (OSError, ConnectionError) as exc:
            self.close()
            raise TransportError(f"connection lost: {exc}", sent=True) from exc
        self.last_used = time.monotonic()
        try:
            reply = json.loads(data.decode("utf-8"))
        except ValueError as exc:
            self.close()
            raise VWError("protocol_error", f"bridge sent invalid JSON: {exc}") from exc
        if not isinstance(reply, dict) or not isinstance(reply.get("success"), bool):
            raise VWError("protocol_error", "bridge reply has no boolean 'success'")
        return reply


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied -> exists
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Instance:
    port: int
    pid: Optional[int] = None
    source: str = "probe"  # discovery | probe | pinned_port
    discovery: dict = field(default_factory=dict)
    status: dict = field(default_factory=dict)
    status_at: float = 0.0
    capabilities: Optional[dict] = None
    main: Optional[Connection] = None
    control: Optional[Connection] = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    control_lock: threading.Lock = field(default_factory=threading.Lock)
    token_index: int = 0

    @property
    def key(self) -> str:
        return str(self.pid) if self.pid else f"port:{self.port}"

    @property
    def revision(self) -> int:
        value = self.status.get("capability_revision", self.discovery.get("capability_revision", 0))
        return value if isinstance(value, int) else 0

    @property
    def protocol(self) -> int:
        value = self.status.get("protocol", self.discovery.get("protocol", 1))
        return value if isinstance(value, int) else 1

    @property
    def v2(self) -> bool:
        return self.protocol >= 2 or self.revision >= 6

    def implemented(self) -> set[str]:
        actions = self.status.get("implemented_actions")
        names: set[str] = set(actions) if isinstance(actions, list) else set()
        caps = self.capabilities or {}
        for entry in caps.get("actions") or []:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.add(entry["name"])
            elif isinstance(entry, str):
                names.add(entry)
        return names

    def action_meta(self, action: str) -> dict:
        for entry in (self.capabilities or {}).get("actions") or []:
            if isinstance(entry, dict) and entry.get("name") == action:
                return entry
        return {}

    def supports(self, action: str) -> bool:
        names = self.implemented()
        if names:
            return action in names
        return action not in V2_ONLY or self.v2

    def summary(self) -> dict:
        s = self.status
        out = {
            "instance": self.key,
            "pid": self.pid or s.get("pid"),
            "port": self.port,
            "protocol": self.protocol,
            "capability_revision": self.revision,
            "bridge_version": s.get("bridge_version") or s.get("version") or self.discovery.get("bridge_version"),
            "vectorworks_version": s.get("vectorworks_version") or self.discovery.get("vectorworks_version"),
            "ui_state": s.get("ui_state") or ("idle" if s.get("main_context_pump_ready") else None),
        }
        if s.get("build_id") or self.discovery.get("build_id"):
            out["build_id"] = s.get("build_id") or self.discovery.get("build_id")
        if not self.v2:
            out["pre_1_0"] = True
        return {k: v for k, v in out.items() if v is not None}

    def close(self) -> None:
        for conn in (self.main, self.control):
            if conn is not None:
                conn.close()


class BridgeClient:
    def __init__(self) -> None:
        self.budgets = config.Budgets()
        self.host = config.host()
        self._instances: dict[str, Instance] = {}
        self._discovered_at = 0.0
        self._unreachable_until: dict[int, float] = {}
        self._pinned_key: Optional[str] = None
        self._last_used_key: Optional[str] = None
        self._lock = threading.Lock()
        self.last_discovery_problems: list[dict] = []
        threading.Thread(target=self._reap_idle, name="vw-idle-reaper", daemon=True).start()

    def _reap_idle(self) -> None:
        """Close sockets idle for more than budgets.idle_close (contract 2.1: 30 s)."""
        while True:
            time.sleep(10.0)
            now = time.monotonic()
            for inst in list(self._instances.values()):
                for conn, lock in ((inst.main, inst.lock), (inst.control, inst.control_lock)):
                    if conn is None or conn.sock is None or now - conn.last_used <= self.budgets.idle_close:
                        continue
                    if lock.acquire(blocking=False):
                        try:
                            if conn.sock is not None and time.monotonic() - conn.last_used > self.budgets.idle_close:
                                conn.close()
                        finally:
                            lock.release()

    # ----- discovery ---------------------------------------------------
    def _read_instance_files(self) -> list[dict]:
        records = []
        directory = config.instances_dir()
        try:
            files = list(directory.glob("*.json"))
        except OSError:
            return records
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("port"), int):
                continue
            pid = data.get("pid") if isinstance(data.get("pid"), int) else 0
            if pid and not _pid_alive(pid):
                try:
                    path.unlink()  # contract 1.2: hosts prune files whose PID is dead
                except OSError:
                    pass
                continue
            records.append(data)
        return records

    def discover(self, force: bool = False) -> list[Instance]:
        with self._lock:
            now = time.monotonic()
            if not force and self._instances and now - self._discovered_at < 2.0:
                return list(self._instances.values())
            found: dict[str, Instance] = {}
            pinned = config.pinned_port()
            if pinned is not None:
                inst = self._instances.get(f"port:{pinned}") or next(
                    (i for i in self._instances.values() if i.port == pinned), None) or Instance(port=pinned, source="pinned_port")
                found[inst.key] = inst
            else:
                for record in self._read_instance_files():
                    pid = record.get("pid") if isinstance(record.get("pid"), int) else None
                    key = str(pid) if pid else f"port:{record['port']}"
                    inst = self._instances.get(key) or Instance(port=record["port"], pid=pid, source="discovery")
                    if inst.port != record["port"]:
                        inst.close()
                        inst = Instance(port=record["port"], pid=pid, source="discovery")
                    inst.discovery = record
                    found[inst.key] = inst
                if not found:
                    inst = self._instances.get(f"port:{config.DEFAULT_PORT}") or Instance(port=config.DEFAULT_PORT, source="probe")
                    found[inst.key] = inst
            for key, old in self._instances.items():
                if key not in found:
                    old.close()
            self._instances = found
            self._discovered_at = now
            return list(found.values())

    def instances(self) -> list[Instance]:
        return self.discover()

    def pin(self, hint: str) -> Instance:
        hint = str(hint).strip().lower().replace("port:", "")
        for inst in self.discover(force=True):
            if hint in {str(inst.pid or ""), str(inst.port), inst.key.lower()}:
                self._pinned_key = inst.key
                return inst
        raise VWError("not_found", f"No Vectorworks instance matches {hint!r}.",
                      details={"instances": [i.summary() for i in self._instances.values()]})

    def forget(self) -> None:
        """Close all sockets (vw_status disconnect)."""
        with self._lock:
            for inst in self._instances.values():
                inst.close()

    def shutdown(self) -> None:
        self.forget()

    # ----- low-level request ------------------------------------------
    def _connection(self, inst: Instance, which: str) -> Connection:
        conn = getattr(inst, which)
        if conn is None:
            conn = Connection(self.host, inst.port, self.budgets, which)
            setattr(inst, which, conn)
        return conn

    def _raw(self, inst: Instance, action: str, params: dict, timeout_s: float, *, which: str,
             timeout_ms: Optional[int] = None, fresh_after_s: Optional[float] = None) -> dict:
        now = time.monotonic()
        until = self._unreachable_until.get(inst.port, 0.0)
        if now < until:
            raise TransportError(f"127.0.0.1:{inst.port} refused a connection {now - (until - UNREACHABLE_CACHE_S):.1f} s ago",
                                 sent=False, refused=True)
        tokens = config.auth_token_candidates()
        if not tokens and not config.insecure_no_auth():
            raise VWError("auth_failed", f"No auth token: {config.auth_token_file()} is missing and VW_MCP_AUTH_TOKEN is not set.",
                          next_step="Ask the user to run the installer (it creates the shared token), then restart Vectorworks.")
        conn = self._connection(inst, which)
        attempts = max(1, len(tokens))
        start_index = inst.token_index if inst.token_index < attempts else 0
        last: Optional[dict] = None
        for n in range(attempts):
            index = (start_index + n) % attempts
            payload: dict[str, Any] = {"id": uuid.uuid4().hex[:16], "action": action,
                                       "auth_token": tokens[index] if tokens else "", "params": params}
            if timeout_ms is not None and inst.v2:
                payload["timeout_ms"] = int(timeout_ms)
            try:
                reply = conn.roundtrip(payload, timeout_s, fresh_after_s)
            except TransportError as exc:
                if exc.refused:
                    self._unreachable_until[inst.port] = time.monotonic() + UNREACHABLE_CACHE_S
                raise
            self._unreachable_until.pop(inst.port, None)
            if not reply.get("success") and _is_auth_failure(reply):
                last = reply
                continue  # auth failure: nothing ran, try the next token (re-read each call)
            inst.token_index = index
            return reply
        assert last is not None
        return last

    # ----- health ------------------------------------------------------
    def ping(self, inst: Instance, *, max_age: float = 0.0) -> dict:
        if max_age and inst.status and time.monotonic() - inst.status_at < max_age:
            return inst.status
        if not inst.control_lock.acquire(timeout=self.budgets.health):
            return inst.status
        try:
            reply = self._raw(inst, "ping", {}, self.budgets.health, which="control")
        except TransportError as exc:
            if exc.refused:
                raise VWError("bridge_unreachable", f"Nothing is listening on 127.0.0.1:{inst.port} ({exc}).") from None
            raise VWError("vectorworks_busy" if exc.timeout else "bridge_unreachable",
                          f"Vectorworks bridge on port {inst.port} did not answer ping: {exc}") from None
        finally:
            inst.control_lock.release()
        if not reply.get("success"):
            raise self._reply_error(reply, "ping", inst)
        status = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        old_session = inst.status.get("session_id") or inst.status.get("bridge_session_id")
        new_session = status.get("session_id") or status.get("bridge_session_id")
        if old_session and new_session and old_session != new_session:
            inst.capabilities = None  # Vectorworks restarted: forget the manifest
        inst.status = status
        inst.status_at = time.monotonic()
        if isinstance(status.get("pid"), int) and not inst.pid:
            inst.pid = status["pid"]
        return status

    def capabilities(self, inst: Instance) -> dict:
        if inst.capabilities is not None:
            return inst.capabilities
        with inst.control_lock:
            try:
                reply = self._raw(inst, "capabilities", {}, self.budgets.health * 2, which="control")
            except TransportError as exc:
                raise VWError("bridge_unreachable", f"capabilities request failed: {exc}") from None
        if not reply.get("success"):
            raise self._reply_error(reply, "capabilities", inst)
        result = reply.get("result")
        inst.capabilities = result if isinstance(result, dict) else {"raw": result}
        return inst.capabilities

    # ----- instance selection -----------------------------------------
    def select(self, instance_hint: Optional[str] = None, document_match: Optional[Callable[[dict], bool]] = None) -> Instance:
        if instance_hint:
            inst = self.pin(instance_hint)
            self.ping(inst)
            return inst
        candidates = self.discover()
        reachable: list[Instance] = []
        errors: list[VWError] = []
        for inst in candidates:
            try:
                self.ping(inst, max_age=STATUS_TTL_S)
                reachable.append(inst)
            except VWError as exc:
                errors.append(exc)
        if not reachable:
            if candidates and candidates[0].source == "probe" and not errors:
                raise VWError("bridge_unreachable", "No Vectorworks instance found.")
            first = errors[0] if errors else VWError("bridge_unreachable", "No Vectorworks instance found.")
            if first.code == "bridge_unreachable":
                ports = ", ".join(str(i.port) for i in candidates)
                first.message = (f"Vectorworks is not reachable (checked port {ports}). "
                                 "Vectorworks is closed or the MCP bridge plug-in is not loaded.")
            raise first
        if len(reachable) == 1:
            self._last_used_key = reachable[0].key
            return reachable[0]
        for key in (self._pinned_key, self._last_used_key):
            for inst in reachable:
                if inst.key == key:
                    return inst
        if document_match is not None:
            matches = [i for i in reachable if document_match(i.status.get("active_document") or {})]
            if len(matches) == 1:
                self._last_used_key = matches[0].key
                return matches[0]
        raise VWError("multiple_instances", f"{len(reachable)} Vectorworks instances are running.",
                      details={"instances": [i.summary() | {"active_document": (i.status.get("active_document") or {}).get("name")}
                                             for i in reachable]})

    # ----- main calls --------------------------------------------------
    def call(self, inst: Instance, action: str, params: Optional[dict] = None, *, kind: str = "read",
             idempotency_key: Optional[str] = None, budget_s: Optional[float] = None,
             progress: Optional[Callable[[float, Optional[float], str], None]] = None,
             cancelled: Optional[threading.Event] = None) -> Any:
        """Run one native action. kind: read | write | long. Raises VWError."""
        params = dict(params or {})
        if not inst.supports(action):
            raise self.missing_action(inst, action)
        budget = budget_s or (self.budgets.read if kind == "read" else self.budgets.write)
        if not inst.v2 and kind != "read":
            self._pre_v2_busy_check(inst)
        if action in SOCKET_THREAD_ACTIONS:
            with inst.control_lock:
                reply = self._send_with_retry(inst, action, params, budget, kind, which="control", idempotency_key=idempotency_key)
            return self._unwrap(inst, action, reply, kind, progress, cancelled)
        long_job = kind == "long" and inst.v2 and (inst.action_meta(action).get("supports_async") or action in {
            "open_document", "export_pdf", "export_dwg", "import_dwg", "export_ifc", "import_ifc",
            "export_vectorworks_document", "run_script", "capture_view"})
        if long_job:
            params["async"] = True
        if not inst.lock.acquire(timeout=self.budgets.lock_wait):
            raise VWError("vectorworks_busy", "Another request from this assistant is still running in Vectorworks.",
                          retryable=True, commit_state="not_started", next_step="Wait for the running request to finish, then retry once.")
        try:
            timeout_ms = int(max(1.0, budget - 2.0) * 1000)
            reply = self._send_with_retry(inst, action, params, budget, kind, which="main",
                                          idempotency_key=idempotency_key, timeout_ms=timeout_ms)
        finally:
            inst.lock.release()
        result = self._unwrap(inst, action, reply, kind, progress, cancelled)
        if long_job and isinstance(result, dict) and result.get("job_id") and result.get("state") in {"queued", "running"}:
            return self.wait_job(inst, str(result["job_id"]), progress=progress, cancelled=cancelled, action=action)
        return result

    def _pre_v2_busy_check(self, inst: Instance) -> None:
        status = self.ping(inst, max_age=STATUS_TTL_S)
        idle = status.get("main_context_idle_ms")
        if isinstance(idle, (int, float)) and not isinstance(idle, bool) and self.budgets.busy_idle_ms and idle >= self.budgets.busy_idle_ms:
            raise VWError("vectorworks_busy", f"Vectorworks has not serviced the bridge for {idle / 1000:.1f} s "
                          "(dialog, long render/export or frozen UI). Nothing was sent.", details={"ui_state": "unknown"})

    def _send_with_retry(self, inst: Instance, action: str, params: dict, budget: float, kind: str, *, which: str,
                         idempotency_key: Optional[str], timeout_ms: Optional[int] = None) -> dict:
        fresh_after = 5.0 if kind != "read" else None
        try:
            return self._raw(inst, action, params, budget, which=which, timeout_ms=timeout_ms, fresh_after_s=fresh_after)
        except TransportError as exc:
            if exc.refused:
                inst.status = {}
                raise VWError("bridge_unreachable", f"Vectorworks bridge on port {inst.port} is not reachable: {exc}") from None
            if not exc.sent:
                try:  # nothing reached Vectorworks: one transparent retry on a new socket
                    return self._raw(inst, action, params, budget, which=which, timeout_ms=timeout_ms)
                except TransportError as exc2:
                    raise VWError("bridge_unreachable", f"Vectorworks bridge connection failed: {exc2}") from None
            if kind == "read":
                if exc.timeout:
                    raise VWError("vectorworks_busy", f"Vectorworks did not answer {action} within {budget:.0f} s (busy or blocked by a dialog).",
                                  commit_state="not_started") from None
                try:
                    return self._raw(inst, action, params, budget, which=which, timeout_ms=timeout_ms)
                except TransportError as exc2:
                    raise VWError("bridge_unreachable", f"connection lost twice during {action}: {exc2}") from None
            # A write whose reply was lost: never resend; resolve by idempotency key.
            if idempotency_key and inst.supports("transaction_status"):
                try:
                    status_reply = self._raw(inst, "transaction_status", {"idempotency_key": idempotency_key},
                                             self.budgets.read, which="control" if inst.v2 else "main")
                    data = status_reply.get("result") if status_reply.get("success") else None
                    if isinstance(data, dict) and data.get("state") == "committed" and isinstance(data.get("recorded_result"), dict):
                        return {"success": True, "result": data["recorded_result"], "recovered": True}
                except (TransportError, VWError):
                    pass
            raise VWError("unknown_commit_state",
                          f"The reply to {action} was lost ({exc}); Vectorworks may or may not have applied it.",
                          commit_state="unknown", details={"idempotency_key": idempotency_key} if idempotency_key else None) from None

    def _unwrap(self, inst: Instance, action: str, reply: dict, kind: str, progress, cancelled) -> Any:
        if reply.get("success"):
            return reply.get("result")
        err = self._reply_error(reply, action, inst)
        if err.code == "still_running" and (err.details or {}).get("job_id") and kind == "long":
            return self.wait_job(inst, str(err.details["job_id"]), progress=progress, cancelled=cancelled, action=action)
        raise err

    def _reply_error(self, reply: dict, action: str, inst: Instance) -> VWError:
        message, code, details, commit_state, retryable = _parse_error(reply)
        if _is_auth_failure(reply):
            return VWError("auth_failed", "The Vectorworks bridge rejected the auth token (file and env token both tried).")
        lowered = message.lower()
        if code is None:
            if "unknown native bridge action" in lowered or "has no transport handler" in lowered or "not implemented" in lowered:
                return self.missing_action(inst, action)
            if "timed out waiting for vectorworks main" in lowered:
                code = "unknown_commit_state" if lowered.startswith("unknown_commit_state") else "vectorworks_busy"
            elif "not ready" in lowered and "pump" in lowered:
                code = "vectorworks_busy"
            elif "no active document" in lowered or "no document" in lowered:
                code = "no_active_document"
            elif "target binding" in lowered or "binding changed" in lowered or "document changed" in lowered or "expected_document" in lowered:
                code = "document_changed"
            elif "stopping" in lowered:
                code = "bridge_unreachable"
            else:
                code = "native_rejected" if action in {"apply_operations", "apply_documentation_operations"} else "native_error"
        host_code = NATIVE_CODE_MAP.get(code, code)
        if host_code == "unknown_commit_state":
            commit_state = "unknown"
        return VWError(host_code, message, retryable=retryable, commit_state=commit_state, details=details or None)

    def missing_action(self, inst: Instance, action: str) -> VWError:
        if not inst.v2:
            return VWError("upgrade_required",
                           f"The running Vectorworks bridge (capability revision {inst.revision}) has no '{action}' action; "
                           "it needs the 1.0 bridge (revision 6).", details={"native_action": action})
        return VWError("unsupported", f"This Vectorworks bridge does not implement '{action}'.", details={"native_action": action})

    # ----- async jobs --------------------------------------------------
    def wait_job(self, inst: Instance, job_id: str, *, progress=None, cancelled: Optional[threading.Event] = None,
                 action: str = "", total_budget: Optional[float] = None) -> Any:
        deadline = time.monotonic() + (total_budget or self.budgets.long_total)
        delay = 0.5
        started = time.monotonic()
        last_state = "queued"
        while True:
            if cancelled is not None and cancelled.is_set():
                raise VWError("still_running", f"Stopped waiting (request cancelled); job {job_id} continues in Vectorworks.",
                              details={"job_id": job_id})
            state = self.job_status(inst, job_id)
            last_state = str(state.get("state") or last_state)
            if last_state == "done":
                return state.get("result")
            if last_state in {"failed", "cancelled"}:
                raise self._reply_error({"success": False, "error": state.get("error") or f"job {last_state}",
                                         "error_code": state.get("error_code"), "details": state.get("details")}, action, inst)
            if progress is not None:
                elapsed = time.monotonic() - started
                progress(elapsed, None, f"{action or 'job'} {last_state} ({elapsed:.0f} s)")
            if time.monotonic() + delay > deadline:
                raise VWError("still_running", f"{action or 'The operation'} is still {last_state} in Vectorworks.",
                              details={"job_id": job_id, "state": last_state, "elapsed_s": round(time.monotonic() - started, 1)})
            time.sleep(delay)
            delay = min(2.0, delay * 1.5)

    def job_status(self, inst: Instance, job_id: str) -> dict:
        if not inst.supports("job_status"):
            raise self.missing_action(inst, "job_status")
        with inst.control_lock:
            try:
                reply = self._raw(inst, "job_status", {"job_id": job_id}, self.budgets.health * 2, which="control")
            except TransportError as exc:
                raise VWError("bridge_unreachable", f"job_status failed: {exc}") from None
        if not reply.get("success"):
            err = self._reply_error(reply, "job_status", inst)
            if "unknown job" in err.message.lower() or "not found" in err.message.lower() or err.code == "not_found":
                raise VWError("job_not_found", f"Job {job_id} is unknown to this Vectorworks session.")
            raise err
        result = reply.get("result")
        return result if isinstance(result, dict) else {"state": "unknown", "raw": result}


def _is_auth_failure(reply: dict) -> bool:
    if reply.get("success"):
        return False
    code = reply.get("error_code")
    if code == "auth_failed":
        return True
    message, *_ = _parse_error(reply)
    lowered = message.lower()
    return "authentication failed" in lowered or "auth_token" in lowered and "invalid" in lowered


def _parse_error(reply: dict) -> tuple[str, Optional[str], dict, Optional[str], Optional[bool]]:
    """Accept v2 fields and the legacy forms (JSON-encoded object string,
    'VW Error (...): {...}', 'code: message')."""
    raw = reply.get("error")
    code = reply.get("error_code") if isinstance(reply.get("error_code"), str) else None
    details = reply.get("details") if isinstance(reply.get("details"), dict) else {}
    commit_state = reply.get("commit_state") if isinstance(reply.get("commit_state"), str) else None
    retryable = reply.get("retryable") if isinstance(reply.get("retryable"), bool) else None
    message = raw if isinstance(raw, str) else json.dumps(raw) if raw is not None else "unknown bridge error"
    text = message.strip()
    if text.startswith("VW Error (") and "): " in text:
        text = text.split("): ", 1)[1].strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            text = str(obj.get("message") or text)
            code = code or (obj.get("code") if isinstance(obj.get("code"), str) else None)
            commit_state = commit_state or (obj.get("commit_state") if isinstance(obj.get("commit_state"), str) else None)
            if isinstance(obj.get("retryable"), bool) and retryable is None:
                retryable = obj["retryable"]
            for key in ("requested_path", "active_path"):
                if obj.get(key):
                    details[key] = obj[key]
    elif code is None and ":" in text.split(" ", 1)[0]:
        head = text.split(":", 1)[0]
        if head in {"unknown_commit_state", "vectorworks_busy", "document_changed", "stale_reference"}:
            code = head
    return text, code, details, commit_state, retryable
