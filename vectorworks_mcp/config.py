"""Environment, paths and logging. Nothing here raises: problems are collected
and surfaced through vw_status instead of exiting the process."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("vectorworks_mcp")

DEFAULT_PORT = 9877
PORT_RANGE = tuple(range(9877, 9887))
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Problems found while reading configuration. Each entry is a dict with
# code/message/next_step and is shown by vw_status.
config_problems: list[dict] = []


def _problem(code: str, message: str, next_step: str) -> None:
    config_problems.append({"code": code, "message": message, "next_step": next_step})


def state_dir() -> Path:
    override = os.environ.get("VW_MCP_STATE_DIR", "").strip()
    if override:
        return Path(override)
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".vectorworks-mcp"


def instances_dir() -> Path:
    return state_dir() / "instances"


def logs_dir() -> Path:
    return state_dir() / "logs"


def captures_dir() -> Path:
    return state_dir() / "captures"


def auth_token_file() -> Path:
    override = os.environ.get("VW_MCP_AUTH_TOKEN_FILE", "").strip()
    return Path(override) if override else state_dir() / "auth-token"


def auth_token_candidates() -> list[str]:
    """Tokens to try, read fresh on every call (never frozen at import).

    The file written by the installer is authoritative; the environment
    variable is a second candidate so a stale launcher env cannot lock us out.
    """
    tokens: list[str] = []
    try:
        text = auth_token_file().read_text(encoding="utf-8-sig").strip()
        if text:
            tokens.append(text)
    except OSError:
        pass
    env = os.environ.get("VW_MCP_AUTH_TOKEN", "").strip()
    if env and env not in tokens:
        tokens.append(env)
    return tokens


def insecure_no_auth() -> bool:
    return os.environ.get("VW_MCP_INSECURE_NO_AUTH", "").strip().lower() in {"1", "true", "yes"}


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _problem("configuration_error", f"{name}={raw!r} is not a number; using {default}.",
                 f"Fix or remove {name} in the MCP client configuration.")
        return default
    return min(max(value, lo), hi)


def host() -> str:
    value = os.environ.get("VW_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if value not in LOOPBACK_HOSTS:
        return "127.0.0.1"
    return "127.0.0.1" if value == "localhost" else value


def pinned_port() -> Optional[int]:
    raw = os.environ.get("VW_MCP_PORT", "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def tool_profile() -> str:
    return os.environ.get("VW_MCP_TOOL_PROFILE", "").strip().lower() or "default"


def trace_enabled() -> bool:
    return os.environ.get("VW_MCP_TRACE", "").strip().lower() in {"1", "true", "yes"}


class Budgets:
    """Per-call budgets (seconds). Always below the client's tool timeout."""

    def __init__(self) -> None:
        self.read = _env_float("VW_MCP_READ_BUDGET", 20.0, 2.0, 60.0)
        self.write = _env_float("VW_MCP_WRITE_BUDGET", 45.0, 5.0, 120.0)
        self.long_total = _env_float("VW_MCP_LONG_BUDGET", 170.0, 10.0, 590.0)
        self.lock_wait = _env_float("VW_MCP_LOCK_WAIT", 8.0, 0.5, 60.0)
        self.connect = _env_float("VW_MCP_CONNECT_TIMEOUT", 0.75, 0.1, 10.0)
        self.health = _env_float("VW_MCP_HEALTH_TIMEOUT", 2.0, 0.2, 10.0)
        self.idle_close = 30.0
        self.busy_idle_ms = _env_float("VW_MCP_BUSY_THRESHOLD_MS", 8000.0, 0.0, 600000.0)


_logging_ready = False


def setup_logging() -> Optional[Path]:
    """stderr + rotating file. Never stdout (that carries MCP frames)."""
    global _logging_ready
    if _logging_ready:
        return None
    _logging_ready = True
    level = logging.DEBUG if trace_enabled() else logging.INFO
    log.setLevel(level)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING if not trace_enabled() else logging.DEBUG)
    err.setFormatter(fmt)
    log.addHandler(err)
    path: Optional[Path] = None
    try:
        logs_dir().mkdir(parents=True, exist_ok=True)
        path = logs_dir() / "host.log"
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8", delay=True)
        fh.setFormatter(fmt)
        fh.setLevel(level)
        log.addHandler(fh)
    except OSError as exc:  # read-only profile etc.
        _problem("log_unavailable", f"Cannot write logs under {logs_dir()}: {exc}", "Logging continues on stderr only.")
        path = None
    return path
