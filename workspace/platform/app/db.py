"""SQLite persistence layer.

All state for the platform lives in a single SQLite database.  The module
exposes connection management plus the full schema.  Transactions are the
single source of atomicity for state transitions (see ``guards.py``).
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("PRINT_TRACE_DB", "/workspace/platform/data/trace.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS keys_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    part_name       TEXT NOT NULL,
    customer        TEXT NOT NULL,
    snapshot_hash   TEXT NOT NULL,
    manifest        TEXT NOT NULL,
    total_layers    INTEGER NOT NULL,
    model_version   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    revised_from    TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    material_closed_at TEXT
);

CREATE TABLE IF NOT EXISTS material_declarations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES jobs(job_id),
    batch_code      TEXT NOT NULL,
    material_type   TEXT NOT NULL,
    grade           TEXT NOT NULL,
    spool_id        TEXT,
    supplier        TEXT,
    declared_at     TEXT NOT NULL,
    UNIQUE(job_id, batch_code)
);

CREATE TABLE IF NOT EXISTS material_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES jobs(job_id),
    batch_code  TEXT NOT NULL,
    entry_type  TEXT NOT NULL CHECK(entry_type IN
                  ('charge','return','consume')),
    weight_g    REAL NOT NULL,
    layer_no    INTEGER,
    session_id  TEXT,
    note        TEXT,
    event_id    TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL REFERENCES jobs(job_id),
    kind        TEXT NOT NULL CHECK(kind IN ('start','resume')),
    device_id   TEXT NOT NULL,
    began_at    TEXT NOT NULL,
    ended_at    TEXT,
    end_reason  TEXT CHECK(end_reason IN
                  ('completed','power_loss','aborted') OR end_reason IS NULL),
    note        TEXT
);

CREATE TABLE IF NOT EXISTS layers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              TEXT NOT NULL REFERENCES jobs(job_id),
    layer_no            INTEGER NOT NULL,
    receipt_id          TEXT NOT NULL UNIQUE,
    snapshot_hash       TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    device_id           TEXT NOT NULL,
    material_usages     TEXT NOT NULL,
    env_temp_c          REAL NOT NULL,
    env_humidity_pct    REAL NOT NULL,
    energy_wh           REAL,
    status              TEXT NOT NULL DEFAULT 'ok'
                            CHECK(status IN ('ok','anomaly','superseded')),
    recorded_at         TEXT NOT NULL,
    UNIQUE(job_id, layer_no)
);

CREATE TABLE IF NOT EXISTS anomalies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES jobs(job_id),
    layer_no    INTEGER,
    code        TEXT NOT NULL,
    detail      TEXT,
    status      TEXT NOT NULL DEFAULT 'open'
                    CHECK(status IN ('open','resolved')),
    opened_at   TEXT NOT NULL,
    resolved_at TEXT,
    resolution  TEXT,
    UNIQUE(job_id, layer_no, code)
);

CREATE TABLE IF NOT EXISTS inspections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES jobs(job_id),
    inspector       TEXT NOT NULL,
    method          TEXT NOT NULL,
    report_ref      TEXT,
    result          TEXT NOT NULL CHECK(result IN ('pass','fail','hold')),
    findings        TEXT,
    snapshot_hash   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    serial      TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
    state       TEXT NOT NULL CHECK(state IN ('released','scrapped')),
    state_at    TEXT NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS product_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    serial      TEXT NOT NULL REFERENCES products(serial),
    event_type  TEXT NOT NULL CHECK(event_type IN
                  ('create','release','scrap')),
    detail      TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE(serial, event_type)
);

CREATE TABLE IF NOT EXISTS certificates (
    cert_id     TEXT PRIMARY KEY,
    serial      TEXT NOT NULL REFERENCES products(serial),
    job_id      TEXT NOT NULL REFERENCES jobs(job_id),
    payload     TEXT NOT NULL,
    signature   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT,
    event_type  TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_layers_job ON layers(job_id);
CREATE INDEX IF NOT EXISTS idx_anomalies_job ON anomalies(job_id);
CREATE INDEX IF NOT EXISTS idx_ledger_job ON material_ledger(job_id);
CREATE INDEX IF NOT EXISTS idx_sessions_job ON sessions(job_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
