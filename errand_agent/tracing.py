"""Per-run JSONL tracing: state transitions, model calls, tool calls (sanitized),
context manifests, approvals, latency, tokens and estimated cost."""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from .clock import Clock

# USD per million tokens (input, output). Source: Claude API pricing page, Nov 2026.
PRICING = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.00, 20.00),
}
# Offline/scripted runs report cost *as if* the default model had been used.
COST_REFERENCE_MODEL = "claude-sonnet-5-5"

_PHONE = re.compile(r"(?<!\d)(\+?91[\s-]?)?[6-9]\d{9}(?!\d)")


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = PRICING.get(model, PRICING[COST_REFERENCE_MODEL])
    return round(tokens_in / 1e6 * pin + tokens_out / 1e6 * pout, 6)


def sanitize_for_trace(value: Any, max_str: int = 240) -> Any:
    """Redact phone numbers and truncate long free text so traces stay small and safe."""
    if isinstance(value, str):
        v = _PHONE.sub("[phone]", value)
        return v if len(v) <= max_str else v[:max_str] + f"...[+{len(v) - max_str} chars]"
    if isinstance(value, dict):
        return {k: sanitize_for_trace(v, max_str) for k, v in value.items()}
    if isinstance(value, list):
        items = [sanitize_for_trace(v, max_str) for v in value[:12]]
        if len(value) > 12:
            items.append(f"...[+{len(value) - 12} items]")
        return items
    return value


class Tracer:
    def __init__(self, trace_dir: str, clock: Clock):
        self.trace_dir = trace_dir
        self.clock = clock
        os.makedirs(trace_dir, exist_ok=True)
        self.run_id: str | None = None
        self.events: list[dict] = []
        self._t0 = 0.0
        self._seq = 0
        self.totals: dict[str, Any] = {}
        self.all_runs: list[str] = []
        self.history: list[dict] = []  # every event across runs (used by evals)

    @property
    def path(self) -> str:
        return os.path.join(self.trace_dir, f"{self.run_id}.jsonl")

    def start_run(self, session_id: str, kind: str, **data: Any) -> str:
        self.run_id = f"run-{self.clock.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.all_runs.append(self.run_id)
        self.events = []
        self._t0 = self.clock.monotonic()
        self._seq = 0
        self.totals = {"model_calls": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0,
                       "cost_usd": 0.0}
        self.event("run_start", session_id=session_id, kind=kind, **data)
        return self.run_id

    def event(self, type_: str, **data: Any) -> dict:
        if self.run_id is None:
            self.start_run("_none", "implicit")
        self._seq += 1
        ev = {"seq": self._seq, "run_id": self.run_id, "type": type_,
              "ts": self.clock.now().isoformat(),
              "t_ms": round((self.clock.monotonic() - self._t0) * 1000, 1), **data}
        self.events.append(ev)
        self.history.append(ev)
        with open(self.path, "a") as f:
            f.write(json.dumps(ev, default=str) + "\n")
        return ev

    def model_call(self, model: str, latency_ms: float, tokens_in: int, tokens_out: int,
                   stop_reason: str, tool_uses: list[str], estimated: bool) -> None:
        cost = estimate_cost(model, tokens_in, tokens_out)
        self.totals["model_calls"] += 1
        self.totals["tokens_in"] += tokens_in
        self.totals["tokens_out"] += tokens_out
        self.totals["cost_usd"] = round(self.totals["cost_usd"] + cost, 6)
        self.event("model_call", model=model, latency_ms=round(latency_ms, 1), tokens_in=tokens_in,
                   tokens_out=tokens_out, est_cost_usd=cost, stop_reason=stop_reason,
                   tool_uses=tool_uses, token_counts_estimated=estimated)

    def tool_call(self, name: str, args: dict, latency_ms: float, result: Any = None,
                  error: str | None = None, attempt: int = 1, caller: str = "app") -> None:
        self.totals["tool_calls"] += 1
        self.event("tool_call", tool=name, caller=caller, attempt=attempt,
                   args=sanitize_for_trace(args), latency_ms=round(latency_ms, 1),
                   result=sanitize_for_trace(result) if error is None else None, error=error)

    def end_run(self, status: str, reply: str, **data: Any) -> None:
        self.event("run_end", status=status, reply_preview=sanitize_for_trace(reply, 160),
                   totals=dict(self.totals), **data)

    def of_type(self, type_: str) -> list[dict]:
        return [e for e in self.events if e["type"] == type_]
