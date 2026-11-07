"""SQLite persistence for everything that must survive a process restart:
workflow state, proposals/approvals, the booking-intent ledger, memories,
conversation history and the proactive-notification outbox."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
  session_id TEXT PRIMARY KEY, user_id TEXT, data TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, material TEXT, material_hash TEXT,
  context TEXT, status TEXT, shown_turn INTEGER, created_at TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT, material_hash TEXT,
  user_message TEXT, turn INTEGER, created_at TEXT, consumed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS intents (
  idem_key TEXT PRIMARY KEY, proposal_id TEXT, session_id TEXT, kind TEXT, material TEXT,
  status TEXT, result TEXT, attempts INTEGER DEFAULT 0, updated_at TEXT);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, key TEXT, value TEXT,
  source TEXT, confirmed INTEGER, status TEXT, evidence TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, user_id TEXT, role TEXT,
  text TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, kind TEXT, text TEXT, ts TEXT,
  delivered INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS known_bookings (
  booking_id TEXT PRIMARY KEY, user_id TEXT, data TEXT, status TEXT, handled TEXT);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit: every write is durable
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def x(self, sql: str, *args: Any) -> int:
        cur = self.db.execute(sql, args)
        return cur.lastrowid

    # workflows ---------------------------------------------------------
    def load_workflow(self, session_id: str) -> dict | None:
        r = self.one("SELECT data FROM workflows WHERE session_id=?", session_id)
        return json.loads(r["data"]) if r else None

    def save_workflow(self, session_id: str, user_id: str, data: dict, ts: str) -> None:
        self.x("INSERT INTO workflows(session_id,user_id,data,updated_at) VALUES(?,?,?,?) "
               "ON CONFLICT(session_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
               session_id, user_id, json.dumps(data), ts)

    # messages ----------------------------------------------------------
    def add_message(self, session_id: str, user_id: str, role: str, text: str, ts: str) -> int:
        return self.x("INSERT INTO messages(session_id,user_id,role,text,ts) VALUES(?,?,?,?,?)",
                      session_id, user_id, role, text, ts)

    def messages(self, user_id: str, limit: int | None = None) -> list[sqlite3.Row]:
        rows = self.q("SELECT * FROM messages WHERE user_id=? ORDER BY id DESC" +
                      (f" LIMIT {int(limit)}" if limit else ""), user_id)
        return list(reversed(rows))

    def count_messages(self, user_id: str) -> int:
        return self.one("SELECT COUNT(*) c FROM messages WHERE user_id=?", user_id)["c"]

    # outbox ------------------------------------------------------------
    def push_outbox(self, user_id: str, kind: str, text: str, ts: str) -> int:
        return self.x("INSERT INTO outbox(user_id,kind,text,ts) VALUES(?,?,?,?)", user_id, kind, text, ts)

    def outbox(self, user_id: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM outbox WHERE user_id=? ORDER BY id", user_id)
