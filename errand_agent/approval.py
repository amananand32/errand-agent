"""Approval enforcement in application code.

The model can only *propose* consequential actions. A proposal records the exact
material details (provider, slot, price / booking id). The application renders
the confirmation card itself from the proposal record, and only a deterministic
parse of the user's *own next message* can approve it. Executors must call
``authorize()`` with the material details as they are at execution time; any
difference in provider, slot or price raises ``ApprovalError``.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .store import Store

MATERIAL_FIELDS = {
    "book": ("provider_id", "slot", "price"),
    "cancel": ("booking_id", "provider_id", "slot"),
    "reschedule": ("booking_id", "provider_id", "from_slot", "to_slot", "price"),
}

AFFIRM = {"yes", "y", "yeah", "yep", "yup", "confirm", "confirmed", "ok", "okay", "sure", "haan", "ha",
          "approve", "approved", "proceed", "correct", "absolutely", "definitely"}
FILLER = {"please", "pls", "go", "ahead", "do", "it", "that", "this", "one", "thanks", "thank", "you",
          "book", "cancel", "reschedule", "move", "sounds", "good", "great", "perfect", "fine", "lets",
          "let's", "theek", "hai", "the", "booking", "yes,"}
NEGATE = {"no", "nope", "nah", "not", "dont", "don't", "wait", "stop", "hold", "instead", "but",
          "change", "different", "another", "cheaper", "later", "nahi", "mat", "except", "rather"}


class ApprovalError(Exception):
    pass


def material_hash(kind: str, material: dict) -> str:
    fields = MATERIAL_FIELDS[kind]
    canon = json.dumps({"kind": kind, **{f: material.get(f) for f in fields}}, sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()


def classify_reply(text: str, ref: str | None = None) -> str:
    """'approve' | 'reject' | 'other'. Deliberately strict: anything that is not a
    plain confirmation (e.g. 'yes but at 11') is *not* an approval."""
    t = text.lower().strip()
    words = re.findall(r"[a-z0-9']+", t)
    if not words:
        return "other"
    codes = [w for w in words if re.fullmatch(r"[a-f0-9]{4}", w) and w not in AFFIRM]
    if codes and ref and any(c != ref.lower() for c in codes):
        return "other"  # a confirmation code for a different/older proposal
    rest = [w for w in words if w not in codes]
    if any(w in NEGATE for w in rest):
        only_neg = all(w in NEGATE or w in FILLER or w in {"thanks", "leave"} for w in rest)
        return "reject" if only_neg else "other"
    if any(w in AFFIRM for w in rest) and all(w in AFFIRM or w in FILLER for w in rest):
        return "approve"
    return "other"


@dataclass
class Proposal:
    id: str
    session_id: str
    kind: str
    material: dict
    material_hash: str
    context: dict
    status: str
    shown_turn: int
    reason: str | None

    @property
    def ref(self) -> str:
        return self.material_hash[:4].upper()


class ApprovalGate:
    def __init__(self, store: Store, tracer=None):
        self.s = store
        self.tracer = tracer

    def _ev(self, type_: str, **d) -> None:
        if self.tracer:
            self.tracer.event(type_, **d)

    def _load(self, r) -> Proposal:
        return Proposal(r["id"], r["session_id"], r["kind"], json.loads(r["material"]), r["material_hash"],
                        json.loads(r["context"] or "{}"), r["status"], r["shown_turn"], r["reason"])

    def get(self, pid: str) -> Proposal | None:
        r = self.s.one("SELECT * FROM proposals WHERE id=?", pid)
        return self._load(r) if r else None

    def pending(self, session_id: str) -> Proposal | None:
        r = self.s.one("SELECT * FROM proposals WHERE session_id=? AND status='pending' "
                       "ORDER BY created_at DESC, rowid DESC", session_id)
        return self._load(r) if r else None

    def latest(self, session_id: str) -> Proposal | None:
        r = self.s.one("SELECT * FROM proposals WHERE session_id=? ORDER BY rowid DESC", session_id)
        return self._load(r) if r else None

    def propose(self, session_id: str, kind: str, material: dict, context: dict, turn: int,
                ts: str) -> Proposal:
        for f in MATERIAL_FIELDS[kind]:
            if material.get(f) in (None, ""):
                raise ApprovalError(f"proposal missing material field {f}")
        h = material_hash(kind, material)
        old = self.pending(session_id)
        if old:
            self.invalidate(old.id, "superseded by a new proposal")
        n = self.s.one("SELECT COUNT(*) c FROM proposals")["c"] + 1
        pid = f"PRP-{n:04d}"
        self.s.x("INSERT INTO proposals(id,session_id,kind,material,material_hash,context,status,shown_turn,"
                 "created_at,reason) VALUES(?,?,?,?,?,?,?,?,?,?)", pid, session_id, kind,
                 json.dumps(material), h, json.dumps(context), "pending", turn, ts, None)
        self._ev("approval_requested", proposal_id=pid, kind=kind, material=material, ref=h[:4].upper())
        return self.get(pid)

    def mark_shown(self, pid: str, turn: int) -> None:
        self.s.x("UPDATE proposals SET shown_turn=? WHERE id=?", turn, pid)

    def approve(self, p: Proposal, user_text: str, turn: int, ts: str) -> int:
        if p.status != "pending":
            raise ApprovalError(f"proposal {p.id} is {p.status}, not pending")
        if p.shown_turn != turn - 1:
            raise ApprovalError("confirmation must answer the card shown in the immediately preceding turn")
        aid = self.s.x("INSERT INTO approvals(proposal_id,material_hash,user_message,turn,created_at) "
                       "VALUES(?,?,?,?,?)", p.id, p.material_hash, user_text, turn, ts)
        self.s.x("UPDATE proposals SET status='approved' WHERE id=?", p.id)
        self._ev("approval_granted", proposal_id=p.id, approval_id=aid, by="user_message", turn=turn)
        return aid

    def reject(self, pid: str, reason: str = "user declined") -> None:
        self.s.x("UPDATE proposals SET status='rejected', reason=? WHERE id=? AND status IN ('pending','approved')",
                 reason, pid)
        self._ev("approval_rejected", proposal_id=pid, reason=reason)

    def invalidate(self, pid: str, reason: str) -> None:
        self.s.x("UPDATE proposals SET status='invalidated', reason=? WHERE id=? AND status IN "
                 "('pending','approved')", reason, pid)
        self.s.x("UPDATE approvals SET consumed=1 WHERE proposal_id=?", pid)
        self._ev("approval_invalidated", proposal_id=pid, reason=reason)

    def authorize(self, pid: str, kind: str, current_material: dict) -> int:
        """Gate that every consequential executor must pass. Returns approval id."""
        p = self.get(pid)
        if not p:
            raise ApprovalError(f"no such proposal {pid}")
        if p.kind != kind:
            raise ApprovalError(f"proposal {pid} is for {p.kind}, not {kind}")
        if p.status not in ("approved", "executing"):
            raise ApprovalError(f"proposal {pid} status is {p.status}")
        a = self.s.one("SELECT * FROM approvals WHERE proposal_id=? AND consumed=0 ORDER BY id DESC", pid)
        if not a:
            raise ApprovalError(f"no unconsumed user approval for {pid}")
        cur = material_hash(kind, current_material)
        if cur != p.material_hash or a["material_hash"] != p.material_hash:
            raise ApprovalError("material details changed since approval "
                                f"(approved {p.material_hash[:8]}, now {cur[:8]})")
        self.s.x("UPDATE proposals SET status='executing' WHERE id=?", pid)
        self._ev("approval_checked", proposal_id=pid, approval_id=a["id"], ok=True)
        return a["id"]

    def consume(self, pid: str, final_status: str = "executed") -> None:
        self.s.x("UPDATE approvals SET consumed=1 WHERE proposal_id=?", pid)
        self.s.x("UPDATE proposals SET status=? WHERE id=?", final_status, pid)
