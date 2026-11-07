"""Hard execution budgets for one user turn (or one proactive run)."""
from __future__ import annotations

from dataclasses import dataclass

from .clock import Clock


@dataclass
class Limits:
    max_model_turns: int = 8
    max_tool_calls: int = 14
    max_wall_clock_s: float = 60.0
    max_retries: int = 3           # per tool call, transient errors only
    backoff_base_s: float = 0.5
    max_status_checks: int = 4     # source-of-truth polls after an ambiguous booking
    max_book_attempts: int = 3


class LimitExceeded(Exception):
    def __init__(self, which: str, detail: str):
        super().__init__(f"{which}: {detail}")
        self.which = which


class Budget:
    def __init__(self, limits: Limits, clock: Clock):
        self.limits = limits
        self.clock = clock
        self.t0 = clock.monotonic()
        self.model_turns = 0
        self.tool_calls = 0

    def elapsed(self) -> float:
        return self.clock.monotonic() - self.t0

    def check_time(self) -> None:
        if self.elapsed() > self.limits.max_wall_clock_s:
            raise LimitExceeded("wall_clock", f"{self.elapsed():.1f}s > {self.limits.max_wall_clock_s}s")

    def charge_model_turn(self) -> None:
        self.check_time()
        if self.model_turns >= self.limits.max_model_turns:
            raise LimitExceeded("model_turns", f"limit {self.limits.max_model_turns} reached")
        self.model_turns += 1

    def charge_tool_call(self) -> None:
        self.check_time()
        if self.tool_calls >= self.limits.max_tool_calls:
            raise LimitExceeded("tool_calls", f"limit {self.limits.max_tool_calls} reached")
        self.tool_calls += 1
