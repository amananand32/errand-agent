"""Errand agent orchestrator.

One user turn = one traced run:
  1. deterministic pre-pass (approval / rejection / repeated confirmation / memory prompt)
  2. otherwise a bounded model<->tool loop over MODEL_TOOLS (no side-effecting tools)
  3. app-rendered confirmation card + claim guard + persisted state
"""
from __future__ import annotations

import json
import os
import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime

from .agent_tools import MODEL_TOOL_NAMES, MODEL_TOOLS
from .approval import ApprovalError, ApprovalGate, Proposal, classify_reply
from .clock import Clock, FakeClock
from .context import ContextBuilder, ContextPacket
from .executor import Executor, Outcome, SimulatedCrash, _fmt_slot, slot_fits
from .limits import Budget, LimitExceeded, Limits
from .llm.base import Model
from .memory import FRIENDLY, KEY_RE, MemoryStore
from .mocks import catalog
from .mocks.service import MockProviderService
from .sanitize import sanitize_tool_result
from .state import Phase, Workflow
from .store import Store
from .tools import ToolFailure, ToolGateway
from .tracing import Tracer, sanitize_for_trace

ALLOWED_MEMORY_PREFIXES = ("area", "time", "language", "technician", "budget", "home")
CLAIM_RE = re.compile(r"\b(is|are|been|got|successfully|now|all)\s+(booked|confirmed|cancell?ed|rescheduled|moved)\b"
                      r"|\bbooking is confirmed\b|\byou'?re all set\b", re.I)
NEG_RE = re.compile(r"\b(nothing|not|no|haven'?t|hasn'?t|won'?t|isn'?t|n't|never|until)\b", re.I)
MEMORY_PROMPT_PLACEHOLDER = "[asked the user whether to save an inferred preference]"
MEMORY_LIST_PLACEHOLDER = "[displayed the user's stored memories directly to them]"


@dataclass
class TurnResult:
    reply: str
    outcome: str
    run_id: str
    proposal_id: str | None = None
    data: dict = field(default_factory=dict)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w₹:\s-]", " ", s.lower())).strip()


def evidence_ok(evidence: str, user_text: str) -> bool:
    e = _norm(evidence or "")
    return len(e) >= 3 and e in _norm(user_text)


