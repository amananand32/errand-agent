"""Mock home-services marketplace implementing the fixed tool contracts.

All randomness is derived from ``ScenarioConfig.seed`` plus a persisted per-tool
call counter, so a run is reproducible and survives a process restart when a
``state_path`` is given (the provider service is "external": it keeps its state
even when the agent process dies).

Business outcomes (slot_taken, hold_expired, slot_conflict) are *returned* as a
``status`` field. Transport failures (503, 500, timeout) are *raised*.
"""
from __future__ import annotations

import copy
import json
import os
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ..clock import IST, Clock, FakeClock
from . import catalog


class ToolError(Exception):
    def __init__(self, code: str, message: str = "", retryable: bool = False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


class TransientError(ToolError):
    def __init__(self, code: str = "503", message: str = "service unavailable"):
        super().__init__(code, message, retryable=True)


class ToolTimeout(ToolError):
    def __init__(self, message: str = "request timed out"):
        super().__init__("timeout", message, retryable=False)


@dataclass
class ScenarioConfig:
    seed: int = 7
    # Outcomes for successive book() calls; once exhausted the seeded RNG is used
    # (70% success, 15% slot_taken, 15% timeout of which half actually booked).
    book_outcomes: list[str] = field(default_factory=list)
    # Per-provider list of price deltas applied to successive hold_slot() calls.
    hold_price_deltas: dict[str, list[int]] = field(default_factory=dict)
    # Probability that a hold quotes a different price when no explicit delta given.
    random_price_drift: float = 0.0
    reschedule_conflict_slots: list[str] = field(default_factory=list)
    # tool name -> list of injected failures for successive calls:
    #   "503", "500", "timeout", "crash" (unexpected exception)
    inject_failures: dict[str, list[str]] = field(default_factory=dict)
    always_fail: dict[str, str] = field(default_factory=dict)
    # Forced results for successive get_booking_status() calls, e.g. ["pending"].
    status_overrides: list[str] = field(default_factory=list)
    latency_s: dict[str, float] = field(default_factory=dict)
    hold_ttl_minutes: int = 10
    timeout_s: float = 8.0
    first_cancel_fails: bool = True

    def to_json(self) -> dict:
        return copy.deepcopy(self.__dict__)


DEFAULT_LATENCY = {
    "search_providers": 0.35,
    "get_provider": 0.2,
    "hold_slot": 0.25,
    "book": 0.6,
    "get_booking_status": 0.15,
    "cancel": 0.3,
    "reschedule": 0.3,
    "list_bookings": 0.15,
}

def _seed_bookings(user_id: str) -> dict[str, dict]:
    rows = [
        ("BK-0999", "PRV-ELE-01", "2026-07-20T10:00", "completed", 600),
        ("BK-1002", "PRV-PLB-02", "2026-09-03T10:00", "completed", 750),
        ("BK-1001", "PRV-ELE-02", "2026-10-12T11:00", "completed", 900),
        ("BK-1003", "PRV-AC-02", "2026-11-12T10:00", "confirmed", 1000),
    ]
    out = {}
    for bid, pid, slot, status, price in rows:
        p = catalog.PROVIDERS[pid]
        out[bid] = {
            "booking_id": bid, "user_id": user_id, "provider_id": pid,
            "provider_name": p["name"], "technician": p["technician"],
            "service": p["service"], "slot": slot, "status": status, "price": price,
            "area": "Indiranagar", "idempotency_key": None, "hold_id": None,
        }
    return out


class MockProviderService:
    TOOL_NAMES = ["search_providers", "get_provider", "hold_slot", "book",
                  "get_booking_status", "cancel", "reschedule", "list_bookings"]

    def __init__(self, config: ScenarioConfig | None = None, clock: Clock | None = None,
                 user_id: str = "u_demo", state_path: str | None = None):
        self.config = config or ScenarioConfig()
        self.clock = clock or FakeClock()
        self.user_id = user_id  # the authenticated caller (like a bearer token)
        self.state_path = state_path
        if state_path and os.path.exists(state_path):
            with open(state_path) as f:
                self.state = json.load(f)
        else:
            self.state = {
                "holds": {}, "bookings": _seed_bookings(user_id), "by_key": {},
                "cancel_attempts": {}, "op_keys": {}, "counters": {}, "consumed": {},
                "taken_slots": [], "events": [], "next_id": 2000, "call_log": [],
            }
            self._save()

    # ------------------------------------------------------------------ utils
    def _save(self) -> None:
        if self.state_path:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.state_path)

    def _rng(self, tool: str) -> random.Random:
        n = self.state["counters"].get(tool, 0)
        self.state["counters"][tool] = n + 1
        return random.Random(f"{self.config.seed}:{tool}:{n}")

    def _next_forced(self, name: str, seq: list) -> Any:
        i = self.state["consumed"].get(name, 0)
        if i < len(seq):
            self.state["consumed"][name] = i + 1
            return seq[i]
        return None

    def _new_id(self, prefix: str) -> str:
        self.state["next_id"] += 1
        return f"{prefix}-{self.state['next_id']}"

    def _enter(self, tool: str, args: dict) -> None:
        """Common prologue: log call, simulate latency, inject failures."""
        self.state["call_log"].append({"tool": tool, "args": args,
                                       "at": self.clock.now().isoformat()})
        self.clock.sleep(self.config.latency_s.get(tool, DEFAULT_LATENCY.get(tool, 0.1)))
        mode = self.config.always_fail.get(tool) or self._next_forced(
            f"fail:{tool}", self.config.inject_failures.get(tool, []))
        if mode:
            self._save()
            if mode in ("503", "500"):
                raise TransientError(mode, f"{tool}: upstream {mode}")
            if mode == "timeout":
                self.clock.sleep(self.config.timeout_s)
                raise ToolTimeout(f"{tool} timed out")
            if mode == "crash":
                raise RuntimeError(f"{tool}: unexpected NoneType in provider adapter")
            raise ToolError(mode, f"{tool}: injected failure")

    def _price(self, pid: str) -> int:
        return catalog.PROVIDERS[pid]["base_price"]

    def _busy_slots(self, pid: str) -> set[str]:
        now = self.clock.now()
        busy = {b["slot"] for b in self.state["bookings"].values()
                if b["provider_id"] == pid and b["status"] in ("confirmed",)}
        busy |= {h["slot"] for h in self.state["holds"].values()
                 if h["provider_id"] == pid and not h.get("consumed")
                 and datetime.fromisoformat(h["expires_at"]) > now}
        busy |= {s.split("|", 1)[1] for s in self.state["taken_slots"] if s.startswith(pid + "|")}
        return busy

    def _base_slots(self, pid: str, day: date) -> list[str]:
        start, end = catalog.PROVIDERS[pid]["hours"]
        rng = random.Random(f"{self.config.seed}:{pid}:{day.isoformat()}")
        hours = list(range(start, end))
        picked = sorted(rng.sample(hours, k=min(len(hours), rng.randint(3, 5))))
        if day.weekday() == 6 and pid.endswith("-05"):
            return []  # a provider that rests on Sundays
        return [f"{day.isoformat()}T{h:02d}:00" for h in picked]

    def available_slots(self, pid: str, d_from: date, d_to: date) -> list[str]:
        now_naive = self.clock.now().replace(tzinfo=None)
        busy = self._busy_slots(pid)
        out, d = [], d_from
        while d <= d_to:
            for s in self._base_slots(pid, d):
                if s not in busy and datetime.fromisoformat(s) > now_naive:
                    out.append(s)
            d += timedelta(days=1)
        return out

    @staticmethod
    def _parse_range(date_range: Any) -> tuple[date, date]:
        if isinstance(date_range, dict):
            a, b = date_range.get("from"), date_range.get("to")
        elif isinstance(date_range, (list, tuple)):
            a, b = date_range
        else:
            a, b = str(date_range).split("/")
        try:
            d1, d2 = date.fromisoformat(a), date.fromisoformat(b)
        except Exception as e:  # noqa: BLE001
            raise ToolError("invalid_argument", f"bad date_range {date_range!r}: {e}")
        if d2 < d1 or (d2 - d1).days > 14:
            raise ToolError("invalid_argument", "date_range must be ordered and <= 14 days")
        return d1, d2

    # ------------------------------------------------------------------ tools
    def search_providers(self, service: str, area: str, date_range: Any) -> dict:
        self._enter("search_providers", {"service": service, "area": area, "date_range": date_range})
        if service not in catalog.SERVICES:
            raise ToolError("invalid_argument", f"unknown service {service!r}; "
                            f"expected one of {sorted(catalog.SERVICES)}")
        if area not in catalog.AREAS:
            raise ToolError("invalid_argument", f"unknown area {area!r}; expected one of {catalog.AREAS}")
        d1, d2 = self._parse_range(date_range)
        pids = [p for p, v in catalog.PROVIDERS.items() if v["service"] == service][:8]
        results = []
        for pid in pids:
            p = catalog.PROVIDERS[pid]
            results.append({
                "provider_id": pid, "name": p["name"], "price": self._price(pid),
                "rating": p["rating"], "review_count": 20 + (sum(map(ord, pid)) % 180),
                "available_slots": self.available_slots(pid, d1, d2),
                "attributes": {"verified": p["verified"], "years_experience": p["years_experience"],
                               "technician": p["technician"], "area": area},
            })
        self._save()
        return {"service": service, "area": area, "date_range": [d1.isoformat(), d2.isoformat()],
                "providers": results}

    def get_provider(self, provider_id: str) -> dict:
        self._enter("get_provider", {"provider_id": provider_id})
        p = catalog.PROVIDERS.get(provider_id)
        if not p:
            raise ToolError("not_found", f"provider {provider_id} not found")
        rng = random.Random(f"{self.config.seed}:reviews:{provider_id}")
        reviews = [{"rating": 5, "text": t} for t in rng.sample(catalog.GOOD_REVIEWS, 2)]
        reviews.append({"rating": 3, "text": rng.choice(catalog.MEH_REVIEWS)})
        if provider_id in catalog.INJECTED_PROVIDERS:
            reviews.insert(1, {"rating": 5, "text": catalog.INJECTION_REVIEW})
        today = self.clock.now().date()
        self._save()
        return {
            "provider_id": provider_id, "name": p["name"], "service": p["service"],
            "technician": p["technician"], "rating": p["rating"], "base_price": p["base_price"],
            "verified": p["verified"], "years_experience": p["years_experience"],
            "working_hours": f"{p['hours'][0]:02d}:00-{p['hours'][1]:02d}:00",
            "available_slots": self.available_slots(provider_id, today, today + timedelta(days=7)),
            "reviews": reviews,
        }

    def hold_slot(self, provider_id: str, slot: str) -> dict:
        self._enter("hold_slot", {"provider_id": provider_id, "slot": slot})
        if provider_id not in catalog.PROVIDERS:
            raise ToolError("not_found", f"provider {provider_id} not found")
        try:
            day = datetime.fromisoformat(slot).date()
        except ValueError:
            raise ToolError("invalid_argument", f"bad slot {slot!r}")
        if slot not in self.available_slots(provider_id, day, day):
            self._save()
            return {"status": "slot_unavailable", "provider_id": provider_id, "slot": slot}
        rng = self._rng("hold_slot")
        delta = self._next_forced(f"price:{provider_id}", self.config.hold_price_deltas.get(provider_id, []))
        if delta is None:
            delta = rng.choice([150, 200, 300]) if rng.random() < self.config.random_price_drift else 0
        hold_id = self._new_id("HLD")
        expires = self.clock.now() + timedelta(minutes=self.config.hold_ttl_minutes)
        hold = {"hold_id": hold_id, "provider_id": provider_id, "slot": slot,
                "quoted_price": self._price(provider_id) + delta,
                "expires_at": expires.isoformat(), "consumed": False}
        self.state["holds"][hold_id] = hold
        self._save()
        return {"status": "held", **{k: v for k, v in hold.items() if k != "consumed"}}

    def book(self, hold_id: str, idempotency_key: str) -> dict:
        self._enter("book", {"hold_id": hold_id, "idempotency_key": idempotency_key})
        if idempotency_key in self.state["by_key"]:
            b = self.state["bookings"][self.state["by_key"][idempotency_key]]
            return {"status": "confirmed", "booking_id": b["booking_id"], "provider_id": b["provider_id"],
                    "slot": b["slot"], "price": b["price"], "idempotent_replay": True}
        hold = self.state["holds"].get(hold_id)
        if not hold:
            raise ToolError("invalid_hold", f"hold {hold_id} not found")
        if self.clock.now() > datetime.fromisoformat(hold["expires_at"]):
            self._save()
            return {"status": "hold_expired", "hold_id": hold_id}
        if hold["consumed"]:
            return {"status": "slot_taken", "hold_id": hold_id}
        outcome = self._next_forced("book", self.config.book_outcomes)
        if outcome is None:
            r = self._rng("book").random()
            outcome = ("success" if r < 0.70 else "slot_taken" if r < 0.85
                       else "timeout_booked" if r < 0.925 else "timeout_not_booked")
        if outcome == "slot_taken":
            hold["consumed"] = True
            self.state["taken_slots"].append(f"{hold['provider_id']}|{hold['slot']}")
            self._save()
            return {"status": "slot_taken", "hold_id": hold_id}
        if outcome == "timeout_not_booked":
            self._save()
            self.clock.sleep(self.config.timeout_s)
            raise ToolTimeout("book timed out")
        booking = self._create_booking(hold, idempotency_key)
        self._save()
        if outcome == "timeout_booked":
            self.clock.sleep(self.config.timeout_s)
            raise ToolTimeout("book timed out")
        return {"status": "confirmed", "booking_id": booking["booking_id"],
                "provider_id": booking["provider_id"], "slot": booking["slot"], "price": booking["price"]}

    def _create_booking(self, hold: dict, key: str) -> dict:
        p = catalog.PROVIDERS[hold["provider_id"]]
        bid = self._new_id("BK")
        booking = {"booking_id": bid, "user_id": self.user_id, "provider_id": hold["provider_id"],
                   "provider_name": p["name"], "technician": p["technician"], "service": p["service"],
                   "slot": hold["slot"], "status": "confirmed", "price": hold["quoted_price"],
                   "area": "Indiranagar", "idempotency_key": key, "hold_id": hold["hold_id"]}
        hold["consumed"] = True
        self.state["bookings"][bid] = booking
        self.state["by_key"][key] = bid
        return booking

    def get_booking_status(self, hold_id: str | None = None, idempotency_key: str | None = None) -> dict:
        self._enter("get_booking_status", {"hold_id": hold_id, "idempotency_key": idempotency_key})
        forced = self._next_forced("status", self.config.status_overrides)
        if forced:
            return {"status": forced}
        b = None
        if idempotency_key and idempotency_key in self.state["by_key"]:
            b = self.state["bookings"][self.state["by_key"][idempotency_key]]
        elif hold_id:
            b = next((x for x in self.state["bookings"].values() if x.get("hold_id") == hold_id), None)
        if not b:
            return {"status": "not_found"}
        return {"status": b["status"], "booking_id": b["booking_id"], "provider_id": b["provider_id"],
                "slot": b["slot"], "price": b["price"]}

    def cancel(self, booking_id: str, idempotency_key: str) -> dict:
        self._enter("cancel", {"booking_id": booking_id, "idempotency_key": idempotency_key})
        prev = self.state["op_keys"].get(idempotency_key)
        if prev:
            return prev
        b = self.state["bookings"].get(booking_id)
        if not b or b["user_id"] != self.user_id:
            raise ToolError("not_found", f"booking {booking_id} not found")
        n = self.state["cancel_attempts"].get(booking_id, 0)
        self.state["cancel_attempts"][booking_id] = n + 1
        if n == 0 and self.config.first_cancel_fails:
            self._save()
            raise TransientError("503", "cancel temporarily unavailable")
        if b["status"] != "confirmed":
            res = {"status": "not_cancellable", "booking_id": booking_id, "current_status": b["status"]}
        else:
            b["status"] = "cancelled"
            res = {"status": "cancelled", "booking_id": booking_id}
        self.state["op_keys"][idempotency_key] = res
        self._save()
        return res

    def reschedule(self, booking_id: str, slot: str, idempotency_key: str) -> dict:
        self._enter("reschedule", {"booking_id": booking_id, "slot": slot, "idempotency_key": idempotency_key})
        prev = self.state["op_keys"].get(idempotency_key)
        if prev:
            return prev
        b = self.state["bookings"].get(booking_id)
        if not b or b["user_id"] != self.user_id:
            raise ToolError("not_found", f"booking {booking_id} not found")
        if b["status"] != "confirmed":
            return {"status": "not_reschedulable", "booking_id": booking_id, "current_status": b["status"]}
        if slot in self.config.reschedule_conflict_slots:
            self._save()
            return {"status": "slot_conflict", "booking_id": booking_id, "slot": slot}
        day = datetime.fromisoformat(slot).date()
        if slot not in self.available_slots(b["provider_id"], day, day):
            self._save()
            return {"status": "slot_conflict", "booking_id": booking_id, "slot": slot}
        b["slot"] = slot
        res = {"status": "rescheduled", "booking_id": booking_id, "slot": slot, "price": b["price"]}
        self.state["op_keys"][idempotency_key] = res
        self._save()
        return res

    def list_bookings(self, user_id: str) -> dict:
        self._enter("list_bookings", {"user_id": user_id})
        rows = [{k: b[k] for k in ("booking_id", "provider_id", "provider_name", "service", "slot",
                                   "status", "price", "technician")}
                for b in self.state["bookings"].values() if b["user_id"] == user_id]
        rows.sort(key=lambda r: r["slot"])
        self._save()
        return {"user_id": user_id, "bookings": rows}

    # ------------------------------------------------- simulation hooks (not tools)
    def simulate_provider_cancellation(self, booking_id: str) -> None:
        b = self.state["bookings"][booking_id]
        b["status"] = "cancelled_by_provider"
        self.state["events"].append({"type": "provider_cancelled", "booking_id": booking_id,
                                     "at": self.clock.now().isoformat()})
        self._save()

    def take_slot_externally(self, provider_id: str, slot: str) -> None:
        self.state["taken_slots"].append(f"{provider_id}|{slot}")
        self._save()

    def bookings_for(self, user_id: str | None = None) -> list[dict]:
        uid = user_id or self.user_id
        return [b for b in self.state["bookings"].values() if b["user_id"] == uid]

    def book_calls(self) -> list[dict]:
        return [c for c in self.state["call_log"] if c["tool"] == "book"]


def now_naive(clock: Clock) -> datetime:
    return clock.now().astimezone(IST).replace(tzinfo=None)
