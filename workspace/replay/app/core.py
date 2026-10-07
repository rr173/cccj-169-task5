"""Core domain primitives: ids, canonical JSON, report signatures, errors."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3

from .db import now_iso


class ApiError(Exception):
    """Domain/HTTP error.  ``status`` is the HTTP status code to return."""

    def __init__(self, status: int, error: str, detail: str | None = None):
        super().__init__(detail or error)
        self.status = status
        self.error = error
        self.detail = detail

    def to_body(self) -> dict:
        body = {"error": self.error}
        if self.detail:
            body["detail"] = self.detail
        return body


# ---------------------------------------------------------------------------
# Deterministic JSON / hashing
# ---------------------------------------------------------------------------

def canonical(obj) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Report signatures (per-device HMAC key)
# ---------------------------------------------------------------------------

def new_device_secret() -> str:
    return secrets.token_hex(32)


def sign_device_payload(secret: str, payload: dict) -> str:
    """HMAC-SHA256 over the canonical payload, keyed by the device secret.

    签名覆盖 payload 全部字段（含 device_id / version_id），因此换设备
    加载或改动任何字段都会验签失败。
    """
    return hmac.new(secret.encode("utf-8"), canonical(payload).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify_device_payload(secret: str, payload: dict, signature: str) -> bool:
    expected = sign_device_payload(secret, payload)
    return hmac.compare_digest(expected, signature or "")


# ---------------------------------------------------------------------------
# Identifier generation (per-day sequence, like the sibling platform)
# ---------------------------------------------------------------------------

def _day_seq(conn: sqlite3.Connection, kind: str) -> str:
    day = now_iso()[:10].replace("-", "")
    key = f"seq:{kind}:{day}"
    conn.execute(
        "INSERT INTO keys_meta(k, v) VALUES (?, '1') "
        "ON CONFLICT(k) DO UPDATE SET v = CAST(v AS INTEGER) + 1", (key,))
    n = conn.execute("SELECT v FROM keys_meta WHERE k = ?", (key,)).fetchone()
    return f"{int(n['v']):03d}"


def new_device_id(conn: sqlite3.Connection) -> str:
    return f"DEV-{_day_seq(conn, 'device')}"


def new_batch_id(conn: sqlite3.Connection) -> str:
    return f"BAT-{_day_seq(conn, 'batch')}"


def new_version_id(conn: sqlite3.Connection) -> str:
    return f"VER-{_day_seq(conn, 'version')}"


def new_finding_id(conn: sqlite3.Connection) -> str:
    return f"FND-{_day_seq(conn, 'finding')}"


def new_report_id(conn: sqlite3.Connection) -> str:
    return f"REP-{_day_seq(conn, 'report')}"


def new_seal_id(conn: sqlite3.Connection) -> str:
    return f"SEA-{_day_seq(conn, 'seal')}"


def log_event(conn: sqlite3.Connection, device_id: str | None,
              event_type: str, detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO events(device_id, event_type, detail, created_at) "
        "VALUES (?, ?, ?, ?)",
        (device_id, event_type,
         canonical(detail) if detail is not None else None, now_iso()))