class Agent:
    def __init__(self, *, service: MockProviderService, model: Model, db_path: str, trace_dir: str,
                 clock: Clock | None = None, limits: Limits | None = None, user_id: str = "u_demo",
                 session_id: str = "main", context_budget: int = 3000, model_retries: int = 2):
        self.clock = clock or FakeClock()
        self.limits = limits or Limits()
        self.user_id, self.session_id = user_id, session_id
        self.model = model
        self.model_retries = model_retries
        self.store = Store(db_path)
        self.tracer = Tracer(trace_dir, self.clock)
        self.memory = MemoryStore(self.store)
        self.gate = ApprovalGate(self.store, self.tracer)
        self.tools = ToolGateway(service, self.tracer, self.clock, self.limits)
        self.executor = Executor(self.store, self.gate, self.tools, self.tracer, self.clock, self.limits, user_id)
        self.ctx = ContextBuilder(self.memory, context_budget)
        d = self.store.load_workflow(session_id)
        self.wf = Workflow.from_dict(d) if d else Workflow(session_id, user_id)
        self.context_log: list[ContextPacket] = []   # in-memory copy for tests / debugging
        self.notices: list[str] = []
        self._carry_verified: list[Outcome] = []
        self._startup_recovery()

    # ------------------------------------------------------------------ util
    def _ts(self) -> str:
        return self.clock.now().isoformat()

    def _save(self) -> None:
        self.store.save_workflow(self.session_id, self.user_id, self.wf.to_dict(), self._ts())

    def _goto(self, phase: str, reason: str) -> None:
        if self.wf.phase != phase:
            self.wf.transition(phase, reason, self.tracer)

    def _unresolved_intents(self) -> list:
        return self.store.q("SELECT idem_key FROM intents WHERE session_id=? AND kind='book' AND status IN "
                            "('in_flight','unknown')", self.session_id)

    def _reconcile(self) -> str | None:
        """Resolve ambiguous bookings against the source of truth. Returns a user notice."""
        msgs = []
        try:
            for out in self.executor.recover(self.wf):
                msgs.append(self._render_outcome(out))
                if out.success:
                    self._carry_verified.append(out)
                    self.wf.pending_proposal_id = None
        except LimitExceeded as e:
            msgs.append(f"I couldn't finish verifying an earlier booking ({e.which}); it is NOT confirmed yet.")
        return ("Update on your earlier booking: " + " ".join(msgs)) if msgs else None

    def _startup_recovery(self) -> None:
        rows = self._unresolved_intents()
        if not rows:
            return
        self.tracer.start_run(self.session_id, "startup_recovery", in_flight=[r["idem_key"] for r in rows])
        self.tools.budget = Budget(self.limits, self.clock)
        text = self._reconcile() or ""
        self.notices.append(text)
        self.store.push_outbox(self.user_id, "recovery", text, self._ts())
        self._save()
        self.tracer.end_run("recovered", text)

    # ------------------------------------------------------------------ entry
    def handle(self, text: str) -> TurnResult:
        text = (text or "").strip()[:2000]
        self.wf.turn += 1
        run_id = self.tracer.start_run(self.session_id, "user_turn", turn=self.wf.turn,
                                       user_text=sanitize_for_trace(text), phase=self.wf.phase)
        budget = Budget(self.limits, self.clock)
        self.tools.budget = budget
        self.store.add_message(self.session_id, self.user_id, "user", text, self._ts())
        self._verified: list[Outcome] = list(self._carry_verified)
        self._carry_verified = []
        self._direct: list[str] = []        # app-rendered blocks shown to user, redacted in history
        self._flags: set[str] = set()
        try:
            if self._unresolved_intents():   # an earlier booking is still ambiguous: resolve it first
                note = self._reconcile()
                if note:
                    self.notices.append(note)
                self._verified += self._carry_verified
                self._carry_verified = []
            reply, outcome = self._handle(text, budget)
        except SimulatedCrash:
            raise  # emulate the process dying: nothing after this point runs
        except LimitExceeded as e:
            self.tracer.event("limit_exceeded", which=e.which, detail=str(e))
            reply, outcome = self._limit_reply(e), "limit_exceeded"
        except Exception as e:  # noqa: BLE001 - every exit path must produce a user-facing outcome
            self.tracer.event("error", error=f"{type(e).__name__}: {e}", tb=traceback.format_exc()[-1500:])
            reply, outcome = ("Sorry - something went wrong on my side. I haven't booked or changed anything "
                              "beyond what I've confirmed above. Please try again."), "error"
        reply_user, reply_hist = self._compose(reply)
        self._save()
        self.store.add_message(self.session_id, self.user_id, "assistant", reply_hist, self._ts())
        self.tracer.end_run(outcome, reply_user, phase=self.wf.phase, budget={
            "model_turns": budget.model_turns, "tool_calls": budget.tool_calls,
            "elapsed_s": round(budget.elapsed(), 2)})
        return TurnResult(reply_user, outcome, run_id, self.wf.pending_proposal_id,
                          data={"verified": [o.data for o in self._verified]})

    def _compose(self, reply: str) -> tuple[str, str]:
        parts_user, parts_hist = [], []
        if self.notices:
            parts_user += self.notices
            parts_hist += self.notices
            self.notices = []
        parts_user.append(reply)
        parts_hist.append(reply)
        for d in self._direct:
            parts_user.append(d)
            parts_hist.append(MEMORY_LIST_PLACEHOLDER)
        p = self.gate.pending(self.session_id)
        if p:
            self.gate.mark_shown(p.id, self.wf.turn)
            self.wf.pending_proposal_id = p.id
            card = self.render_card(p)
            parts_user.append(card)
            parts_hist.append(card)
        elif self.wf.pending_memory_prompt is None:
            mp = self._maybe_memory_prompt()
            if mp:
                parts_user.append(mp)
                parts_hist.append(MEMORY_PROMPT_PLACEHOLDER)
        return "\n\n".join(x for x in parts_user if x), "\n\n".join(x for x in parts_hist if x)

    # --------------------------------------------------------- deterministic pre-pass
    def _handle(self, text: str, budget: Budget) -> tuple[str, str]:
        p = self.gate.pending(self.session_id)
        if p:
            c = classify_reply(text, p.ref)
            self.tracer.event("confirmation_parse", proposal_id=p.id, classification=c)
            if c == "approve":
                try:
                    self.gate.approve(p, text, self.wf.turn, self._ts())
                except ApprovalError as e:
                    self.tracer.event("approval_refused", proposal_id=p.id, error=str(e))
                    return "Please check the details below once more and reply 'yes' to confirm.", "stale_approval"
                return self._execute(self.gate.get(p.id))
            if c == "reject":
                self.gate.reject(p.id)
                self.wf.pending_proposal_id = None
                self._goto(Phase.IDLE, "user declined proposal")
                return ("Okay, I won't go ahead - nothing was booked or changed. Want me to look at other "
                        "options?"), "rejected"
        else:
            c = classify_reply(text)
            if self.wf.pending_memory_prompt is not None:
                return self._memory_prompt_answer(text) or self._llm_loop(text, budget)
            if c == "approve":
                latest = self.gate.latest(self.session_id)
                if latest and latest.status in ("executed", "duplicate"):
                    self.tracer.event("duplicate_confirmation_ignored", proposal_id=latest.id)
                    return (f"That's already done ({self._describe(latest)}). I haven't made any duplicate "
                            "booking."), "already_done"
                if latest and latest.status in ("invalidated", "rejected", "failed"):
                    return ("There's nothing waiting for your confirmation right now - the earlier proposal is no "
                            "longer valid. Tell me what you'd like and I'll set it up again."), "nothing_to_confirm"
        return self._llm_loop(text, budget)

    def _describe(self, p: Proposal) -> str:
        m = p.material
        if p.kind == "book":
            row = self.store.one("SELECT result FROM intents WHERE proposal_id=? AND status='succeeded'", p.id)
            bid = json.loads(row["result"])["booking_id"] if row else None
            return f"{m['provider_name']} on {_fmt_slot(m['slot'])}" + (f", booking {bid}" if bid else "")
        return f"{p.kind} of {m['booking_id']}"

    # --------------------------------------------------------- execution
    def _execute(self, p: Proposal) -> tuple[str, str]:
        try:
            if p.kind == "book":
                out = self.executor.execute_booking(p, self.wf)
            elif p.kind == "cancel":
                out = self.executor.execute_cancel(p, self.wf)
            else:
                out = self.executor.execute_reschedule(p, self.wf)
        except LimitExceeded as e:
            self.tracer.event("limit_exceeded", which=e.which, during="execution")
            self.wf.pending_proposal_id = None
            return (f"I ran out of time while confirming ({e.which}). The action is NOT confirmed yet; I'll "
                    "verify it with the same reference before doing anything else, so there's no risk of a "
                    "duplicate."), "unverified"
        self.wf.pending_proposal_id = out.proposal.id if out.proposal else None
        if out.success:
            self._verified.append(out)
        if out.code in ("slot_taken", "slot_gone") and p.kind == "book":
            alt = self._propose_alternative(p)
            return self._render_outcome(out) + (" " + alt if alt else " Want me to search again?"), out.code
        if out.code == "slot_conflict":
            alt = self._propose_reschedule_alternative(p)
            return self._render_outcome(out) + (" " + alt if alt else ""), out.code
        if out.code == "needs_reapproval":
            self._goto(Phase.AWAITING_APPROVAL, "material change requires fresh approval")
        if out.code == "not_booked":
            self._goto(Phase.IDLE, "earlier attempt not booked")
        return self._render_outcome(out), out.code

    def _render_outcome(self, out: Outcome) -> str:
        d = out.data
        if out.code == "booked":
            extra = (" (The booking request timed out, so I checked with the provider's system before "
                     "confirming - it went through exactly once.)" if "status" in d.get("verified_by", "") else "")
            return (f"✅ Booked and verified: {d['provider_name']}, {_fmt_slot(d['slot'])}, ₹{d['price']}. "
                    f"Booking ID {d['booking_id']}.{extra}")
        if out.code == "already_booked":
            return f"That's already booked ({d.get('booking_id')}) - I haven't made a duplicate."
        if out.code == "cancelled":
            return f"✅ Cancelled booking {d.get('booking_id')} (verified with the provider)."
        if out.code == "rescheduled":
            return (f"✅ Moved booking {d.get('booking_id')} to {_fmt_slot(d['slot'])} with {d.get('provider_name')} "
                    "(confirmed by the provider).")
        return out.message

    def _propose_alternative(self, failed: Proposal) -> str:
        bad = (failed.material["provider_id"], failed.material["slot"])
        for c in sorted(self.wf.candidates, key=lambda c: bool(c.get("review_flagged"))):
            for s in c.get("fitting_slots", []):
                if (c["provider_id"], s) == bad:
                    continue
                r = self.executor.prepare_booking(self.wf, c["provider_id"], s, c["price"], c["name"])
                if isinstance(r, Proposal):
                    self.wf.pending_proposal_id = r.id
                    self._goto(Phase.AWAITING_APPROVAL, "alternative proposed after failure")
                    return "I've reserved the next best option instead - please confirm below if it works."
        return ""

    def _propose_reschedule_alternative(self, failed: Proposal) -> str:
        m = failed.material
        tried = {m["to_slot"]} | {json.loads(r["material"])["to_slot"] for r in self.store.q(
            "SELECT material FROM proposals WHERE kind='reschedule' AND material LIKE ?", f'%{m["booking_id"]}%')}
        try:
            g = self.tools.call("get_provider", provider_id=m["provider_id"])
        except ToolFailure:
            return "I couldn't load other slots right now."
        day = m["to_slot"][:10]
        slots = [s for s in g.get("available_slots", []) if s not in tried and s != m["from_slot"]
                 and slot_fits(s, self.wf.request)[0]]
        slots.sort(key=lambda s: (s[:10] != day, s))
        if not slots:
            self._goto(Phase.IDLE, "no reschedule alternative")
            return "I couldn't find another free slot that fits."
        alt = dict(m, to_slot=slots[0])
        p = self.gate.propose(self.session_id, "reschedule", alt, {}, self.wf.turn, self._ts())
        self.wf.pending_proposal_id = p.id
        return "The nearest free alternative is below - it needs your confirmation."

    # --------------------------------------------------------- model loop
    def _llm_loop(self, text: str, budget: Budget) -> tuple[str, str]:
        history = [r for r in self.store.messages(self.user_id)][:-1]
        scratch: list[dict] = []
        final = ""
        while True:
            budget.charge_model_turn()
            pkt = self.ctx.build(self.wf, self.clock.now().strftime("%Y-%m-%d %A %H:%M IST"), history, text, scratch)
            self.context_log.append(pkt)
            self.tracer.event("context", **pkt.manifest)
            resp = self._call_model(pkt)
            if not resp.tool_uses:
                final = resp.text
                break
            scratch.append({"role": "assistant", "content": resp.content})
            results = []
            for tu in resp.tool_uses:
                out, is_err = self._dispatch(tu["name"], tu.get("input") or {}, text)
                results.append({"type": "tool_result", "tool_use_id": tu["id"],
                                "content": json.dumps(out, ensure_ascii=False, default=str), "is_error": is_err})
            scratch.append({"role": "user", "content": results})
        final = self._guard_claims(final or "Okay.")
        outcome = ("proposed" if "proposed" in self._flags else
                   "asked" if self.wf.phase == Phase.GATHERING else "replied")
        return final, outcome

    def _call_model(self, pkt: ContextPacket):
        last = None
        for attempt in range(1, self.model_retries + 2):
            t0 = self.clock.monotonic()
            try:
                resp = self.model.complete(pkt.system, pkt.messages, MODEL_TOOLS)
            except Exception as e:  # noqa: BLE001
                last = e
                self.tracer.event("model_error", attempt=attempt, error=f"{type(e).__name__}: {e}")
                self.clock.sleep(self.limits.backoff_base_s * 2 ** (attempt - 1))
                continue
            self.tracer.model_call(resp.model, (self.clock.monotonic() - t0) * 1000, resp.tokens_in,
                                   resp.tokens_out, resp.stop_reason, [t["name"] for t in resp.tool_uses],
                                   resp.estimated)
            return resp
        raise RuntimeError(f"model unavailable after retries: {last}")

    def _guard_claims(self, text: str) -> str:
        if self._verified:
            return text
        for sent in re.split(r"(?<=[.!?])\s+", text):
            if CLAIM_RE.search(sent) and not NEG_RE.search(sent):
                self.tracer.event("unverified_claim_blocked", text=sanitize_for_trace(text))
                return ("I haven't booked or changed anything yet - any booking, cancellation or change needs "
                        "your confirmation and is only reported once the provider confirms it.")
        return text

    def _limit_reply(self, e: LimitExceeded) -> str:
        what = {"model_turns": "reasoning steps", "tool_calls": "tool calls",
                "wall_clock": "time"}.get(e.which, e.which)
        pend = " Your pending confirmation is still shown below." if self.gate.pending(self.session_id) else ""
        return (f"I had to stop because I hit my limit on {what} for this request. Nothing was booked, cancelled "
                f"or changed in this step.{pend} You can say 'try again' and I'll pick up from where we are.")

    # --------------------------------------------------------- tool dispatch
    def _dispatch(self, name: str, inp: dict, user_text: str) -> tuple[dict, bool]:
        if name not in MODEL_TOOL_NAMES:
            self.tracer.event("blocked_tool_call", tool=name, args=sanitize_for_trace(inp),
                              reason="not exposed to model")
            return {"error": f"tool '{name}' is not available. Consequential actions can only be proposed with "
                             "propose_* and are executed by the app after the user confirms."}, True
        try:
            return getattr(self, f"_t_{name}")(inp, user_text)
        except ToolFailure as e:
            return {"error": e.code, "message": e.message, "attempts": e.attempts}, True
        except (ValueError, KeyError, TypeError) as e:
            self.tracer.event("tool_input_error", tool=name, error=str(e))
            return {"error": "invalid_input", "message": str(e)}, True

    def _t_update_request(self, inp: dict, user_text: str):
        if not evidence_ok(inp.get("evidence", ""), user_text):
            self.tracer.event("blocked_request_change", fields={k: v for k, v in inp.items() if k != "evidence"},
                              evidence=sanitize_for_trace(inp.get("evidence", "")),
                              reason="evidence is not a quote of the user's message")
            return {"error": "rejected: request details can only come from the user's own words; evidence "
                             "was not found in the user's latest message"}, True
        wf = self.wf
        intent = inp.get("intent")
        # A different intent, or any new request after a completed task, starts a fresh request.
        # Corrections within an active task (incl. after a failure/rejection) keep all other fields.
        if intent and ((wf.intent is not None and intent != wf.intent) or wf.phase == Phase.DONE):
            if self.gate.pending(self.session_id):
                self.gate.invalidate(self.gate.pending(self.session_id).id, "user started a different task")
            wf.reset_for_new_task(self.tracer)
        if intent:
            wf.intent = intent
        # Bind AFTER a possible reset: reset_for_new_task replaces wf.request (BUG-1 in BUGS.md).
        r = wf.request
        changed = []

        def setf(k, v):
            if getattr(r, k) != v:
                setattr(r, k, v)
                r.sources[k] = "user"
                changed.append(k)

        if "service" in inp:
            if inp["service"] not in catalog.SERVICES:
                raise ValueError(f"unknown service {inp['service']}")
            setf("service", inp["service"])
        if "area" in inp:
            area = next((a for a in catalog.AREAS if a.lower() == str(inp["area"]).lower()), None)
            if not area:
                raise ValueError(f"unsupported area {inp['area']!r}; supported: {catalog.AREAS}")
            setf("area", area)
        for k in ("date_from", "date_to"):
            if k in inp:
                datetime.fromisoformat(inp[k])
                setf(k, inp[k])
        if r.date_from and r.date_to and r.date_to < r.date_from:
            raise ValueError("date_to before date_from")
        for k in ("earliest_time", "latest_time"):
            if k in inp:
                if not re.fullmatch(r"[0-2]\d:[0-5]\d", str(inp[k])):
                    raise ValueError(f"{k} must be HH:MM")
                setf(k, inp[k])
        if inp.get("no_budget"):
            setf("budget_max", None)
            r.budget_set = True
        elif "budget_max" in inp:
            if int(inp["budget_max"]) <= 0:
                raise ValueError("budget must be positive")
            setf("budget_max", int(inp["budget_max"]))
            r.budget_set = True
        if "provider_preference" in inp:
            if inp["provider_preference"] not in catalog.PROVIDERS:
                raise ValueError("unknown provider id")
            setf("provider_preference", inp["provider_preference"])
        filled = self._apply_memory_defaults()
        if "earliest_time" in changed and r.sources.get("earliest_time") == "user":
            explicit = self.memory.active(self.user_id, "time.earliest")
            if not explicit or explicit.value != r.earliest_time:
                im = self.memory.add(self.user_id, "time.earliest", r.earliest_time, "inferred", self._ts(),
                                     evidence=inp["evidence"][:200])
                if im.source == "inferred" and not im.confirmed and im.id not in wf.inference_candidates:
                    wf.inference_candidates.append(im.id)
                    self.tracer.event("memory_inferred", id=im.id, key=im.key, confirmed=False)
        if changed:
            self.tracer.event("request_updated", changed=changed, request=sanitize_for_trace(r.__dict__))
            p = self.gate.pending(self.session_id)
            if p and p.kind == "book":
                self.gate.invalidate(p.id, f"user changed {', '.join(changed)}")
                wf.pending_proposal_id = None
            wf.candidates = []
        missing = r.missing() if wf.intent in (None, "book") else []
        if wf.intent in (None, "book"):
            self._goto(Phase.GATHERING if missing else Phase.SEARCHING,
                       "missing " + ", ".join(missing) if missing else "request complete")
        else:
            self._goto(Phase.SEARCHING, f"{wf.intent}: locating booking")
        if missing:
            self._flags.add("asked")
        view = {k: v for k, v in r.__dict__.items() if v not in (None, [], {}, False) and k != "sources"}
        if r.provider_preference:
            view["provider_name"] = catalog.PROVIDERS[r.provider_preference]["name"]
        return {"ok": True, "intent": wf.intent, "request": view, "missing": missing, "changed": changed,
                "filled_from_memory": filled}, False

    def _apply_memory_defaults(self) -> dict:
        r = self.wf.request
        filled = {}
        if self.wf.intent not in (None, "book", "reschedule"):
            return filled
        pairs = (("area", "area.preferred"), ("earliest_time", "time.earliest"))
        if self.wf.intent == "reschedule":
            pairs = (("earliest_time", "time.earliest"),)
        for fld, key in pairs:
            if getattr(r, fld) is None:
                m = self.memory.active(self.user_id, key)
                if m and m.eligible:
                    setattr(r, fld, m.value)
                    r.sources[fld] = f"memory:{m.id}"
                    filled[fld] = m.value
        if r.service and not r.budget_set and self.wf.intent != "reschedule":
            m = self.memory.active(self.user_id, f"budget.{r.service}")
            if m and m.eligible:
                r.budget_max, r.budget_set = int(m.value), True
                r.sources["budget_max"] = f"memory:{m.id}"
                filled["budget_max"] = m.value
        if filled:
            self.tracer.event("memory_defaults_applied", filled=filled)
        return filled

    def rank(self, providers: list[dict]) -> tuple[list[dict], str]:
        r = self.wf.request
        out, reasons = [], {"over_budget": 0, "no_fitting_slot": 0, "excluded": 0, "not_preferred": 0}
        for p in providers:
            if p["provider_id"] in r.exclude_providers:
                reasons["excluded"] += 1
                continue
            if r.provider_preference and p["provider_id"] != r.provider_preference:
                reasons["not_preferred"] += 1
                continue
            if r.budget_max is not None and p["price"] > r.budget_max:
                reasons["over_budget"] += 1
                continue
            fitting = [s for s in p["available_slots"] if slot_fits(s, r, p["price"])[0]]
            if not fitting:
                reasons["no_fitting_slot"] += 1
                continue
            out.append({"provider_id": p["provider_id"], "name": p["name"], "price": p["price"],
                        "rating": p["rating"], "fitting_slots": fitting})
        out.sort(key=lambda c: (-c["rating"], c["price"]))
        return out[:5], ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in reasons.items() if v)

    def _t_search_providers(self, inp: dict, user_text: str):
        r = self.wf.request
        if self.wf.intent in (None, "book") and r.missing():
            return {"error": f"request incomplete; ask the user for: {', '.join(r.missing())}"}, True
        for k in ("service", "area", "date_from", "date_to"):
            want = getattr(r, k)
            if want and str(inp.get(k, "")).lower() != str(want).lower():
                self.tracer.event("search_args_mismatch", field=k, model_value=inp.get(k), request_value=want)
                return {"error": f"search {k} must match the user's request ({want}); call update_request "
                                 "first if the user changed it"}, True
        res = self.tools.call("search_providers", caller="model", service=r.service, area=r.area,
                              date_range=[r.date_from, r.date_to])
        ranked, filtered = self.rank(res["providers"])
        self.wf.candidates = ranked
        self._goto(Phase.SEARCHING, f"{len(ranked)} options fit")
        clean, flags = sanitize_tool_result({
            "ranked_options": [{"rank": i + 1, "provider_id": c["provider_id"], "name": c["name"],
                                "price": c["price"], "rating": c["rating"],
                                "earliest_fitting_slot": c["fitting_slots"][0],
                                "other_fitting_slots": c["fitting_slots"][1:3]} for i, c in enumerate(ranked)],
            "total_found": len(res["providers"]), "filtered_out_summary": filtered or "none",
            "note": "Ranking and constraint filtering were done by the app from the user's request."})
        return clean, False

    def _t_get_provider(self, inp: dict, user_text: str):
        res = self.tools.call("get_provider", caller="model", provider_id=inp["provider_id"])
        r = self.wf.request
        slots = res.get("available_slots", [])
        if r.date_from and r.date_to:   # keep what matters for this request before capping size
            slots = [s for s in slots if r.date_from <= s[:10] <= r.date_to] or slots
        res = dict(res, available_slots=slots[:12])
        clean, flags = sanitize_tool_result(res)
        if flags:
            self.tracer.event("injection_flagged", tool="get_provider", provider_id=inp["provider_id"], paths=flags)
            for c in self.wf.candidates:
                if c["provider_id"] == inp["provider_id"]:
                    c["review_flagged"] = True
        return clean, False

    def _t_list_bookings(self, inp: dict, user_text: str):
        res = self.tools.call("list_bookings", caller="model", user_id=self.user_id)
        self.wf.bookings_seen = res["bookings"]
        return res, False

    def _t_propose_booking(self, inp: dict, user_text: str):
        pid, slot = inp.get("provider_id"), inp.get("slot")
        c = next((c for c in self.wf.candidates if c["provider_id"] == pid), None)
        if not c or slot not in c["fitting_slots"]:
            self.tracer.event("blocked_proposal", provider_id=pid, slot=slot,
                              reason="not among code-verified fitting search results")
            return {"error": "provider/slot must be one of the fitting options from the latest search"}, True
        r = self.executor.prepare_booking(self.wf, pid, slot, c["price"], c["name"])
        if not isinstance(r, Proposal):
            return {"error": r}, True
        self.wf.pending_proposal_id = r.id
        self._goto(Phase.AWAITING_APPROVAL, "booking proposed")
        self._flags.add("proposed")
        return {"ok": True, "proposal": {"provider_name": r.material["provider_name"], "slot": slot,
                                         "price": r.material["price"], "search_price": c["price"]},
                "note": "The app shows the user a confirmation card. Do not say it is booked."}, False

    def _booking_seen(self, booking_id: str) -> dict:
        b = next((b for b in self.wf.bookings_seen if b["booking_id"] == booking_id), None)
        if not b:
            raise ValueError("booking must come from list_bookings in this conversation")
        if b["status"] != "confirmed":
            raise ValueError(f"booking {booking_id} is {b['status']}")
        if datetime.fromisoformat(b["slot"]) < self.clock.now().replace(tzinfo=None):
            raise ValueError("booking is in the past")
        return b

    def _t_propose_reschedule(self, inp: dict, user_text: str):
        b = self._booking_seen(inp["booking_id"])
        slot = inp["new_slot"]
        datetime.fromisoformat(slot)
        ok, why = slot_fits(slot, self.wf.request)
        if not ok:
            return {"error": f"rejected: {why}"}, True
        m = {"booking_id": b["booking_id"], "provider_id": b["provider_id"], "provider_name": b["provider_name"],
             "from_slot": b["slot"], "to_slot": slot, "price": b["price"], "service": b["service"]}
        p = self.gate.propose(self.session_id, "reschedule", m, {}, self.wf.turn, self._ts())
        self.wf.target_booking, self.wf.pending_proposal_id = b, p.id
        self._goto(Phase.AWAITING_APPROVAL, "reschedule proposed")
        self._flags.add("proposed")
        return {"ok": True, "proposal": m, "note": "The app shows the user a confirmation card."}, False

    def _t_propose_cancel(self, inp: dict, user_text: str):
        b = self._booking_seen(inp["booking_id"])
        m = {"booking_id": b["booking_id"], "provider_id": b["provider_id"], "provider_name": b["provider_name"],
             "slot": b["slot"], "service": b["service"]}
        p = self.gate.propose(self.session_id, "cancel", m, {}, self.wf.turn, self._ts())
        self.wf.target_booking, self.wf.pending_proposal_id = b, p.id
        self._goto(Phase.AWAITING_APPROVAL, "cancel proposed")
        self._flags.add("proposed")
        return {"ok": True, "proposal": m, "note": "The app shows the user a confirmation card."}, False

    def _t_remember(self, inp: dict, user_text: str):
        key, value = str(inp.get("key", "")), str(inp.get("value", "")).strip()
        if not evidence_ok(inp.get("evidence", ""), user_text):
            self.tracer.event("blocked_memory_write", key=key, value=sanitize_for_trace(value),
                              reason="evidence is not a quote of the user's message")
            return {"error": "rejected: memories can only be saved from the user's own words"}, True
        if not KEY_RE.match(key) or key.split(".")[0] not in ALLOWED_MEMORY_PREFIXES or not value:
            raise ValueError(f"bad memory key/value {key!r}")
        if key == "area.preferred":
            value = next((a for a in catalog.AREAS if a.lower() == value.lower()), None) or ""
            if not value:
                raise ValueError("unsupported area")
        if key.startswith("time.") and not re.fullmatch(r"[0-2]\d:[0-5]\d", value):
            raise ValueError("time must be HH:MM")
        m = self.memory.add(self.user_id, key, value[:200], "explicit", self._ts(), evidence=inp["evidence"][:200])
        self.tracer.event("memory_write", id=m.id, key=key, source="explicit")
        return {"ok": True, "key": key, "value": m.value, "source": "explicit"}, False

    def _t_forget(self, inp: dict, user_text: str):
        key = str(inp.get("key", ""))
        if not evidence_ok(inp.get("evidence", ""), user_text):
            self.tracer.event("blocked_memory_write", key=key, reason="forget without user evidence")
            return {"error": "rejected: only the user can ask to forget"}, True
        # also scrub the original statements that created the memory
        evid = [r["evidence"] for r in self.store.q("SELECT evidence FROM memories WHERE user_id=? AND key=? AND "
                                                    "status IN ('active','superseded') AND evidence!=''",
                                                    self.user_id, key)]
        values = self.memory.forget(self.user_id, key, self._ts())
        self.memory.scrub_history(self.user_id, evid)
        r = self.wf.request
        for fld, src in list(r.sources.items()):
            if src.startswith("memory:") and fld in ("area", "earliest_time", "budget_max"):
                mem = self.memory.get(int(src.split(":")[1]))
                if mem and mem.status == "forgotten":
                    setattr(r, fld, None)
                    if fld == "budget_max":
                        r.budget_set = False
                    r.sources.pop(fld)
        self.tracer.event("memory_forgotten", key=key, rows=len(values))
        return {"ok": True, "key": key, "forgotten_values": len(values)}, False

    def _t_show_memories(self, inp: dict, user_text: str):
        mems = self.memory.all(self.user_id, include_inactive=True)
        active = [m for m in mems if m.status == "active"]
        lines = ["**What I remember about you**"]
        for m in active:
            lines.append(f"- {FRIENDLY.get(m.key, m.key)}: {m.value}  _({m.label()})_")
        gone = [m for m in mems if m.status == "forgotten"]
        if gone:
            lines.append(f"- Forgotten at your request: {', '.join(sorted({m.key for m in gone}))} (values deleted)")
        lines.append("Say 'forget my <thing>' or 'my <thing> is now ...' to change anything. Inferred items are "
                     "never used until you confirm them.")
        self._direct.append("\n".join(lines))
        return {"ok": True, "displayed_items": len(active),
                "note": "Shown to the user directly; values are not repeated to you."}, False

    # --------------------------------------------------------- memory confirmation prompts
    def _maybe_memory_prompt(self) -> str | None:
        if not any(o.code == "booked" for o in self._verified):
            return None
        for m in self.memory.pending_inferences(self.user_id):
            if m.id in self.wf.prompted_memories or m.id not in self.wf.inference_candidates:
                continue
            self.wf.prompted_memories.append(m.id)
            self.wf.pending_memory_prompt = m.id
            self.tracer.event("memory_confirmation_requested", memory_id=m.id, key=m.key)
            return (f"By the way, should I remember **{FRIENDLY.get(m.key, m.key)}: {m.value}** as a general "
                    "preference for future bookings? (yes / no)")
        return None

    def _memory_prompt_answer(self, text: str) -> tuple[str, str] | None:
        mid = self.wf.pending_memory_prompt
        self.wf.pending_memory_prompt = None
        stripped = re.sub(r"\b(remember|save|store)\b( it| that)?", "", text, flags=re.I)
        c = classify_reply(stripped) if stripped.strip() else "approve"
        m = self.memory.get(mid)
        if not m or m.status != "active":
            return None
        if c == "approve":
            self.memory.confirm(mid, self._ts())
            self.tracer.event("memory_confirmed", memory_id=mid, key=m.key)
            return f"Saved - I'll use {FRIENDLY.get(m.key, m.key).lower()} {m.value} for future bookings.", "memory_confirmed"
        if c == "reject":
            self.memory.reject(mid, self._ts())
            self.tracer.event("memory_rejected", memory_id=mid, key=m.key)
            return "Okay, I won't remember that.", "memory_rejected"
        return None

    # --------------------------------------------------------- rendering
    @staticmethod
    def render_card(p: Proposal) -> str:
        m = p.material
        if p.kind == "book":
            sp = p.context.get("search_price")
            note = f" (search listed ₹{sp}; this is the provider's quote)" if sp and sp != m["price"] else ""
            body = (f"**Confirm booking** - {m['provider_name']} ({catalog.SERVICES.get(m.get('service'), '')})\n"
                    f"- When: {_fmt_slot(m['slot'])}\n- Price: ₹{m['price']}{note}")
        elif p.kind == "cancel":
            body = (f"**Confirm cancellation** - booking {m['booking_id']} with {m['provider_name']}\n"
                    f"- When: {_fmt_slot(m['slot'])}")
        else:
            body = (f"**Confirm reschedule** - booking {m['booking_id']} with {m['provider_name']}\n"
                    f"- From: {_fmt_slot(m['from_slot'])}\n- To: {_fmt_slot(m['to_slot'])}\n"
                    f"- Price: ₹{m['price']} (unchanged)")
        return body + f"\nReply **yes** to confirm, or tell me what to change.  `ref {p.ref}`"
