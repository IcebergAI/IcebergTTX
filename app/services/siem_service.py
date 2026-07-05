"""Pluggable SIEM forwarding for audit events (#24).

The application is the forwarder (no Vector/Fluent Bit sidecar). Each audit event
built by ``audit_service.emit`` is shipped, off the response path, to every
**enabled** delivery method chosen by the runtime ``AuditSettings`` row:

- ``stdout`` — the always-on baseline. The JSON line is already written by the
  ``iceberg_ttx.audit`` handler (``logging_config``), so this method adds no
  second emission here; it exists so an operator can BYO a node-level shipper.
- ``file``   — append the JSON line to a local file (for a file-tailing shipper).
- ``syslog`` — an RFC 5424 message over UDP/TCP, JSON event in the MSG body.
- ``http``   — a JSON ``POST`` to an HTTP event collector / webhook (Splunk HEC,
  Elastic, generic), authenticated with the env-only ``settings.siem_http_token``.

Every sink is wrapped so a failing/unreachable SIEM is logged locally but **never**
raises — auditing must not break the request that triggered it. Routing is read
from an in-memory snapshot (``get_config``/``set_config``) refreshed at startup and
whenever an admin saves, so the sync ``emit`` path never does a per-event DB read
(single-process, like ``ws_manager``/``rate_limit``).
"""

import asyncio
import json
import logging
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from app.config import settings

logger = logging.getLogger("iceberg_ttx.siem")

# Inspectable by tests — every event that passes the enabled/severity gate is
# appended here (regardless of method), so the pipeline is observable without a
# live collector.
OUTBOX: list[dict] = []

# Short ceilings so a slow/unreachable SIEM can't pile up background work.
_HTTP_TIMEOUT = 5.0
_SOCKET_TIMEOUT = 5.0

_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def _rank(severity: str | None) -> int:
    return _SEVERITY_RANK.get((severity or "info").lower(), 0)


@dataclass(frozen=True)
class SiemConfig:
    """Immutable snapshot of the routing config (no secret — token is env-only)."""

    enabled: bool = False
    methods: frozenset[str] = field(default_factory=frozenset)
    min_severity: str = "info"
    file_path: str = ""
    syslog_host: str = "localhost"
    syslog_port: int = 514
    syslog_protocol: str = "UDP"
    syslog_facility: int = 13
    http_endpoint: str = ""
    http_verify_tls: bool = True


# In-memory routing snapshot; a disabled default until the row is loaded at startup.
_config: SiemConfig = SiemConfig()


def get_config() -> SiemConfig:
    return _config


def set_config(cfg: SiemConfig) -> None:
    global _config
    _config = cfg


async def emit(event: dict, cfg: SiemConfig) -> None:
    """Dispatch one audit event dict to every enabled forwarder.

    No-ops when disabled or below the configured minimum severity. Each sink is
    isolated — one failing sink never stops the others and never propagates.
    """
    if not cfg.enabled:
        return
    if _rank(event.get("severity")) < _rank(cfg.min_severity):
        return

    OUTBOX.append(event)
    methods = cfg.methods
    if "file" in methods and cfg.file_path:
        await _safe(_emit_file, event, cfg)
    if "syslog" in methods:
        await _safe(_emit_syslog, event, cfg)
    if "http" in methods:
        await _safe(_emit_http, event, cfg)


async def _safe(fn, *args) -> None:
    try:
        await fn(*args)
    except Exception:  # noqa: BLE001 — a failing sink must never break the caller
        logger.exception("audit SIEM emit failed via %s", fn.__name__)


def _line(event: dict) -> str:
    return json.dumps(event, separators=(",", ":"), sort_keys=True, default=str)


async def _emit_file(event: dict, cfg: SiemConfig) -> None:
    line = _line(event)

    def _write() -> None:
        with open(cfg.file_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    await asyncio.to_thread(_write)


async def _emit_syslog(event: dict, cfg: SiemConfig) -> None:
    # RFC 5424: <PRI>VERSION TIMESTAMP HOSTNAME APP-NAME PROCID MSGID STRUCTURED MSG
    # severity numeric per RFC 5424 (INFO=6, WARNING=4, CRITICAL=2) combined with
    # the configured facility into the PRI value.
    sev = {"info": 6, "warning": 4, "critical": 2}.get((event.get("severity") or "info").lower(), 6)
    pri = cfg.syslog_facility * 8 + sev
    ts = datetime.now(UTC).isoformat()
    host = socket.gethostname() or "-"
    msg = f"<{pri}>1 {ts} {host} iceberg-ttx - audit - {_line(event)}"
    data = msg.encode("utf-8")
    kind = socket.SOCK_STREAM if cfg.syslog_protocol.upper() == "TCP" else socket.SOCK_DGRAM

    def _send() -> None:
        with socket.socket(socket.AF_INET, kind) as sock:
            sock.settimeout(_SOCKET_TIMEOUT)
            sock.connect((cfg.syslog_host, cfg.syslog_port))
            if kind == socket.SOCK_STREAM:
                sock.sendall(data + b"\n")
            else:
                sock.send(data)

    await asyncio.to_thread(_send)


async def _emit_http(event: dict, cfg: SiemConfig) -> None:
    if not cfg.http_endpoint:
        return
    token = settings.siem_http_token  # SECRET: env-only, never persisted/logged
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=cfg.http_verify_tls) as client:
        resp = await client.post(cfg.http_endpoint, content=_line(event), headers=headers)
        resp.raise_for_status()
