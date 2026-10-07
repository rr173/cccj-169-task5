"""Core domain primitives: ids, canonical hashing, signatures, errors."""
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


def snapshot_hash(manifest: dict) -> str:
    """Content hash of the frozen job manifest (model + process + limits)."""
    return hashlib.sha256(canonical(manifest).encode("utf-8")).hexdigest()


def get_server_secret(conn: sqlite3.Connection) -> bytes:
    """HMAC secret for certificate signatures, generated once per database."""
    row = conn.execute(
        "SELECT v FROM keys_meta WHERE k = 'cert_secret'").fetchone()
    if row:
        return row["v"].encode("utf-8")
    secret = secrets.token_hex(32)
    conn.execute(
        "INSERT INTO keys_meta(k, v) VALUES ('cert_secret', ?)", (secret,))
    return secret.encode("utf-8")


def sign_payload(conn: sqlite3.Connection, payload: dict) -> str:
    secret = get_server_secret(conn)
    return hmac.new(secret, canonical(payload).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify_payload(conn: sqlite3.Connection, payload: dict,
                   signature: str) -> bool:
    expected = sign_payload(conn, payload)
    return hmac.compare_digest(expected, signature or "")


# ---------------------------------------------------------------------------
# Identifier generation
# ---------------------------------------------------------------------------

def _day_seq(conn: sqlite3.Connection, kind: str) -> str:
    day = now_iso()[:10].replace("-", "")
    key = f"seq:{kind}:{day}"
    conn.execute(
        "INSERT INTO keys_meta(k, v) VALUES (?, '1') "
        "ON CONFLICT(k) DO UPDATE SET v = CAST(v AS INTEGER) + 1", (key,))
    n = conn.execute("SELECT v FROM keys_meta WHERE k = ?", (key,)).fetchone()
    return f"{int(n['v']):03d}"


def new_job_id(conn: sqlite3.Connection) -> str:
    return f"JOB-{_day_seq(conn, 'job')}"


def new_receipt_id(conn: sqlite3.Connection) -> str:
    return f"RCP-{_day_seq(conn, 'receipt')}"


def new_cert_id(conn: sqlite3.Connection) -> str:
    return f"CERT-{_day_seq(conn, 'cert')}"


def new_event_id(conn: sqlite3.Connection) -> str:
    return f"EVT-{_day_seq(conn, 'evt')}"


def log_event(conn: sqlite3.Connection, job_id: str | None,
              event_type: str, detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO events(job_id, event_type, detail, created_at) "
        "VALUES (?, ?, ?, ?)",
        (job_id, event_type,
         canonical(detail) if detail is not None else None, now_iso()))
