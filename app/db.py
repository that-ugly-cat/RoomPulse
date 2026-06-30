"""SQLite layer per RoomPulse.

Schema con separazione template/esecuzione:
- presentation : la deck riusabile, con join_code FISSO e puntatore al run corrente
- slide        : domanda-template appartenente a una presentation
- run          : una sessione live della deck (active_slide + ciclo di vita)
- run_slide    : stato per-run-per-slide (pending|open|closed|revealed), creato lazy
- response     : risposta agganciata a (run_id, slide_id)
"""

import os
import sqlite3
import uuid
import secrets
from datetime import datetime, timezone
from pathlib import Path

# percorso del DB: di default accanto al codice, sovrascrivibile con RP_DB (utile in Docker)
DB_PATH = Path(os.environ.get("RP_DB") or (Path(__file__).resolve().parent.parent / "roompulse.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS presentation (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    owner         TEXT NOT NULL DEFAULT 'spit',
    join_code     TEXT UNIQUE NOT NULL,   -- fisso: ciò che digita il pubblico
    active_run_id TEXT,                    -- puntatore al run corrente
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slide (
    id              TEXT PRIMARY KEY,
    presentation_id TEXT NOT NULL REFERENCES presentation(id),
    ord             INTEGER NOT NULL,
    type            TEXT NOT NULL,         -- mc | scale | ... (v1: mc, scale)
    question        TEXT NOT NULL,
    config          TEXT NOT NULL DEFAULT '{}',
    pair_id         TEXT,                  -- pre/post (post-v1, già nello schema)
    UNIQUE (presentation_id, ord)
);

CREATE TABLE IF NOT EXISTS run (
    id              TEXT PRIMARY KEY,
    presentation_id TEXT NOT NULL REFERENCES presentation(id),
    label           TEXT,
    active_slide_id TEXT,                  -- quale slide è live ORA in questo run
    started_at      TEXT NOT NULL,
    ended_at        TEXT
);

CREATE TABLE IF NOT EXISTS run_slide (
    run_id   TEXT NOT NULL REFERENCES run(id),
    slide_id TEXT NOT NULL REFERENCES slide(id),
    state    TEXT NOT NULL DEFAULT 'pending',  -- pending|open|closed|revealed
    PRIMARY KEY (run_id, slide_id)
);

CREATE TABLE IF NOT EXISTS response (
    id                TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES run(id),
    slide_id          TEXT NOT NULL REFERENCES slide(id),
    participant_token TEXT NOT NULL,
    payload           TEXT NOT NULL,        -- JSON poliforme per-tipo
    status            TEXT NOT NULL DEFAULT 'visible',  -- visible|hidden|flagged
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_response_run_slide ON response(run_id, slide_id);

CREATE TABLE IF NOT EXISTS qa_vote (
    run_id      TEXT NOT NULL,
    response_id TEXT NOT NULL REFERENCES response(id),
    token       TEXT NOT NULL,           -- un upvote per token per domanda
    PRIMARY KEY (response_id, token)
);

CREATE TABLE IF NOT EXISTS mc_option (
    id         TEXT PRIMARY KEY,        -- opzione mc aggiunta da un partecipante (scope: run)
    run_id     TEXT NOT NULL,
    slide_id   TEXT NOT NULL,
    label      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    name          TEXT,
    is_active     INTEGER NOT NULL DEFAULT 1,
    api_key       TEXT,                   -- chiave API Claude per-utente (clustering)
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    slide_id     TEXT NOT NULL,
    kind         TEXT NOT NULL,           -- 'claim' | 'arg'
    label        TEXT NOT NULL,
    ord          INTEGER NOT NULL,
    generated_at TEXT NOT NULL
);
"""

# colonne aggiunte a tabelle esistenti (migrazione idempotente per DB già creati)
_MIGRATIONS = [
    "ALTER TABLE user ADD COLUMN api_key TEXT",
    "ALTER TABLE response ADD COLUMN claim_cluster_id TEXT",
    "ALTER TABLE response ADD COLUMN arg_cluster_id TEXT",
    "ALTER TABLE response ADD COLUMN cluster_id TEXT",  # clustering a un asse (open text)
]


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # colonna già presente


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def new_join_code(conn) -> str:
    """Codice numerico a 5 cifre, unico tra le presentation."""
    while True:
        code = str(secrets.randbelow(90000) + 10000)
        if not conn.execute(
            "SELECT 1 FROM presentation WHERE join_code = ?", (code,)
        ).fetchone():
            return code
