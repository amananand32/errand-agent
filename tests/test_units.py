"""Component tests + 'tests of the tests' (the invariants must catch a broken agent)."""
import json
import os

import pytest

from errand_agent.approval import ApprovalError, ApprovalGate, classify_reply, material_hash
from errand_agent.llm.base import RecordingModel, ReplayModel
from errand_agent.llm.rule_model import RuleModel
from errand_agent.mocks.service import MockProviderService, ScenarioConfig, ToolTimeout, TransientError
from errand_agent.sanitize import sanitize_tool_result
from errand_agent.store import Store
from evals.harness import RUNS_DIR, Env, global_invariants

PLUMBER_REQ = "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500."


@pytest.mark.parametrize("text,ref,expected", [
    ("yes", None, "approve"), ("Yes please, go ahead", None, "approve"), ("confirm", None, "approve"),
    ("ok book it", None, "approve"), ("yes 21F1", "21F1", "approve"), ("yes ffff", "21F1", "other"),
    ("yes but make it 11am", None, "other"), ("no", None, "reject"), ("wait, what? no", None, "reject"),
    ("no, find a cheaper one in koramangala", None, "other"), ("is he verified?", None, "other"),
    ("", None, "other"),
])
def test_classify_reply(text, ref, expected):
    assert classify_reply(text, ref) == expected


def test_material_hash_sensitive_to_price_slot_provider():
    base = {"provider_id": "P1", "slot": "2026-11-10T09:00", "price": 900}
    h = material_hash("book", base)
    assert h == material_hash("book", dict(base, provider_name="ignored display field"))
    for k, v in (("price", 950), ("slot", "2026-11-10T10:00"), ("provider_id", "P2")):
        assert material_hash("book", dict(base, **{k: v})) != h


def test_authorize_requires_user_approval_and_same_material(tmp_path):
    g = ApprovalGate(Store(str(tmp_path / "a.db")))
    m = {"provider_id": "P1", "provider_name": "X", "slot": "2026-11-10T09:00", "price": 900}
    p = g.propose("s", "book", m, {}, turn=1, ts="t")
    with pytest.raises(ApprovalError):
        g.authorize(p.id, "book", m)                       # no approval yet
    with pytest.raises(ApprovalError):
        g.approve(p, "yes", turn=3, ts="t")                # not the turn right after the card
    g.approve(p, "yes", turn=2, ts="t")
    with pytest.raises(ApprovalError):
        g.authorize(p.id, "book", dict(m, price=1000))     # material changed
    p2 = g.propose("s", "book", m, {}, turn=4, ts="t")
    g.approve(p2, "yes", turn=5, ts="t")
    assert g.authorize(p2.id, "book", m)
    g.consume(p2.id)
    with pytest.raises(ApprovalError):
        g.authorize(p2.id, "book", m)                      # approvals are single-use


def test_mock_book_distribution_and_idempotency():
    svc = MockProviderService(ScenarioConfig(seed=11))
    outcomes = {"success": 0, "slot_taken": 0, "timeout": 0}
    for i in range(400):
        pid = "PRV-PLB-01"
        svc.state["holds"][f"H{i}"] = {"hold_id": f"H{i}", "provider_id": pid, "slot": f"2026-12-01T{i % 24:02d}:00",
                                       "quoted_price": 900, "expires_at": "2099-01-01T00:00:00+05:30",
                                       "consumed": False}
        try:
            r = svc.book(f"H{i}", f"k{i}")
            outcomes["success" if r["status"] == "confirmed" else r["status"]] += 1
        except ToolTimeout:
            outcomes["timeout"] += 1
    assert 0.62 < outcomes["success"] / 400 < 0.86 and outcomes["timeout"] > 20 and outcomes["slot_taken"] > 20
    k = next(iter(svc.state["by_key"]))
    assert svc.book("anything", k)["idempotent_replay"] is True


def test_mock_cancel_first_call_503_then_ok():
    svc = MockProviderService()
    with pytest.raises(TransientError):
        svc.cancel("BK-1003", "key-1")
    assert svc.cancel("BK-1003", "key-1")["status"] == "cancelled"
    assert svc.cancel("BK-1003", "key-1")["status"] == "cancelled"   # idempotent


