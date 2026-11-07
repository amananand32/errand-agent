"""Explicit workflow state machine for one user session."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class Phase:
    IDLE = "IDLE"
    GATHERING = "GATHERING"            # collecting missing request details
    SEARCHING = "SEARCHING"            # have a complete request, comparing providers
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"            # side effect in flight (book/cancel/reschedule)
    VERIFYING = "VERIFYING"            # ambiguous outcome, consulting source of truth
    DONE = "DONE"
    FAILED = "FAILED"


ALLOWED = {
    Phase.IDLE: {Phase.GATHERING, Phase.SEARCHING, Phase.AWAITING_APPROVAL, Phase.DONE, Phase.FAILED},
    Phase.GATHERING: {Phase.GATHERING, Phase.SEARCHING, Phase.AWAITING_APPROVAL, Phase.IDLE, Phase.FAILED},
    Phase.SEARCHING: {Phase.SEARCHING, Phase.GATHERING, Phase.AWAITING_APPROVAL, Phase.IDLE, Phase.FAILED},
    Phase.AWAITING_APPROVAL: {Phase.EXECUTING, Phase.SEARCHING, Phase.GATHERING, Phase.AWAITING_APPROVAL,
                              Phase.IDLE},
    Phase.EXECUTING: {Phase.DONE, Phase.VERIFYING, Phase.FAILED, Phase.AWAITING_APPROVAL, Phase.SEARCHING},
    Phase.VERIFYING: {Phase.DONE, Phase.FAILED, Phase.EXECUTING, Phase.VERIFYING},
    Phase.DONE: {Phase.IDLE, Phase.GATHERING, Phase.SEARCHING, Phase.AWAITING_APPROVAL},
    Phase.FAILED: {Phase.IDLE, Phase.GATHERING, Phase.SEARCHING, Phase.AWAITING_APPROVAL},
}

REQUIRED_FOR_BOOKING = ("service", "area", "date_from", "date_to", "budget")


class TransitionError(Exception):
    pass


@dataclass
class Request:
    service: str | None = None
    area: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    earliest_time: str | None = None   # "HH:MM"
    latest_time: str | None = None
    budget_max: int | None = None
    budget_set: bool = False           # True once user gave a budget or said "no limit"
    provider_preference: str | None = None
    exclude_providers: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)  # field -> "user" | "memory:<id>" | "event"

    def missing(self) -> list[str]:
        out = []
        if not self.service:
            out.append("service")
        if not self.area:
            out.append("area")
        if not (self.date_from and self.date_to):
            out.append("date window")
        if not self.budget_set and not self.provider_preference:
            # a named provider has a known price, so a budget question would be noise
            out.append("budget")
        return out


@dataclass
class Workflow:
    session_id: str
    user_id: str
    phase: str = Phase.IDLE
    intent: str | None = None          # book | reschedule | cancel
    request: Request = field(default_factory=Request)
    candidates: list[dict] = field(default_factory=list)   # code-verified search results
    target_booking: dict | None = None  # for reschedule/cancel
    pending_proposal_id: str | None = None
    last_outcome: dict | None = None
    turn: int = 0
    transitions: list[dict] = field(default_factory=list)
    pending_memory_prompt: int | None = None   # memory id awaiting user confirmation
    origin: str = "user"               # "user" | "proactive"
    bookings_seen: list[dict] = field(default_factory=list)  # last list_bookings result (code-verified)
    prompted_memories: list[int] = field(default_factory=list)
    inference_candidates: list[int] = field(default_factory=list)  # inferred this task, not yet confirmed

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Workflow":
        d = dict(d)
        d["request"] = Request(**d.get("request", {}))
        return cls(**d)

    def transition(self, to: str, reason: str, tracer: Any = None) -> None:
        if to not in ALLOWED[self.phase]:
            raise TransitionError(f"illegal transition {self.phase} -> {to} ({reason})")
        rec = {"from": self.phase, "to": to, "reason": reason, "turn": self.turn}
        self.transitions = (self.transitions + [rec])[-30:]
        self.phase = to
        if tracer:
            tracer.event("state_transition", **rec)

    def reset_for_new_task(self, tracer: Any = None) -> None:
        if self.phase not in (Phase.IDLE,):
            self.transition(Phase.IDLE, "new task", tracer)
        self.intent = None
        self.request = Request()
        self.candidates = []
        self.target_booking = None
        self.pending_proposal_id = None
        self.origin = "user"
        self.bookings_seen = []
