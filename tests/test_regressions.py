"""Regression tests for the real bugs documented in BUGS.md (+ smaller ones in docs/DEVLOG.md)."""
from errand_agent.context import SYSTEM_PROMPT
from errand_agent.executor import SimulatedCrash
from errand_agent.llm.rule_model import parse_state
from errand_agent.mocks.service import ScenarioConfig
from evals.harness import Env

PLUMBER_REQ = "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500."


def test_bug1_new_request_after_completed_task_is_not_lost():
    """BUG-1: after a finished booking, a new fully-specified request was written to the
    discarded Request object, so search said 'request incomplete'."""
    env = Env("reg_bug1", seed="none", config=ScenarioConfig(book_outcomes=["success"]))
    env.say("Find a carpenter in Jayanagar tomorrow, no budget limit")
    assert env.say("yes").outcome == "booked"
    r = env.say("Also find a cleaner in Jayanagar on Friday, no budget limit")
    req = env.agent.wf.request
    assert req.service == "cleaning" and req.area == "Jayanagar" and req.date_from == "2026-11-13"
    assert r.outcome == "proposed", r.reply


def test_bug2_forget_scrubs_paraphrased_mentions():
    """BUG-2: forgetting 'home.pets' removed the exact value but 'Bruno' survived in an old
    assistant message ("I'll mention Bruno to technicians") and reached the model."""
    env = Env("reg_bug2", history=True)
    env.say("Please forget about my dog")
    n = len(env.packets)
    env.say(PLUMBER_REQ)
    later = "\n".join(p.system + str(p.messages) for p in env.packets[n:])
    stored = " ".join(r["text"] for r in env.agent.store.q("SELECT text FROM messages"))
    assert "Bruno" not in later and "Bruno" not in stored


def test_bug3_crash_leaves_no_live_approval():
    """BUG-3: invalidate() only matched pending/approved proposals; after a crash the
    proposal was 'executing', so its approval stayed live in the DB after restart."""
    env = Env("reg_bug3")
    r1 = env.say(PLUMBER_REQ)
    env.agent.executor.crash_points = {"before_book"}
    try:
        env.say("yes")
    except SimulatedCrash:
        env.crashes.append(env.agent.tracer.run_id)
    env.restart()
    p = env.agent.gate.get(r1.proposal_id)
    live = env.agent.store.q("SELECT * FROM approvals WHERE proposal_id=? AND consumed=0", p.id)
    assert p.status == "invalidated" and not live
    assert env.say("yes").outcome == "nothing_to_confirm"
    assert not env.service.book_calls()


def test_state_tag_parsing_ignores_prose_mentions():
    """DEVLOG: the RuleModel parsed '<state>' from the prompt's prose, not the JSON block."""
    system = SYSTEM_PROMPT + '\n\n<state>{"phase":"IDLE","request":{}}</state>'
    assert "<state>" in SYSTEM_PROMPT and parse_state(system)["phase"] == "IDLE"


def test_context_budget_counts_full_payload():
    """DEVLOG: per-section estimates ignored the message envelope and overshot the cap."""
    env = Env("reg_ctx", history=True, context_budget=2200, config=ScenarioConfig(book_outcomes=["success"]))
    env.say(PLUMBER_REQ)
    env.say("yes")
    assert env.packets and all(p.tokens <= 2200 for p in env.packets)