def test_search_returns_3_to_8_and_reviews_have_injection():
    svc = MockProviderService()
    for s in ("plumbing", "electrical", "ac_service", "carpentry", "cleaning", "pest_control"):
        n = len(svc.search_providers(s, "Whitefield", ["2026-11-09", "2026-11-11"])["providers"])
        assert 3 <= n <= 8
    clean, flags = sanitize_tool_result(svc.get_provider("PRV-PLB-03"))
    assert flags and "UNTRUSTED" in json.dumps(clean)


def test_invariants_catch_a_broken_approval_gate(monkeypatch):
    """Mutation test: if the agent booked without asking, the global invariant must fail."""
    env = Env("mutant_no_approval", config=ScenarioConfig(book_outcomes=["success"]))
    env.say(PLUMBER_REQ)
    p = env.agent.gate.pending("main")
    # bypass the user: approve in code and execute (what a buggy agent might do)
    env.agent.store.x("INSERT INTO approvals(proposal_id,material_hash,user_message,turn,created_at) "
                      "VALUES(?,?,?,?,?)", p.id, "0" * 64, "(auto)", 1, "t")
    env.agent.store.x("UPDATE proposals SET status='approved' WHERE id=?", p.id)
    env.agent.store.x("UPDATE proposals SET material_hash=? WHERE id=?", "0" * 64, p.id)
    hold = p.context["hold_id"]
    env.service.book(hold, "mutant-key")
    checks = dict((n, ok) for n, ok, _ in global_invariants(env))
    assert checks["INV no booking without matching user approval"] is False


def test_invariants_catch_a_false_success_claim():
    env = Env("mutant_claim")
    env.say(PLUMBER_REQ)
    env.turns[-1].result.reply = "Your booking is confirmed!"
    checks = dict((n, ok) for n, ok, _ in global_invariants(env))
    assert checks["INV success only claimed when verified"] is False


def test_record_then_strict_replay_reproduces_run(tmp_path):
    """Offline evals can run against a *recorded* model: record once, replay strictly."""
    cassette = str(tmp_path / "cassette.jsonl")
    env = Env("record_run", config=ScenarioConfig(book_outcomes=["success"]))
    env.agent.model = RecordingModel(RuleModel(), cassette)
    env.say(PLUMBER_REQ)
    env.say("yes")
    rec_replies = [t.result.reply for t in env.turns]
    env2 = Env("replay_run", config=ScenarioConfig(book_outcomes=["success"]))
    env2.agent.model = ReplayModel(cassette, strict=True)
    env2.say(PLUMBER_REQ)
    env2.say("yes")
    assert [t.result.reply for t in env2.turns] == rec_replies
    assert env2.turns[-1].result.outcome == "booked"


def test_anthropic_adapter_shapes_blocks(monkeypatch):
    """The real-LLM adapter maps SDK objects to our block format (no network)."""
    import sys
    import types

    class Blk:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Msgs:
        def create(self, **kw):
            assert kw["tools"] and kw["system"] and kw["messages"][-1]["role"] == "user"
            return Blk(content=[Blk(type="text", text="Searching"),
                                Blk(type="tool_use", id="t1", name="search_providers", input={"service": "plumbing"})],
                       usage=Blk(input_tokens=1200, output_tokens=40), stop_reason="tool_use")

    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda: Blk(messages=Msgs())
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    from errand_agent.llm.base import AnthropicModel
    r = AnthropicModel("claude-sonnet-5-5").complete("sys", [{"role": "user", "content": "hi"}], [{"name": "x"}])
    assert r.tool_uses[0]["name"] == "search_providers" and r.tokens_in == 1200 and not r.estimated


def test_trace_has_required_fields():
    env = Env("trace_fields", config=ScenarioConfig(book_outcomes=["timeout_booked"]))
    env.say(PLUMBER_REQ)
    env.say("yes")
    types_ = {e["type"] for e in env.events}
    assert {"state_transition", "model_call", "tool_call", "context", "approval_granted", "run_end"} <= types_
    mc = env.ev("model_call")[0]
    assert {"latency_ms", "tokens_in", "tokens_out", "est_cost_usd"} <= set(mc)
    tc = env.ev("tool_call")[0]
    assert {"tool", "args", "latency_ms", "result"} <= set(tc)
    assert os.path.exists(env.agent.tracer.path)
