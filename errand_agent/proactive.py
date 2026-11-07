"""Proactive follow-up: detect provider-side cancellations without a user prompt.

Detection uses only contract tools: ``list_bookings`` is polled and diffed against
the bookings this agent created (``known_bookings``). Policy:
  * slot already in the past      -> no notification (nothing actionable), logged
  * user busy with another action -> notify only, offer to find a replacement
  * otherwise                     -> notify + search a replacement inside the user's
                                     known preferences, hold it, and ask for FRESH
                                     approval. Never rebook automatically.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from .executor import _fmt_slot
from .limits import Budget, LimitExceeded
from .mocks import catalog
from .state import Phase, Request
from .tools import ToolFailure


class ProactiveMonitor:
    def __init__(self, agent):
        self.a = agent

    def tick(self) -> list[str]:
        a = self.a
        a.tracer.start_run(a.session_id, "proactive_tick")
        a.tools.budget = Budget(a.limits, a.clock)
        notes: list[str] = []
        try:
            rows = a.tools.call("list_bookings", user_id=a.user_id)["bookings"]
        except (ToolFailure, LimitExceeded) as e:
            a.tracer.end_run("monitor_error", str(e))
            return notes
        live = {r["booking_id"]: r for r in rows}
        known = a.store.q("SELECT * FROM known_bookings WHERE user_id=? AND status='confirmed' AND handled IS NULL",
                          a.user_id)
        for k in known:
            cur = live.get(k["booking_id"])
            if not cur or cur["status"] != "cancelled_by_provider":
                continue
            a.tracer.event("provider_cancellation_detected", booking_id=k["booking_id"], slot=cur["slot"])
            msg = self._handle(cur)
            if msg:
                notes.append(msg)
        a._save()
        a.tracer.end_run("notified" if notes else "no_change", " | ".join(notes)[:300], notifications=len(notes))
        return notes

    def _handle(self, b: dict) -> str | None:
        a = self.a
        now = a.clock.now().replace(tzinfo=None)
        slot_dt = datetime.fromisoformat(b["slot"])
        if slot_dt <= now:
            a.store.x("UPDATE known_bookings SET status=?, handled='past_no_action' WHERE booking_id=?",
                      b["status"], b["booking_id"])
            a.tracer.event("decision", what="no_notification", why="cancelled booking is already in the past",
                           booking_id=b["booking_id"])
            return None
        a.store.x("UPDATE known_bookings SET status=?, handled='notified' WHERE booking_id=?",
                  b["status"], b["booking_id"])
        urgent = slot_dt - now < timedelta(hours=24)
        head = (f"{'⚠️ Urgent: ' if urgent else 'Heads-up: '}{b['provider_name']} cancelled your "
                f"{catalog.SERVICES[b['service']].lower()} visit on {_fmt_slot(b['slot'])}.")
        busy = a.gate.pending(a.session_id) or a.wf.phase in (Phase.EXECUTING, Phase.VERIFYING)
        a.tracer.event("decision", what="notify", urgent=urgent, find_replacement=not busy)
        if busy:
            return self._deliver(head + " Reply 'find a replacement' when you're ready and I'll look for one.")

        wf = a.wf
        if wf.phase != Phase.IDLE:
            wf.reset_for_new_task(a.tracer)
        wf.intent, wf.origin = "book", "proactive"
        mem = lambda k: a.memory.value(a.user_id, k)  # eligible (explicit/confirmed) values only
        d0 = max(slot_dt.date(), now.date())
        prior = b.get("price") or catalog.PROVIDERS[b["provider_id"]]["base_price"]
        budget = mem(f"budget.{b['service']}")
        wf.request = Request(
            service=b["service"], area=mem("area.preferred") or "Indiranagar",
            date_from=d0.isoformat(), date_to=(d0 + timedelta(days=2)).isoformat(),
            earliest_time=mem("time.earliest"), latest_time=mem("time.latest"),
            budget_max=int(budget) if budget else int(prior * 1.25), budget_set=True,
            exclude_providers=[b["provider_id"]],
            sources={"service": "event", "area": "memory/event", "earliest_time": "memory",
                     "budget_max": "memory" if budget else "event:prior_price*1.25"})
        prefs = (f"in {wf.request.area}"
                 + (f", not before {wf.request.earliest_time}" if wf.request.earliest_time else "")
                 + f", up to ₹{wf.request.budget_max}")
        a.tracer.event("replacement_request", request=wf.request.__dict__)
        wf.transition(Phase.SEARCHING, "proactive replacement search", a.tracer)
        try:
            res = a.tools.call("search_providers", service=wf.request.service, area=wf.request.area,
                               date_range=[wf.request.date_from, wf.request.date_to])
        except (ToolFailure, LimitExceeded) as e:
            wf.transition(Phase.IDLE, "replacement search failed", a.tracer)
            return self._deliver(head + f" I couldn't search for a replacement just now ({e}). Reply 'find a "
                                        "replacement' to retry.")
        ranked, filtered = a.rank(res["providers"])
        wf.candidates = ranked
        for c in ranked:
            p = a.executor.prepare_booking(wf, c["provider_id"], c["fitting_slots"][0], c["price"], c["name"])
            if hasattr(p, "id"):
                wf.pending_proposal_id = p.id
                wf.transition(Phase.AWAITING_APPROVAL, "proactive replacement proposed", a.tracer)
                a.gate.mark_shown(p.id, wf.turn)
                return self._deliver(head + f" I found a replacement within your preferences ({prefs}). "
                                            "Nothing is booked until you confirm:\n\n" + a.render_card(p))
        wf.transition(Phase.IDLE, "no replacement fits", a.tracer)
        return self._deliver(head + f" I couldn't find a replacement within your preferences ({prefs}; "
                                    f"filtered: {filtered}). Want me to widen the dates or budget?")

    def _deliver(self, text: str) -> str:
        a = self.a
        a.store.push_outbox(a.user_id, "provider_cancellation", text, a._ts())
        a.store.add_message(a.session_id, a.user_id, "assistant", text, a._ts())
        a.tracer.event("notification_sent", channel="outbox", text=text[:200])
        return text
