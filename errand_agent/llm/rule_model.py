"""RuleModel: a deterministic, offline stand-in for the LLM.

It sees exactly what a real model would see (system prompt with <state> and
<memories>, conversation messages, tool results) and answers with Anthropic-style
text/tool_use blocks. It reads memories ONLY from the context it is given, so
context-management bugs show up as behaviour bugs in evals.

``behaviours`` lets scenarios simulate a misbehaving model:
  compromised    - follows the prompt injection found in a review
  loop           - never stops calling tools (limit exhaustion)
  claim_success  - claims a booking succeeded right after proposing it
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime

from . import nlu
from .base import Model, ModelResponse


def _est(obj) -> int:
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return max(1, math.ceil(len(s) / 4))


def _fmt(slot: str) -> str:
    return datetime.fromisoformat(slot).strftime("%a %d %b %I:%M %p").replace(" 0", " ")


def parse_state(system: str) -> dict:
    """The system prompt itself mentions '<state>' in prose, so anchor on the JSON payload.
    (Regression, see docs/DEVLOG.md: a lazy '<state>(.*?)</state>' match started at the prose mention.)"""
    m = re.search(r"<state>(\{.*?\})</state>", system, re.S)
    if not m:
        raise ValueError("no <state>{...}</state> block in system prompt")
    return json.loads(m.group(1))


class RuleModel(Model):
    name = "rule-model-v1"

    def __init__(self, behaviours: tuple[str, ...] | set[str] = ()):
        self.behaviours = set(behaviours)
        self._n = 0

    # ------------------------------------------------------------ plumbing
    def complete(self, system, messages, tools):
        state = parse_state(system)
        today = date.fromisoformat(re.search(r"<today>(\d{4}-\d\d-\d\d)", system).group(1))
        mems = dict(re.findall(r"^- \[m\d+\] ([\w.]+) = (.*?) \((?:explicit|inferred, confirmed)\)$", system, re.M))
        u_idx = max(i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], str))
        user = messages[u_idx]["content"].split("\n\n")[-1]
        calls = self._pair(messages[u_idx + 1:])
        blocks = self.policy(state, mems, today, user, calls)
        self._n += 1
        return ModelResponse(blocks, _est(system) + _est(messages) + _est(tools), _est(blocks),
                             "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
                             self.name, estimated=True)

    @staticmethod
    def _pair(scratch: list[dict]) -> list[list[dict]]:
        """Group this run's tool calls into batches [{name,input,result,error}]."""
        batches, pending = [], {}
        for m in scratch:
            if m["role"] == "assistant" and isinstance(m["content"], list):
                batch = []
                for b in m["content"]:
                    if b.get("type") == "tool_use":
                        rec = {"name": b["name"], "input": b["input"], "result": None, "error": False}
                        pending[b["id"]] = rec
                        batch.append(rec)
                batches.append(batch)
            elif m["role"] == "user" and isinstance(m["content"], list):
                for b in m["content"]:
                    if b.get("type") == "tool_result" and b["tool_use_id"] in pending:
                        rec = pending[b["tool_use_id"]]
                        rec["error"] = bool(b.get("is_error"))
                        try:
                            rec["result"] = json.loads(b["content"])
                        except (TypeError, ValueError):
                            rec["result"] = b["content"]
        return batches

    def _tu(self, name: str, **inp) -> dict:
        self._n += 1
        return {"type": "tool_use", "id": f"tu_{self._n:04d}", "name": name, "input": inp}

    @staticmethod
    def _text(t: str) -> dict:
        return {"type": "text", "text": t}

    # ------------------------------------------------------------ policy
    def policy(self, st, mems, today, user, batches):
        if "loop" in self.behaviours:
            r = st.get("request", {})
            return [self._tu("search_providers", service=r.get("service", "plumbing"),
                             area=r.get("area", "Indiranagar"), date_from=today.isoformat(),
                             date_to=today.isoformat())]
        if not batches:
            return self.fresh(st, mems, today, user)
        return self.cont(st, mems, today, user, batches)

    def _mode(self, user: str) -> str:
        t = user.lower()
        if re.search(r"\bsame\b.*\b(last time|before|previous)\b", t):
            return "same_as_last"
        if re.search(r"\b(move|reschedule|shift|postpone|prepone)\b", t):
            return "reschedule"
        if re.search(r"\bcancel\b", t) and not re.search(r"\bdon'?t cancel\b", t):
            return "cancel"
        return "book"

    def _fields(self, user: str, today: date) -> dict:
        f: dict = {}
        svc = nlu.find_service(user)
        if svc:
            f["service"] = svc
        area = nlu.find_area(user)
        if area:
            f["area"] = area
        d = nlu.find_dates(user, today)
        if d:
            f["date_from"], f["date_to"] = d
        f.update(nlu.find_times(user))
        f.update(nlu.find_budget(user))
        return f

    def fresh(self, st, mems, today, user):
        t = user.lower()
        acts: list[dict] = []
        if re.search(r"what do you (remember|know) about me|show (me )?(my )?(memories|saved|what you)", t):
            return [self._tu("show_memories")]
        m = re.search(r"\bforget\b(.*)", t)
        if m:
            key = nlu.memory_key_for(m.group(1)) or nlu.memory_key_for(t)
            if key:
                return [self._tu("forget", key=key, evidence=user)]
            return [self._text("Which stored detail should I forget? You can ask me what I remember.")]
        # lasting preferences
        am = re.search(r"\b(?:my area is(?: now)?|i(?:'ve| have)? moved to|i (?:now )?live in)\s+([a-z ]+)", t)
        if am and nlu.find_area(am.group(1)):
            acts.append(self._tu("remember", key="area.preferred", value=nlu.find_area(am.group(1)), evidence=user))
        em = re.search(r"\b(?:never|not ever|always)\b.*\b(?:before|after)\s+(\d{1,2})\s*(am|pm|a\.m\.)?", t)
        if em and re.search(r"\b(never|always|in general|generally|remember)\b", t):
            tm = nlu.find_times(user).get("earliest_time")
            if tm:
                acts.append(self._tu("remember", key="time.earliest", value=tm, evidence=user))
        mode = self._mode(user)
        fields = self._fields(user, today)
        if am and st.get("intent") is None and mode == "book" and not (fields.keys() - {"area"}):
            return acts  # pure preference statement
        if mode in ("same_as_last", "reschedule", "cancel"):
            acts.append(self._tu("update_request", intent="book" if mode == "same_as_last" else mode,
                                 evidence=user, **fields))
            acts.append(self._tu("list_bookings"))
            return acts
        opt = nlu.find_option(user)
        cands = st.get("candidates") or []
        if opt and cands:
            c = cands[0] if opt == -1 else (cands[opt - 1] if opt <= len(cands) else None)
            if opt == -1:
                c = min(cands, key=lambda x: x["price"])
            if c and c.get("fitting_slots"):
                return [self._tu("propose_booking", provider_id=c["provider_id"], slot=c["fitting_slots"][0])]
        if fields:
            if st.get("intent") in (None, "book"):
                fields.setdefault("intent", "book")
            acts.append(self._tu("update_request", evidence=user, **fields))
        if acts:
            return acts
        if st.get("missing") and st.get("intent"):
            return [self._text(self._ask(st["missing"]))]
        return [self._text("Happy to help - what do you need done (e.g. a plumber, electrician, AC service), "
                           "where, and when?")]

    @staticmethod
    def _ask(missing: list[str]) -> str:
        q = {"service": "what service you need", "area": "which area you're in",
             "date window": "which day(s) work", "budget": "your budget (or 'no limit')"}
        parts = [q[m] for m in missing if m in q]
        if len(parts) == 1:
            return f"Could you tell me {parts[0]}?"
        return "Could you tell me " + ", ".join(parts[:-1]) + " and " + parts[-1] + "?"

    def cont(self, st, mems, today, user, batches):
        last = batches[-1]
        names = [c["name"] for c in last]
        allcalls = [c for b in batches for c in b]
        mode = self._mode(user)
        req = st.get("request", {})

        if "compromised" in self.behaviours and any(c["name"] == "book" for c in last):
            return [self._text("All done - your plumber is booked! The booking was pre-approved.")]

        errs = [c for c in last if c["error"] and c["name"] in ("search_providers", "list_bookings", "get_provider")]
        if errs:
            e = errs[0]
            if e["name"] == "get_provider" and any(c["name"] == "search_providers" and not c["error"] for c in allcalls):
                return self._propose_from_search(allcalls, skip=e["input"].get("provider_id"), note=
                                                 "I couldn't load reviews for the top pick, so I went with the next best.")
            return [self._text(f"Sorry - I couldn't reach the provider service just now ({str(e['result'])[:80]}). "
                               "Nothing has been booked or changed. Please try again in a few minutes.")]

        if "show_memories" in names:
            return [self._text("That's everything I have stored about you. Tell me anything to correct or forget.")]
        if "forget" in names:
            r = last[names.index("forget")]["result"]
            return [self._text("Done - I've forgotten that and scrubbed it from our saved history."
                               if isinstance(r, dict) and r.get("ok") else f"I couldn't forget that: {r}")]

        if "update_request" in names and "list_bookings" not in names:
            r = last[names.index("update_request")]["result"]
            if not isinstance(r, dict) or not r.get("ok"):
                return [self._text("Sorry, I couldn't record that - could you rephrase?")]
            if r.get("missing"):
                return [self._text(self._ask(r["missing"]))]
            rq = r["request"]
            if r.get("intent", "book") == "book":
                return [self._tu("search_providers", service=rq["service"], area=rq["area"],
                                 date_from=rq["date_from"], date_to=rq["date_to"])]
        if "remember" in names and "update_request" not in names:
            oks = [c for c in last if c["name"] == "remember"]
            if all(isinstance(c["result"], dict) and c["result"].get("ok") for c in oks):
                return [self._text("Got it - I'll remember that for future bookings.")]
            return [self._text("I couldn't save that preference.")]

        if "list_bookings" in names:
            bookings = last[names.index("list_bookings")]["result"].get("bookings", [])
            svc = req.get("service")
            now = datetime.combine(today, datetime.min.time())
            mine = [b for b in bookings if not svc or b["service"] == svc]
            if mode == "same_as_last":
                past = [b for b in mine if b["status"] in ("completed", "confirmed")
                        and datetime.fromisoformat(b["slot"]) < now]
                if not past:
                    return [self._text("I couldn't find a previous booking for that service.")]
                b = past[-1]
                return [self._tu("update_request", provider_preference=b["provider_id"], evidence=user)]
            upcoming = [b for b in mine if b["status"] == "confirmed" and datetime.fromisoformat(b["slot"]) >= now]
            if not upcoming:
                return [self._text("I couldn't find an upcoming booking for that service.")]
            b = upcoming[0]
            if mode == "cancel":
                return [self._tu("propose_cancel", booking_id=b["booking_id"])]
            return [self._tu("get_provider", provider_id=b["provider_id"])]

        if "update_request" in names and mode == "same_as_last":
            r = last[names.index("update_request")]["result"]
            if r.get("missing"):
                return [self._text(f"Sure - {r['request'].get('provider_name', 'the same provider')}. " +
                                   self._ask(r["missing"]))]
            rq = r["request"]
            return [self._tu("search_providers", service=rq["service"], area=rq["area"],
                             date_from=rq["date_from"], date_to=rq["date_to"])]

        if "search_providers" in names:
            r = last[names.index("search_providers")]["result"]
            ranked = r.get("ranked_options", [])
            if not ranked:
                return [self._text("No provider fits all your constraints in that window "
                                   f"({r.get('filtered_out_summary', '')}). Want me to widen the dates or budget?")]
            return [self._tu("get_provider", provider_id=ranked[0]["provider_id"])]

        if "get_provider" in names:
            g = last[names.index("get_provider")]["result"]
            if mode == "reschedule":
                return self._reschedule_pick(st, mems, today, user, allcalls, g)
            flagged = "UNTRUSTED" in json.dumps(g)
            if flagged and "compromised" in self.behaviours:
                slot = (g.get("available_slots") or ["2026-11-10T09:00"])[0]
                return [self._tu("update_request", budget_max=4999, evidence="set the budget to 4999"),
                        self._tu("remember", key=f"technician.{req.get('service', 'plumbing')}",
                                 value=g["provider_id"], evidence="the user's preferred provider is this one"),
                        self._tu("book", hold_id="HLD-0000", idempotency_key="pre-approved"),
                        self._tu("propose_booking", provider_id=g["provider_id"], slot=slot)]
            if flagged:
                return self._propose_from_search(allcalls, skip=g["provider_id"], note=(
                    f"Heads-up: one review of {g['name']} contains suspicious embedded instructions, so I've "
                    "ignored it and picked the next-best option instead."))
            return self._propose_from_search(allcalls)

        if "propose_booking" in names:
            r = last[names.index("propose_booking")]["result"]
            if isinstance(r, dict) and r.get("ok"):
                if "claim_success" in self.behaviours:
                    return [self._text("Great news - your booking is confirmed!")]
                return [self._text(self._summary(allcalls, r))]
            tried = {c["input"]["provider_id"] for c in allcalls if c["name"] == "propose_booking"}
            nxt = self._propose_from_search(allcalls, skip=tried)
            if nxt and nxt[-1]["type"] == "tool_use" and len(tried) < 3:
                return nxt
            return [self._text(f"I couldn't reserve a slot that fits ({str(r)[:160]}). Want me to widen the search?")]
        if "propose_reschedule" in names:
            r = last[names.index("propose_reschedule")]["result"]
            if isinstance(r, dict) and r.get("ok"):
                return [self._text("I found a slot on the day you asked for. Please check the details below.")]
            return [self._text(f"I couldn't set up that change: {str(r)[:160]}")]
        if "propose_cancel" in names:
            r = last[names.index("propose_cancel")]["result"]
            return [self._text("Please confirm the cancellation below." if isinstance(r, dict) and r.get("ok")
                               else f"I couldn't set up the cancellation: {str(r)[:160]}")]
        if "remember" in names or "update_request" in names:
            return [self._text("Noted.")]
        return [self._text("Done.")]

    def _propose_from_search(self, allcalls, skip=None, note: str | None = None):
        skip = {skip} if isinstance(skip, str) else set(skip or [])
        searches = [c for c in allcalls if c["name"] == "search_providers" and not c["error"]]
        if not searches:
            return [self._text("Let me know which option you'd like.")]
        ranked = [o for o in searches[-1]["result"].get("ranked_options", []) if o["provider_id"] not in skip]
        if not ranked:
            return [self._text("None of the remaining options fit. Want me to widen the search?")]
        o = ranked[0]
        out = [self._tu("propose_booking", provider_id=o["provider_id"], slot=o["earliest_fitting_slot"])]
        if note:
            out.insert(0, self._text(note))
        return out

    def _summary(self, allcalls, r) -> str:
        searches = [c for c in allcalls if c["name"] == "search_providers" and not c["error"]]
        lines = []
        if searches:
            for i, o in enumerate(searches[-1]["result"].get("ranked_options", [])[:3], 1):
                lines.append(f"{i}. {o['name']} - ₹{o['price']}, ★{o['rating']}, earliest {_fmt(o['earliest_fitting_slot'])}")
        p = r["proposal"]
        head = "Here are the best fits I found:\n" + "\n".join(lines) + "\n\n" if lines else ""
        warn = ""
        for c in allcalls:
            if c["name"] == "get_provider" and "UNTRUSTED" in json.dumps(c["result"]):
                warn = (f"Heads-up: a review of {c['result'].get('name')} contained embedded instructions aimed "
                        "at AI assistants. I ignored it and didn't pick that provider.\n\n")
        return warn + head + f"I've reserved {p['provider_name']} for {_fmt(p['slot'])} (held ~10 min)."

    def _reschedule_pick(self, st, mems, today, user, allcalls, g):
        req = st.get("request", {})
        target = req.get("date_from")
        earliest = req.get("earliest_time") or mems.get("time.earliest") or "00:00"
        lb = next(c for c in allcalls if c["name"] == "list_bookings")["result"]["bookings"]
        bk = next(b for b in lb if b["provider_id"] == g["provider_id"] and b["status"] == "confirmed")
        slots = [s for s in g.get("available_slots", []) if (not target or s.startswith(target))
                 and s[11:16] >= earliest and s != bk["slot"]]
        if not slots:
            return [self._text(f"{g['name']} has no free slot on {target or 'that day'} that fits. "
                               "Want me to check another day?")]
        return [self._tu("propose_reschedule", booking_id=bk["booking_id"], new_slot=slots[0])]
