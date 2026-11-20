"""SQLite storage for one business.

Only system tables live here. Business tables (customers, visits, pets, ...) are created
at runtime by the schema engine (schema.py) from a vertical pack or by the assistant.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS entity_types (
    name TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    spec TEXT NOT NULL                       -- JSON entity spec (fields, labels, synonyms)
);
CREATE TABLE IF NOT EXISTS aliases (         -- name/phone variants -> one record
    entity TEXT NOT NULL,
    key TEXT NOT NULL,                       -- 'p:+40722...' | 'n:ioana popescu'
    record_id INTEGER NOT NULL,
    PRIMARY KEY (entity, key)
);

CREATE TABLE IF NOT EXISTS raw_files (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                      -- table | text | records
    sha256 TEXT NOT NULL UNIQUE,
    content_text TEXT NOT NULL,
    entity TEXT,
    report TEXT,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS mapping_memory (
    signature TEXT PRIMARY KEY,
    entity TEXT NOT NULL,
    mapping TEXT NOT NULL,
    date_order TEXT
);

CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY,
    source_file_id INTEGER,
    section TEXT NOT NULL,
    text TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'file',     -- file | chat | history | photo
    kind TEXT,                               -- goal | policy | fact | staff | note
    quote TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    decided_on TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    expected_outcome TEXT,
    review_on TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    quote TEXT
);
CREATE TABLE IF NOT EXISTS contact_log (
    id INTEGER PRIMARY KEY,
    party_id INTEGER NOT NULL,
    contacted_on TEXT NOT NULL,
    channel TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS schema_changes (
    id INTEGER PRIMARY KEY,
    op TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,                    -- applied | undone | rejected
    origin TEXT NOT NULL,                    -- pack | import | assistant | user
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT
);

CREATE TABLE IF NOT EXISTS design_history (
    id INTEGER PRIMARY KEY,
    spec TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS metric_defs (
    name TEXT PRIMARY KEY,
    label TEXT NOT NULL,                     -- JSON {en,fr,ro,de}
    sql TEXT NOT NULL,                       -- SELECT returning one number; may use :start :end
    format TEXT NOT NULL DEFAULT 'number'    -- number | money | percent
);
CREATE TABLE IF NOT EXISTS history_queue (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,                    -- claude | chatgpt
    conversation TEXT,
    sent_at TEXT,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'   -- pending | sent | done
);
CREATE TABLE IF NOT EXISTS tasks (            -- promises, follow-ups, meetings, money to collect
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    due TEXT,                                -- YYYY-MM-DD
    due_time TEXT,                           -- HH:MM
    party_id INTEGER,
    assignee TEXT,
    amount INTEGER,                          -- minor units, e.g. money a customer promised to pay
    status TEXT NOT NULL DEFAULT 'open',     -- open | done
    quote TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS recurring_costs (  -- rent, salaries, subscriptions: counted every month automatically
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    amount INTEGER NOT NULL,
    start_month TEXT NOT NULL,               -- YYYY-MM
    end_month TEXT,                          -- YYYY-MM (inclusive) or NULL = ongoing
    quote TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS trash (            -- snapshots that make destructive changes undoable
    change_id INTEGER PRIMARY KEY,
    data TEXT NOT NULL                       -- JSON {"tables": {table: [rows]}, "aliases": [...], "spec": ...}
);
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY,
    tool TEXT NOT NULL,
    at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- People layer (people.py) --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vocab (            -- fact kinds and relation types (pack + added from chat)
    type TEXT NOT NULL,                      -- fact_kind | relation_type
    name TEXT NOT NULL,
    spec TEXT NOT NULL,                      -- JSON {label, polarity?, sensitivity?, synonyms, symmetric?}
    origin TEXT NOT NULL DEFAULT 'pack',
    PRIMARY KEY (type, name)
);
CREATE TABLE IF NOT EXISTS facts (            -- typed, sourced, dated knowledge about a record (usually a person)
    id INTEGER PRIMARY KEY,
    entity TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    key TEXT,                                -- comparable aspect, e.g. 'budget', 'floor' (contradiction check)
    value TEXT NOT NULL,
    value_json TEXT,
    polarity TEXT NOT NULL DEFAULT 'neutral',  -- likes | dislikes | must | must_not | neutral
    sensitivity TEXT NOT NULL DEFAULT 'normal', -- normal | private | special
    confidence TEXT NOT NULL DEFAULT 'confirmed', -- confirmed | stated | inferred
    source TEXT,
    quote TEXT,
    observed_at TEXT,
    valid_until TEXT,
    superseded_by INTEGER,                   -- facts change; the old one is kept, never overwritten
    _created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_facts_record ON facts(entity, record_id);
CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY,
    a_entity TEXT NOT NULL,
    a_id INTEGER NOT NULL,
    b_entity TEXT NOT NULL,
    b_id INTEGER NOT NULL,
    type TEXT NOT NULL,                      -- a <type> b, e.g. a parent_of b
    since TEXT,
    until TEXT,
    note TEXT,
    source TEXT,
    quote TEXT,
    _created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_relations_a ON relations(a_entity, a_id);
CREATE INDEX IF NOT EXISTS ix_relations_b ON relations(b_entity, b_id);
"""
# Columns of system tables that query_data / custom metrics may only read through masking views.
MASKED_COLUMNS = {"facts": {"value", "value_json", "quote"}}


