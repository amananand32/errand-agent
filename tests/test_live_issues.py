"""Regressions for problems seen in the first live Groq (openai/gpt-oss-120b) run."""
import json
import time

from errand_agent.context import SYSTEM_PROMPT, ContextBuilder
from errand_agent.llm.rule_model import RuleModel
from errand_agent.mocks.service import ScenarioConfig
from evals.harness import Env

REQ = "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500."


def test_resending_same_request_keeps_pending_proposal():
    """Live: re-sending the request replaced a ₹650 card with a ₹1,100 one from another provider."""
    env = Env("live_dup_request")
    r1 = env.say(REQ)
    r2 = env.say(REQ)
    assert r2.proposal_id == r1.proposal_id
    assert env.agent.gate.get(r1.proposal_id).status == "pending"
    assert len(env.tool_calls("hold_slot")) == 1 and not env.ev("approval_invalidated")


def test_search_uses_recorded_request_even_if_model_dates_differ():
    """Live: model sent date_to=2026-11-13 for 'this week'; app errored and the model then
    proposed from stale results. Now the app searches with the recorded request and says so."""
    env = Env("live_search_override")
    env.say(REQ)
    env.agent._flags = set()
    out, err = env.agent._dispatch("search_providers", {"service": "plumber", "area": "Indiranagar",
                                                        "date_from": "2026-11-09", "date_to": "2026-11-13"}, REQ)
    assert not err and out["ranked_options"] and out["searched_with"]["date_to"] == "2026-11-15"
    assert "ignored" in out["note"] and env.ev("search_args_overridden")


def test_today_text_gives_exact_week_ranges():
    env = Env("live_dates")   # FakeClock: Mon 9 Nov 2026
    t = env.agent._today_text()
    assert "this week=2026-11-09..2026-11-15" in t and "tomorrow=2026-11-10" in t
    assert "this weekend=2026-11-14..2026-11-15" in t and "Thu=2026-11-12" in t


def test_prompt_forbids_promising_untooled_actions():
    """Live: 'I'll let the technician know about Bruno' - there is no tool for that."""
    assert "Never promise or imply any action you have no tool for" in SYSTEM_PROMPT
    assert "Do NOT\n  restate" in SYSTEM_PROMPT


class SlowRuleModel(RuleModel):
    def complete(self, *a, **k):
        time.sleep(0.05)
        return super().complete(*a, **k)


def test_model_latency_is_real_and_advances_simulated_clock():
    """Live: every model call showed 0ms because latency was read from the FakeClock."""
    env = Env("live_latency")
    env.agent.model = SlowRuleModel()
    t0 = env.clock.monotonic()
    env.say(REQ)
    calls = env.ev("model_call")
    assert calls and all(c["latency_ms"] >= 45 for c in calls)
    assert env.clock.monotonic() - t0 >= 0.045 * len(calls)


def test_compression_keeps_search_results_usable():
    big = json.dumps({"ranked_options": [{"rank": i, "provider_id": f"P{i}", "price": 900,
                                          "earliest_fitting_slot": "2026-11-10T09:00",
                                          "other_fitting_slots": ["x"] * 20} for i in range(5)],
                      "note": "n" * 500})
    small = json.loads(ContextBuilder._shrink(big))
    assert small["_compressed"] and len(small["ranked_options"]) == 3
    assert small["ranked_options"][0]["earliest_fitting_slot"] == "2026-11-10T09:00"
    assert "other_fitting_slots" not in small["ranked_options"][0]


def test_full_booking_still_works_after_fixes():
    env = Env("live_e2e", config=ScenarioConfig(book_outcomes=["timeout_booked"]))
    env.say(REQ)
    r = env.say("yes")
    assert r.outcome == "booked" and len(env.new_bookings()) == 1
