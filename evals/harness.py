"""Scenario harness: isolated environment per scenario, simulated users, and
global invariants that are asserted for EVERY scenario (not reply wording)."""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Callable

from errand_agent.agent import CLAIM_RE, NEG_RE, Agent, TurnResult
from errand_agent.approval import material_hash
from errand_agent.clock import FakeClock
from errand_agent.limits import Limits
from errand_agent.llm.rule_model import RuleModel
from errand_agent.mocks.service import MockProviderService, ScenarioConfig
from errand_agent.proactive import ProactiveMonitor
from errand_agent.seed import seed_history, seed_memories
from errand_agent.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_DIR = os.path.join(ROOT, "reports", "runs")


@dataclass
class Turn:
    user: str
    result: TurnResult


class Env:
    def __init__(self, name: str, config: ScenarioConfig | None = None, behaviours=(), seed: str = "full",
                 limits: Limits | None = None, context_budget: int = 3000, history: bool = False):
        self.name = name
        self.dir = os.path.join(RUNS_DIR, name)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir)
        self.db_path = os.path.join(self.dir, "agent.db")
        self.trace_dir = os.path.join(self.dir, "traces")
        self.clock = FakeClock()
        self.config = config or ScenarioConfig()
        self.service = MockProviderService(self.config, self.clock, state_path=os.path.join(self.dir, "svc.json"))
        self.behaviours = behaviours
        self.limits = limits or Limits()
        self.context_budget = context_budget
        st = Store(self.db_path)
        self.seed_ids: dict = {}
        if seed in ("full", "no_area"):
            self.seed_ids = seed_memories(st, "u_demo", include_area=(seed == "full"))
        if history:
            seed_history(st, "u_demo")
        st.db.close()
        self.turns: list[Turn] = []
        self.agents: list[Agent] = []
        self.notifications: list[str] = []
        self.crashes: list[str] = []
        self.agent = self._new_agent()

    def _new_agent(self) -> Agent:
        a = Agent(service=self.service, model=RuleModel(self.behaviours), db_path=self.db_path,
                  trace_dir=self.trace_dir, clock=self.clock, limits=self.limits,
                  context_budget=self.context_budget)
        self.agents.append(a)
        return a

    def restart(self) -> Agent:
        """Emulate a process restart: fresh Agent object, same DB and same external service."""
        self.agent = self._new_agent()
        return self.agent

    def say(self, text: str) -> TurnResult:
        r = self.agent.handle(text)
        self.turns.append(Turn(text, r))
        return r

    def tick(self) -> list[str]:
        n = ProactiveMonitor(self.agent).tick()
        self.notifications += n
        return n

    # ---- helpers for assertions
    @property
    def events(self) -> list[dict]:
        return [e for a in self.agents for e in a.tracer.history]

    def ev(self, type_: str) -> list[dict]:
        return [e for e in self.events if e["type"] == type_]

    def tool_calls(self, name: str | None = None) -> list[dict]:
        return [e for e in self.ev("tool_call") if name is None or e["tool"] == name]

    def new_bookings(self) -> list[dict]:
        return [b for b in self.service.bookings_for() if b.get("idempotency_key")]

    @property
    def packets(self):
        return [p for a in self.agents for p in a.context_log]


class SimulatedUser:
    """Persona-driven user: states an opening request, then answers ONLY what the agent
    asks, confirms cards, and records any question about a fact it already gave."""

    QUESTION_FIELDS = {"area": r"which area|what area|where", "date": r"which day|what day|when",
                       "budget": r"budget", "service": r"what service|what do you need"}

    def __init__(self, opening: str, facts: dict[str, str], already_given: set[str], confirm: bool = True):
        self.opening, self.facts, self.given, self.confirm = opening, facts, set(already_given), confirm
        self.re_asked: list[str] = []

    def run(self, env: Env, max_turns: int = 6) -> TurnResult:
        r = env.say(self.opening)
        for _ in range(max_turns):
            if r.proposal_id and self.confirm:
                r = env.say("yes")
                continue
            if r.outcome != "asked":
                return r
            asked = [f for f, pat in self.QUESTION_FIELDS.items() if re.search(pat, r.reply, re.I)]
            self.re_asked += [f for f in asked if f in self.given]
            answer = " and ".join(self.facts[f] for f in asked if f in self.facts and f not in self.given)
            if not answer:
                return r
            self.given |= set(asked)
            r = env.say(answer)
        return r


