"""SQLite layer per RoomPulse.

Schema con separazione template/esecuzione:
- presentation : la deck riusabile, con join_code FISSO e puntatore al run corrente
- slide        : domanda-template appartenente a una presentation
- run          : una sessione live della deck (active_slide + ciclo di vita)
- run_slide    : stato per-run-per-slide (pending|open|closed|revealed), creato lazy
- response     : risposta agganciata a (run_id, slide_id)
- presence     : heartbeat per token dentro un run (chi e' in sala ORA)
- carry_assign : argstep in modo peer, chi eredita il claim di chi (stabile per token)
"""

import os
import sqlite3
import time
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
    presenter_notes TEXT NOT NULL DEFAULT '',  -- visibili solo al presenter, mai al pubblico
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

CREATE TABLE IF NOT EXISTS presence (
    run_id       TEXT NOT NULL,           -- chi e' in sala ORA: un heartbeat per token,
    token        TEXT NOT NULL,           -- scritto dal poll del pubblico, non dal voto
    last_seen_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, token)
);

CREATE INDEX IF NOT EXISTS idx_presence_seen ON presence(run_id, last_seen_ms);

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
    api_key       TEXT,                   -- chiave API Claude per-utente (tier free)
    role          TEXT NOT NULL DEFAULT 'free',  -- free | full | admin
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id            TEXT PRIMARY KEY,       -- consumo chiave centrale (tier full/admin), per cost tracking
    user_id       TEXT NOT NULL,
    run_id        TEXT,
    slide_id      TEXT,
    kind          TEXT,                   -- argpoll | opentext
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL,
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

CREATE TABLE IF NOT EXISTS carry_assign (
    run_id             TEXT NOT NULL,      -- assegnazione peer per argstep: chi obietta a chi
    slide_id           TEXT NOT NULL,      -- (stabile per token, così il carry non balla fra un poll e l'altro)
    token              TEXT NOT NULL,
    source_response_id TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    PRIMARY KEY (run_id, slide_id, token)
);

CREATE INDEX IF NOT EXISTS idx_carry_source ON carry_assign(run_id, slide_id, source_response_id);

CREATE TABLE IF NOT EXISTS moonshot_game (
    run_id        TEXT NOT NULL,
    slide_id      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'lobby',   -- lobby | running
    captain_token TEXT,
    total_crew    INTEGER NOT NULL DEFAULT 0,      -- congelato al launch (denominatore)
    started_at_ms INTEGER,                         -- epoch ms del lancio (schedule finestre)
    PRIMARY KEY (run_id, slide_id)
);

CREATE TABLE IF NOT EXISTS moonshot_player (
    run_id   TEXT NOT NULL,
    slide_id TEXT NOT NULL,
    token    TEXT NOT NULL,
    PRIMARY KEY (run_id, slide_id, token)
);

CREATE TABLE IF NOT EXISTS moonshot_boost (
    run_id     TEXT NOT NULL,
    slide_id   TEXT NOT NULL,
    window_idx INTEGER NOT NULL,
    token      TEXT NOT NULL,
    PRIMARY KEY (run_id, slide_id, window_idx, token)   -- un tap per token per finestra (dedup)
);
"""

# colonne aggiunte a tabelle esistenti (migrazione idempotente per DB già creati)
_MIGRATIONS = [
    "ALTER TABLE user ADD COLUMN api_key TEXT",
    "ALTER TABLE response ADD COLUMN claim_cluster_id TEXT",
    "ALTER TABLE response ADD COLUMN arg_cluster_id TEXT",
    "ALTER TABLE response ADD COLUMN cluster_id TEXT",  # clustering a un asse (open text)
    "ALTER TABLE slide ADD COLUMN presenter_notes TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE user ADD COLUMN role TEXT NOT NULL DEFAULT 'free'",
    "ALTER TABLE response ADD COLUMN source_response_id TEXT",  # argstep: la risposta da cui eredita
    "ALTER TABLE user ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0",  # invalida le sessioni al cambio password
    # il subject immutabile con cui un gate SSO a monte conosce questa persona.
    # NULL per chi ha sempre fatto login qui: non e' l'email di proposito, perche'
    # l'email cambia con l'istituzione e questo no
    "ALTER TABLE user ADD COLUMN borant_sub TEXT",
    "CREATE UNIQUE INDEX ix_user_borant_sub ON user (borant_sub)",
]


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # letture e scritture non si bloccano (boost + poll)
        conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # colonna già presente
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_ms() -> int:
    return int(time.time() * 1000)


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
