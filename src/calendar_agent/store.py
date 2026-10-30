"""Local SQLite store: proposals awaiting the owner, their outcomes, and small state.

Every proposal the owner approves, edits or rejects stays here; later triage
uses these decisions as examples.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime

from .config import DATA_DIR, ensure_data_dirs

DB_FILE = DATA_DIR / "agent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,              -- event | reminder
  item TEXT NOT NULL,              -- JSON, see items.Item
  status TEXT NOT NULL,            -- pending | editing | applied | rejected | undone | failed
  source TEXT NOT NULL,            -- telegram | agent | ...
  source_text TEXT NOT NULL DEFAULT '',
  chat_id INTEGER,
  message_id INTEGER,
  result TEXT,                     -- JSON: where it was written
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS proposals_message ON proposals(chat_id, message_id);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def connect() -> sqlite3.Connection:
    ensure_data_dirs()
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def put(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    conn.commit()


def add_proposal(conn, kind: str, item: dict, source: str, source_text: str) -> str:
    pid = uuid.uuid4().hex[:12]
    t = now()
    conn.execute(
        "INSERT INTO proposals(id,kind,item,status,source,source_text,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            pid,
            kind,
            json.dumps(item, ensure_ascii=False),
            "pending",
            source,
            source_text,
            t,
            t,
        ),
    )
    conn.commit()
    return pid


def proposal(conn, pid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()


def proposal_by_message(conn, chat_id: int, message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM proposals WHERE chat_id=? AND message_id=?",
        (chat_id, message_id),
    ).fetchone()


def update_proposal(conn, pid: str, **fields) -> None:
    for key in ("item", "result"):
        if (
            key in fields
            and not isinstance(fields[key], str)
            and fields[key] is not None
        ):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    fields["updated_at"] = now()
    cols = ",".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE proposals SET {cols} WHERE id=?", (*fields.values(), pid))
    conn.commit()


def recent_decisions(conn, limit: int = 8) -> list[dict]:
    """The owner's latest decided proposals, newest first, for triage examples."""
    rows = conn.execute(
        "SELECT kind,item,status,source_text FROM proposals WHERE status IN ('applied','rejected','undone') ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "kind": r["kind"],
            "item": json.loads(r["item"]),
            "decision": r["status"],
            "source": r["source_text"][:300],
        }
        for r in rows
    ]
