"""Consequential-action executors: booking, cancellation, rescheduling.

Guarantees
* Every side effect passes ``ApprovalGate.authorize`` with the material details *as
  they are at execution time* (re-held price/slot), so a change invalidates approval.
* Each target (user, provider, slot) maps to one durable intent row and one
  idempotency key; the row is written *before* the side effect, so repeated
  confirmations, retries and restarts all reuse the same key.
* Ambiguous outcomes (timeout / 5xx on book) are resolved via
  ``get_booking_status`` - success is only reported once verified.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .approval import ApprovalError, ApprovalGate, Proposal
from .mocks.service import ToolTimeout
from .state import Phase, Workflow
from .tools import ToolFailure, ToolGateway


class SimulatedCrash(Exception):
    """Raised by test hooks to emulate the process dying at a precise point."""


@dataclass
class Outcome:
    code: str
    message: str = ""
    data: dict = field(default_factory=dict)
    proposal: Proposal | None = None

    @property
    def success(self) -> bool:
        return self.code in ("booked", "already_booked", "cancelled", "rescheduled")


def _fmt_slot(slot: str) -> str:
    d = datetime.fromisoformat(slot)
    return d.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def slot_fits(slot: str, req, price: int | None = None) -> tuple[bool, str]:
    d = datetime.fromisoformat(slot)
    if req.date_from and d.date() < date.fromisoformat(req.date_from):
        return False, f"{_fmt_slot(slot)} is before {req.date_from}"
    if req.date_to and d.date() > date.fromisoformat(req.date_to):
        return False, f"{_fmt_slot(slot)} is after {req.date_to}"
    hm = d.strftime("%H:%M")
    if req.earliest_time and hm < req.earliest_time:
        return False, f"{_fmt_slot(slot)} is earlier than {req.earliest_time}"
    if req.latest_time and hm >= req.latest_time:
        return False, f"{_fmt_slot(slot)} starts at/after {req.latest_time}"
    if price is not None and req.budget_max is not None and price > req.budget_max:
        return False, f"price ₹{price} exceeds budget ₹{req.budget_max}"
    return True, ""


class Executor:
    def __init__(self, store, gate: ApprovalGate, tools: ToolGateway, tracer, clock, limits, user_id: str):
        self.s = store
        self.gate = gate
        self.tools = tools
        self.tracer = tracer
        self.clock = clock
        self.limits = limits
        self.user_id = user_id
        self.crash_points: set[str] = set()

    def _crash(self, point: str) -> None:
        if point in self.crash_points:
            self.crash_points.discard(point)
            self.tracer.event("simulated_crash", point=point)
            raise SimulatedCrash(point)

    def _now(self) -> str:
        return self.clock.now().isoformat()

    # ------------------------------------------------------------- ledger
    def _intent_for(self, base: str) -> Any:
        return self.s.one("SELECT * FROM intents WHERE idem_key LIKE ? AND status IN "
                          "('prepared','in_flight','unknown','succeeded') ORDER BY updated_at DESC",
                          hashlib.sha256(base.encode()).hexdigest()[:16] + "%")

    def _new_key(self, base: str) -> str:
        prefix = hashlib.sha256(base.encode()).hexdigest()[:16]
        gen = self.s.one("SELECT COUNT(*) c FROM intents WHERE idem_key LIKE ?", prefix + "%")["c"]
        return f"{prefix}-g{gen}"

    def _ledger(self, key: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        self.s.x(f"UPDATE intents SET {sets}, updated_at=? WHERE idem_key=?", *vals, self._now(), key)
        self.tracer.event("ledger", idem_key=key, **{k: v for k, v in fields.items() if k != "material"})

    def _ledger_create(self, key: str, p: Proposal, wf: Workflow) -> None:
        self.s.x("INSERT INTO intents(idem_key,proposal_id,session_id,kind,material,status,result,attempts,"
                 "updated_at) VALUES(?,?,?,?,?,?,?,?,?)", key, p.id, wf.session_id, p.kind,
                 json.dumps(p.material), "prepared", None, 0, self._now())
        self.tracer.event("ledger", idem_key=key, status="prepared", proposal_id=p.id)

    def _remember_booking(self, b: dict) -> None:
        self.s.x("INSERT INTO known_bookings(booking_id,user_id,data,status,handled) VALUES(?,?,?,?,?) "
                 "ON CONFLICT(booking_id) DO UPDATE SET data=excluded.data, status=excluded.status",
                 b["booking_id"], self.user_id, json.dumps(b), b.get("status", "confirmed"), None)

    # ------------------------------------------------------ booking: prepare
    def prepare_booking(self, wf: Workflow, provider_id: str, slot: str, search_price: int | None,
                        provider_name: str) -> Proposal | str:
        """Hold the slot and create a proposal. Returns a Proposal or an error string."""
        ok, why = slot_fits(slot, wf.request)
        if not ok:
            return f"rejected: {why} (violates the user's stated constraints)"
        if provider_id in wf.request.exclude_providers:
            return f"rejected: provider {provider_id} is excluded for this request"
        try:
            hold = self.tools.call("hold_slot", provider_id=provider_id, slot=slot)
        except ToolFailure as e:
            return f"error: could not hold slot ({e.code})"
        if hold.get("status") != "held":
            return f"unavailable: slot {slot} for {provider_id} is no longer available"
        ok, why = slot_fits(slot, wf.request, hold["quoted_price"])
        if not ok:
            return (f"rejected: provider quoted ₹{hold['quoted_price']} at hold time "
                    f"(search price ₹{search_price}); {why}")
        material = {"provider_id": provider_id, "provider_name": provider_name, "slot": slot,
                    "price": hold["quoted_price"], "service": wf.request.service}
        ctx = {"hold_id": hold["hold_id"], "expires_at": hold["expires_at"], "search_price": search_price}
        return self.gate.propose(wf.session_id, "book", material, ctx, wf.turn, self._now())

    def _rehold(self, p: Proposal, wf: Workflow, why: str) -> Outcome | dict:
        """Hold expired: re-acquire. Same price+slot keeps approval valid; anything
        else invalidates it and produces a fresh proposal."""
        m = p.material
        self.tracer.event("rehold", proposal_id=p.id, reason=why)
        try:
            hold = self.tools.call("hold_slot", provider_id=m["provider_id"], slot=m["slot"])
        except ToolFailure as e:
            self.gate.invalidate(p.id, f"could not re-hold: {e.code}")
            return Outcome("failed", f"I couldn't re-reserve the slot ({e.code}). Nothing was booked.")
        if hold.get("status") != "held":
            self.gate.invalidate(p.id, "slot no longer available at re-hold")
            return Outcome("slot_gone", f"The {_fmt_slot(m['slot'])} slot with {m['provider_name']} was taken "
                                        "before I could book it. Nothing was booked.")
        if hold["quoted_price"] != m["price"]:
            self.gate.invalidate(p.id, f"price changed ₹{m['price']} -> ₹{hold['quoted_price']}")
            new_m = dict(m, price=hold["quoted_price"])
            ctx = {"hold_id": hold["hold_id"], "expires_at": hold["expires_at"],
                   "search_price": p.context.get("search_price")}
            np = self.gate.propose(wf.session_id, "book", new_m, ctx, wf.turn, self._now())
            return Outcome("needs_reapproval",
                           f"The price changed from ₹{m['price']} to ₹{hold['quoted_price']} when I re-reserved "
                           "the slot, so I haven't booked anything. Please confirm the new price.", proposal=np)
        self.s.x("UPDATE proposals SET context=? WHERE id=?",
                 json.dumps(dict(p.context, hold_id=hold["hold_id"], expires_at=hold["expires_at"])), p.id)
        return hold

    # ------------------------------------------------------ booking: execute
    def execute_booking(self, p: Proposal, wf: Workflow) -> Outcome:
        m = p.material
        base = f"{self.user_id}|book|{m['provider_id']}|{m['slot']}"
        intent = self._intent_for(base)
        if intent and intent["status"] == "succeeded":
            self.gate.consume(p.id, "duplicate")
            res = json.loads(intent["result"])
            self.tracer.event("duplicate_suppressed", idem_key=intent["idem_key"], booking_id=res.get("booking_id"))
            return Outcome("already_booked", "", data=res)
        if intent and intent["status"] in ("in_flight", "unknown"):
            return self.reconcile_intent(intent, wf)
        key = intent["idem_key"] if intent else self._new_key(base)
        if not intent:
            self._ledger_create(key, p, wf)
        elif intent["proposal_id"] != p.id:
            self._ledger(key, proposal_id=p.id)

        hold_id, expires = p.context["hold_id"], datetime.fromisoformat(p.context["expires_at"])
        if self.clock.now() >= expires:
            r = self._rehold(p, wf, "hold expired before booking")
            if isinstance(r, Outcome):
                self._ledger(key, status="failed", result={"reason": r.code})
                return r
            hold_id = r["hold_id"]
            current_price = r["quoted_price"]
        else:
            current_price = m["price"]

        try:
            self.gate.authorize(p.id, "book", {"provider_id": m["provider_id"], "slot": m["slot"],
                                               "price": current_price})
        except ApprovalError as e:
            self.tracer.event("approval_denied", proposal_id=p.id, error=str(e))
            self._ledger(key, status="failed", result={"reason": "approval_denied"})
            return Outcome("approval_denied", f"I could not book because the approval check failed: {e}")

        wf.transition(Phase.EXECUTING, "approved booking", self.tracer)
        attempts = intent["attempts"] if intent else 0
        for _ in range(self.limits.max_book_attempts):
            attempts += 1
            self._ledger(key, status="in_flight", attempts=attempts)   # durable BEFORE side effect
            self._crash("before_book")
            try:
                res = self.tools.call("book", hold_id=hold_id, idempotency_key=key)
            except ToolTimeout:
                self._crash("after_book_timeout")
                wf.transition(Phase.VERIFYING, "book timed out; consulting source of truth", self.tracer)
                st = self.check_status(key)
                if st.get("status") == "confirmed":
                    return self._succeed(key, p, wf, st, verified_by="get_booking_status")
                if st.get("status") == "not_found":
                    self.tracer.event("decision", what="retry_book", why="status=not_found after timeout",
                                      idem_key=key)
                    if self.clock.now() >= datetime.fromisoformat(p.context["expires_at"]):
                        p = self.gate.get(p.id)
                        r = self._rehold(p, wf, "hold expired during timeout")
                        if isinstance(r, Outcome):
                            self._ledger(key, status="failed", result={"reason": r.code})
                            return r
                        hold_id = r["hold_id"]
                        p = self.gate.get(p.id)
                    wf.transition(Phase.EXECUTING, "retrying book with same idempotency key", self.tracer)
                    continue
                self._ledger(key, status="unknown")
                wf.transition(Phase.FAILED, "booking outcome unverifiable", self.tracer)
                return Outcome("unverified",
                               "The booking request timed out and I couldn't verify whether it went through. "
                               "I have NOT confirmed it. I'll keep checking with the same reference, so you "
                               "won't be double-booked.", data={"idem_key": key})
            except ToolFailure as e:
                self._ledger(key, status="failed", result={"reason": e.code})
                self.gate.consume(p.id, "failed")
                wf.transition(Phase.FAILED, f"book failed: {e.code}", self.tracer)
                return Outcome("failed", f"The booking failed ({e.code}). Nothing was booked.")
            self._crash("after_book")
            status = res.get("status")
            if status == "confirmed":
                return self._succeed(key, p, wf, res, verified_by="book_response")
            if status == "slot_taken":
                self._ledger(key, status="failed", result={"reason": "slot_taken"})
                self.gate.consume(p.id, "failed")
                wf.transition(Phase.FAILED, "slot taken", self.tracer)
                return Outcome("slot_taken", f"Someone else just took the {_fmt_slot(m['slot'])} slot with "
                                             f"{m['provider_name']}. Nothing was booked.")
            if status == "hold_expired":
                p = self.gate.get(p.id)
                r = self._rehold(p, wf, "book returned hold_expired")
                if isinstance(r, Outcome):
                    self._ledger(key, status="failed", result={"reason": r.code})
                    if wf.phase == Phase.EXECUTING and r.code == "needs_reapproval":
                        wf.transition(Phase.AWAITING_APPROVAL, "price changed at re-hold", self.tracer)
                    elif wf.phase == Phase.EXECUTING:
                        wf.transition(Phase.FAILED, r.code, self.tracer)
                    return r
                hold_id = r["hold_id"]
                continue
            self._ledger(key, status="failed", result={"reason": status})
            wf.transition(Phase.FAILED, f"unexpected book status {status}", self.tracer)
            return Outcome("failed", f"Unexpected booking response ({status}). Nothing confirmed.")
        self._ledger(key, status="unknown")
        wf.transition(Phase.FAILED, "book attempts exhausted", self.tracer)
        return Outcome("unverified", "I couldn't complete the booking after several attempts and could not "
                                     "verify it. I have NOT confirmed it.", data={"idem_key": key})

    def _succeed(self, key: str, p: Proposal, wf: Workflow, res: dict, verified_by: str) -> Outcome:
        m = p.material
        booking = {"booking_id": res["booking_id"], "provider_id": m["provider_id"],
                   "provider_name": m["provider_name"], "slot": res.get("slot", m["slot"]),
                   "price": res.get("price", m["price"]), "service": m.get("service"), "status": "confirmed"}
        self._ledger(key, status="succeeded", result=booking)
        self.gate.consume(p.id, "executed")
        self._remember_booking(booking)
        self.tracer.event("booking_verified", booking_id=booking["booking_id"], verified_by=verified_by)
        if wf.phase != Phase.DONE:
            wf.transition(Phase.DONE, f"booking verified via {verified_by}", self.tracer)
        return Outcome("booked", "", data=dict(booking, verified_by=verified_by))

    def check_status(self, key: str) -> dict:
        for i in range(self.limits.max_status_checks):
            try:
                st = self.tools.call("get_booking_status", idempotency_key=key)
            except ToolFailure:
                st = {"status": "error"}
            if st.get("status") in ("confirmed", "not_found", "cancelled", "cancelled_by_provider"):
                return st
            self.clock.sleep(self.limits.backoff_base_s * 2 ** i)
        return {"status": "unknown"}

    def reconcile_intent(self, intent, wf: Workflow) -> Outcome:
        """Resolve an in-flight/unknown intent (after a timeout or a restart)."""
        key = intent["idem_key"]
        self.tracer.event("reconcile", idem_key=key, ledger_status=intent["status"])
        st = self.check_status(key)
        p = self.gate.get(intent["proposal_id"])
        if st.get("status") == "confirmed":
            if wf.phase not in (Phase.VERIFYING, Phase.DONE):
                wf.phase = Phase.VERIFYING  # recovery path: process restarted mid-execution
            return self._succeed(key, p, wf, st, verified_by="get_booking_status(recovery)")
        if st.get("status") == "not_found":
            # Never resume a side effect on stale approval after an interruption: ask again.
            self._ledger(key, status="failed", result={"reason": "not_booked_before_interruption"})
            if p and p.status in ("approved", "executing"):
                self.gate.invalidate(p.id, "interrupted before booking; fresh approval required")
            return Outcome("not_booked", "Your earlier booking attempt did not go through (I checked with the "
                                         "provider). Nothing was booked.", proposal=None,
                           data={"material": json.loads(intent["material"])})
        return Outcome("unverified", "I still can't verify the earlier booking attempt. It is NOT confirmed; "
                                     "I'll keep checking with the same reference.", data={"idem_key": key})

    def recover(self, wf: Workflow) -> list[Outcome]:
        rows = self.s.q("SELECT * FROM intents WHERE session_id=? AND status IN ('in_flight','unknown') "
                        "AND kind='book'", wf.session_id)
        return [self.reconcile_intent(r, wf) for r in rows]

    # ------------------------------------------------------- cancel
    def _current_booking(self, booking_id: str) -> dict | None:
        rows = self.tools.call("list_bookings", user_id=self.user_id)["bookings"]
        return next((b for b in rows if b["booking_id"] == booking_id), None)

    def execute_cancel(self, p: Proposal, wf: Workflow) -> Outcome:
        m = p.material
        base = f"{self.user_id}|cancel|{m['booking_id']}"
        intent = self._intent_for(base)
        if intent and intent["status"] == "succeeded":
            self.gate.consume(p.id, "duplicate")
            return Outcome("cancelled", "already cancelled", data=json.loads(intent["result"]))
        key = intent["idem_key"] if intent else self._new_key(base)
        if not intent:
            self._ledger_create(key, p, wf)
        try:
            cur = self._current_booking(m["booking_id"])
        except ToolFailure as e:
            return Outcome("failed", f"I couldn't load your booking to verify it ({e.code}). Nothing changed.")
        if not cur or cur["status"] != "confirmed":
            self.gate.invalidate(p.id, "booking no longer active")
            return Outcome("failed", "That booking is no longer active, so there was nothing to cancel.")
        try:
            self.gate.authorize(p.id, "cancel", {"booking_id": cur["booking_id"], "provider_id": cur["provider_id"],
                                                 "slot": cur["slot"]})
        except ApprovalError as e:
            self.tracer.event("approval_denied", proposal_id=p.id, error=str(e))
            return Outcome("approval_denied", f"Cancellation not performed: {e}")
        wf.transition(Phase.EXECUTING, "approved cancellation", self.tracer)
        self._ledger(key, status="in_flight", attempts=1)
        try:
            res = self.tools.call("cancel", booking_id=m["booking_id"], idempotency_key=key)
        except (ToolFailure, ToolTimeout) as e:
            self._ledger(key, status="unknown")
            wf.transition(Phase.FAILED, "cancel failed after retries", self.tracer)
            return Outcome("failed", f"Cancellation didn't go through after retries ({getattr(e, 'code', e)}). "
                                     "Your booking is unchanged as far as I can verify.")
        wf.transition(Phase.VERIFYING, "verify cancellation", self.tracer)
        after = self._current_booking(m["booking_id"])
        if res.get("status") == "cancelled" and after and after["status"] == "cancelled":
            self._ledger(key, status="succeeded", result=res)
            self.gate.consume(p.id)
            self.s.x("UPDATE known_bookings SET status='cancelled', handled='user_cancel' WHERE booking_id=?",
                     m["booking_id"])
            wf.transition(Phase.DONE, "cancellation verified", self.tracer)
            return Outcome("cancelled", "", data=res)
        self._ledger(key, status="failed", result=res)
        wf.transition(Phase.FAILED, "cancellation not verified", self.tracer)
        return Outcome("failed", f"The provider did not confirm the cancellation ({res.get('status')}).")

    # ------------------------------------------------------- reschedule
    def execute_reschedule(self, p: Proposal, wf: Workflow) -> Outcome:
        m = p.material
        base = f"{self.user_id}|reschedule|{m['booking_id']}|{m['to_slot']}"
        intent = self._intent_for(base)
        if intent and intent["status"] == "succeeded":
            self.gate.consume(p.id, "duplicate")
            return Outcome("rescheduled", "already", data=json.loads(intent["result"]))
        key = intent["idem_key"] if intent else self._new_key(base)
        if not intent:
            self._ledger_create(key, p, wf)
        try:
            cur = self._current_booking(m["booking_id"])
        except ToolFailure as e:
            return Outcome("failed", f"I couldn't load your booking ({e.code}). Nothing changed.")
        if not cur:
            return Outcome("failed", "I couldn't find that booking any more.")
        try:
            self.gate.authorize(p.id, "reschedule", {"booking_id": cur["booking_id"], "provider_id": cur["provider_id"],
                                                     "from_slot": cur["slot"], "to_slot": m["to_slot"],
                                                     "price": cur["price"]})
        except ApprovalError as e:
            self.tracer.event("approval_denied", proposal_id=p.id, error=str(e))
            return Outcome("approval_denied", f"Reschedule not performed: {e}")
        wf.transition(Phase.EXECUTING, "approved reschedule", self.tracer)
        self._ledger(key, status="in_flight", attempts=1)
        try:
            res = self.tools.call("reschedule", booking_id=m["booking_id"], slot=m["to_slot"], idempotency_key=key)
        except (ToolFailure, ToolTimeout) as e:
            self._ledger(key, status="unknown")
            wf.transition(Phase.FAILED, "reschedule failed", self.tracer)
            return Outcome("failed", f"Rescheduling failed ({getattr(e, 'code', e)}). Your original slot "
                                     f"{_fmt_slot(cur['slot'])} is unchanged as far as I can verify.")
        if res.get("status") == "rescheduled":
            self._ledger(key, status="succeeded", result=res)
            self.gate.consume(p.id)
            wf.transition(Phase.DONE, "reschedule confirmed", self.tracer)
            return Outcome("rescheduled", "", data=dict(res, provider_name=m["provider_name"]))
        self._ledger(key, status="failed", result=res)
        self.gate.consume(p.id, "failed")
        wf.transition(Phase.AWAITING_APPROVAL if res.get("status") == "slot_conflict" else Phase.FAILED,
                      f"reschedule returned {res.get('status')}", self.tracer)
        return Outcome(res.get("status", "failed"), f"{_fmt_slot(m['to_slot'])} isn't available "
                       f"({res.get('status')}). Your booking stays at {_fmt_slot(cur['slot'])}.",
                       data={"booking": cur, "to_slot": m["to_slot"]})
