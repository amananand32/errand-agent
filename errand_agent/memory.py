"""Persistent user memory with provenance.

* ``explicit``  - the user said it (verified against their own words). Eligible for context.
* ``inferred``  - derived by application code from behaviour. Never sent to the model
                  until the user confirms it (then ``confirmed=1``).
Status: active | superseded | forgotten | rejected.
Forgetting redacts the stored value *and* scrubs it from stored conversation history,
so it cannot leak back into model context through old transcripts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .store import Store

CORE_KEYS = {"area.preferred", "time.earliest", "time.latest", "language"}
KEY_RE = re.compile(r"^[a-z_]+(\.[a-z_]+)*$")

FRIENDLY = {
    "area.preferred": "Preferred area",
    "time.earliest": "Earliest acceptable visit time",
    "time.latest": "Latest acceptable visit time",
    "language": "Preferred language",
}


@dataclass
class Memory:
    id: int
    key: str
    value: str
    source: str
    confirmed: bool
    status: str
    evidence: str
    updated_at: str

    @property
    def eligible(self) -> bool:
        return self.status == "active" and (self.source == "explicit" or self.confirmed)

    def label(self) -> str:
        if self.source == "explicit":
            return "explicit"
        return "inferred, confirmed" if self.confirmed else "inferred, NOT confirmed"


def _row(r) -> Memory:
    return Memory(r["id"], r["key"], r["value"], r["source"], bool(r["confirmed"]), r["status"],
                  r["evidence"] or "", r["updated_at"])


class MemoryStore:
    def __init__(self, store: Store):
        self.s = store

    def add(self, user_id: str, key: str, value: str, source: str, ts: str,
            confirmed: bool = False, evidence: str = "") -> Memory:
        if not KEY_RE.match(key):
            raise ValueError(f"bad memory key {key!r}")
        if source not in ("explicit", "inferred"):
            raise ValueError("source must be explicit or inferred")
        if source == "explicit":
            # an explicit statement supersedes anything (explicit or inferred) under the key
            self.s.x("UPDATE memories SET status='superseded', updated_at=? WHERE user_id=? AND key=? "
                     "AND status='active'", ts, user_id, key)
        else:
            # never let an inference override something the user told us, or re-create
            # a fact the user forgot/rejected
            blocker = self.s.one("SELECT id FROM memories WHERE user_id=? AND key=? AND "
                                 "((status='active' AND source='explicit') OR status IN ('forgotten','rejected'))",
                                 user_id, key)
            if blocker:
                return self.get(blocker["id"])
            self.s.x("UPDATE memories SET status='superseded', updated_at=? WHERE user_id=? AND key=? "
                     "AND status='active' AND source='inferred'", ts, user_id, key)
        mid = self.s.x("INSERT INTO memories(user_id,key,value,source,confirmed,status,evidence,created_at,"
                       "updated_at) VALUES(?,?,?,?,?,?,?,?,?)", user_id, key, value, source,
                       int(confirmed or source == "explicit"), "active", evidence, ts, ts)
        return self.get(mid)

    def get(self, mid: int) -> Memory | None:
        r = self.s.one("SELECT * FROM memories WHERE id=?", mid)
        return _row(r) if r else None

    def active(self, user_id: str, key: str) -> Memory | None:
        r = self.s.one("SELECT * FROM memories WHERE user_id=? AND key=? AND status='active' "
                       "ORDER BY id DESC", user_id, key)
        return _row(r) if r else None

    def value(self, user_id: str, key: str) -> str | None:
        """Value only if it is eligible to be *used* (explicit or confirmed)."""
        m = self.active(user_id, key)
        return m.value if m and m.eligible else None

    def all(self, user_id: str, include_inactive: bool = False) -> list[Memory]:
        sql = "SELECT * FROM memories WHERE user_id=?" + ("" if include_inactive else " AND status='active'")
        return [_row(r) for r in self.s.q(sql + " ORDER BY key, id", user_id)]

    def eligible(self, user_id: str) -> list[Memory]:
        return [m for m in self.all(user_id) if m.eligible]

    def pending_inferences(self, user_id: str) -> list[Memory]:
        return [m for m in self.all(user_id) if m.source == "inferred" and not m.confirmed]

    def confirm(self, mid: int, ts: str) -> None:
        self.s.x("UPDATE memories SET confirmed=1, updated_at=? WHERE id=?", ts, mid)

    def reject(self, mid: int, ts: str) -> None:
        self.s.x("UPDATE memories SET status='rejected', value='[rejected]', updated_at=? WHERE id=?", ts, mid)

    def forget(self, user_id: str, key: str, ts: str) -> list[str]:
        rows = self.s.q("SELECT * FROM memories WHERE user_id=? AND key=? AND status IN "
                        "('active','superseded')", user_id, key)
        values = sorted({r["value"] for r in rows if r["value"] and not r["value"].startswith("[")},
                        key=len, reverse=True)
        self.s.x("UPDATE memories SET status='forgotten', value='[forgotten]', evidence='', updated_at=? "
                 "WHERE user_id=? AND key=? AND status IN ('active','superseded')", ts, user_id, key)
        self.scrub_history(user_id, values)
        if not rows:
            # tombstone so that later inference cannot silently re-learn it
            self.s.x("INSERT INTO memories(user_id,key,value,source,confirmed,status,evidence,created_at,"
                     "updated_at) VALUES(?,?,?,?,?,?,?,?,?)", user_id, key, "[forgotten]", "explicit", 1,
                     "forgotten", "", ts, ts)
        return values

    def scrub_history(self, user_id: str, values: list[str]) -> None:
        for v in values:
            if len(v) < 3:
                continue
            pat = re.compile(re.escape(v), re.IGNORECASE)
            for r in self.s.q("SELECT id, text FROM messages WHERE user_id=?", user_id):
                if pat.search(r["text"]):
                    self.s.x("UPDATE messages SET text=? WHERE id=?", pat.sub("[forgotten]", r["text"]), r["id"])

    # relevance ---------------------------------------------------------
    @staticmethod
    def relevance(m: Memory, service: str | None) -> int:
        if m.key in CORE_KEYS:
            return 3
        parts = m.key.split(".")
        if service and parts[-1] == service:
            return 3
        if parts[0] in ("technician", "budget") and service is None:
            return 1
        if parts[0] in ("home", "payment", "access_notes"):
            return 1
        return 0