class TxConnection(sqlite3.Connection):
    """While `hold` is set (during one tool call), inner commits are deferred so the call is all-or-nothing."""
    hold = False

    def commit(self) -> None:
        if not self.hold:
            super().commit()


def _prepare(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    from .schema import rebuild_views  # role views exist (possibly empty) from the start
    rebuild_views(conn)
    conn.commit()
    return conn


def connect(path: str | Path, shared: bool = False) -> sqlite3.Connection:
    """shared=True allows use from worker threads; callers must serialize access."""
    return _prepare(sqlite3.connect(str(path), check_same_thread=not shared, factory=TxConnection))


def open_memory(data: Optional[bytes] = None) -> TxConnection:
    """In-memory database loaded from a file's bytes (None = new empty memory). Never touches disk."""
    conn = sqlite3.connect(":memory:", check_same_thread=False, factory=TxConnection)
    return load_into(conn, data)  # type: ignore[return-value]


def load_into(conn: sqlite3.Connection, data: Optional[bytes]) -> sqlite3.Connection:
    """Replace the content of an in-memory connection in place, so objects holding `conn` stay valid."""
    conn.rollback()
    conn.deserialize(data if data is not None else _empty_bytes())
    return _prepare(conn)


def _empty_bytes() -> bytes:
    blank = sqlite3.connect(":memory:")
    try:
        return blank.serialize()
    finally:
        blank.close()


def readonly_copy(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Read-only snapshot of an in-memory database for query_data and custom metrics."""
    ro = sqlite3.connect(":memory:", check_same_thread=False)
    ro.deserialize(conn.serialize())
    return _guard_readonly(ro)


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    """Read-only connection to a database file that only allows SELECT statements."""
    return _guard_readonly(sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True))


def _guard_readonly(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    masked = {t: set(cols) for t, cols in MASKED_COLUMNS.items()}
    import json
    for r in conn.execute("SELECT name, spec FROM entity_types"):       # fields marked sensitive: true
        for f in json.loads(r["spec"])["fields"]:
            if f.get("sensitive"):
                masked.setdefault(f"e_{r['name']}", set()).add(f"{f['name']}_id" if f["type"] == "ref" else f["name"])

    def authorizer(action, table, column, _db, via_view):
        if action == sqlite3.SQLITE_READ and column in masked.get(table, ()) and not (via_view or "").startswith("v_"):
            return sqlite3.SQLITE_IGNORE        # read as NULL; masked values are exposed only through v_facts
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    conn.set_authorizer(authorizer)
    return conn


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))