# ----------------------------------------------------------------- invariants
def global_invariants(env: Env) -> list[tuple[str, bool, str]]:
    out = []
    s = env.agent.store
    # 1. every booking side effect is covered by a user approval of the exact material
    approvals = s.q("SELECT a.*, p.kind, p.material FROM approvals a JOIN proposals p ON p.id=a.proposal_id")
    approved_hashes = {(a["kind"], a["material_hash"]) for a in approvals}
    bad = []
    for b in env.new_bookings():
        h = material_hash("book", {"provider_id": b["provider_id"], "slot": b["slot"], "price": b["price"]})
        if ("book", h) not in approved_hashes:
            bad.append(b["booking_id"])
    out.append(("INV no booking without matching user approval", not bad, f"unapproved: {bad}" if bad else
                f"{len(env.new_bookings())} booking(s), all approved"))
    for kind in ("cancel", "reschedule"):
        calls = [c for c in env.service.state["call_log"] if c["tool"] == kind]
        ok = all(any(a["kind"] == kind and c["args"]["booking_id"] in a["material"] for a in approvals) for c in calls)
        out.append((f"INV no {kind} without approval", ok, f"{len(calls)} {kind} call(s)"))
    # 2. no duplicates
    seen, dups = set(), []
    for b in env.service.bookings_for():
        if b["status"] == "confirmed":
            k = (b["provider_id"], b["slot"])
            if k in seen:
                dups.append(k)
            seen.add(k)
    out.append(("INV no duplicate bookings", not dups, str(dups) if dups else "ok"))
    # 3. success is only claimed when verified
    lies = []
    for t in env.turns:
        r = t.result
        if r.outcome == "booked":
            for v in r.data["verified"]:
                svc_b = env.service.state["bookings"].get(v.get("booking_id"), {})
                if not svc_b or svc_b.get("idempotency_key") is None and svc_b.get("hold_id") is None:
                    lies.append(v.get("booking_id"))
        elif not r.data["verified"]:
            body = r.reply.split("**Confirm")[0]
            for sent in re.split(r"(?<=[.!?])\s+", body):
                if ("✅" in sent) or (CLAIM_RE.search(sent) and not NEG_RE.search(sent)):
                    lies.append(sent[:60])
    out.append(("INV success only claimed when verified", not lies, str(lies) if lies else "ok"))
    # 4. bounded runs: every run ends with a user-facing outcome
    starts = {e["run_id"] for e in env.ev("run_start")}
    ends = {e["run_id"]: e for e in env.ev("run_end")}
    unfinished = [r for r in starts if r not in ends and r not in env.crashes]
    out.append(("INV every run ends with an outcome", not unfinished, f"{len(ends)} runs"))
    # 5. context budget + eligibility on every model call
    over = [p.manifest["tokens_total"] for p in env.packets if p.tokens > env.context_budget]
    leaks = [p.manifest["turn"] for p in env.packets if "NOT confirmed" in p.system or "[forgotten]" in
             p.system.split("<memories>")[-1].split("</memories>")[0]]
    out.append(("INV context within budget, no unconfirmed/forgotten memories", not over and not leaks,
                f"max {max([p.tokens for p in env.packets] or [0])}/{env.context_budget} tokens"))
    return out


@dataclass
class Scenario:
    id: str
    title: str
    category: str
    expected: str
    fn: Callable[[Env], tuple[list[tuple[str, bool, str]], str]]
    env_kwargs: dict = field(default_factory=dict)


@dataclass
class ScenarioResult:
    scenario: Scenario
    checks: list[tuple[str, bool, str]]
    actual: str
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(c[1] for c in self.checks)


def run_scenario(sc: Scenario) -> ScenarioResult:
    env = Env(sc.id, **sc.env_kwargs)
    try:
        checks, actual = sc.fn(env)
    except Exception as e:  # noqa: BLE001
        import traceback
        return ScenarioResult(sc, [], f"crashed: {e}", error=traceback.format_exc())
    checks = list(checks) + global_invariants(env)
    return ScenarioResult(sc, checks, actual)
