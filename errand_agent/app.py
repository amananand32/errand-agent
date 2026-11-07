"""Wiring for interactive use (CLI / web demo)."""
from __future__ import annotations

import os

from .agent import Agent
from .clock import FakeClock, SystemClock
from .limits import Limits
from .llm.base import DEFAULT_MODEL, AnthropicModel, Model, RecordingModel
from .llm.rule_model import RuleModel
from .mocks.service import MockProviderService, ScenarioConfig
from .seed import seed_history, seed_memories
from .store import Store

PRESETS = {
    "default": ScenarioConfig(),                                      # seeded 70/15/15 outcomes
    "timeout": ScenarioConfig(book_outcomes=["timeout_booked"]),      # first book: timeout after success
    "price-drift": ScenarioConfig(hold_price_deltas={"PRV-PLB-06": [200], "PRV-PLB-01": [150]}),
    "slot-taken": ScenarioConfig(book_outcomes=["slot_taken", "success"]),
}


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (KEY=value lines); real environment variables win."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def make_model(name: str, record_to: str | None = None) -> Model:
    """rule | grok | groq | openai-compat | anthropic | <claude model id>.
    `grok:<model-id>` / `groq:<model-id>` pick a specific model."""
    load_dotenv()
    provider, _, model_id = name.partition(":")
    if name == "rule":
        m: Model = RuleModel()
    elif provider in ("grok", "xai", "groq", "openai-compat"):
        from .llm.openai_compat import OpenAICompatModel
        m = OpenAICompatModel("grok" if provider == "xai" else provider, model=model_id or None)
    elif name in ("anthropic", "claude"):
        m = AnthropicModel(os.environ.get("ERRAND_MODEL", DEFAULT_MODEL))
    else:
        m = AnthropicModel(name)
    return RecordingModel(m, record_to) if record_to else m


def build(data_dir: str = "data", model: str = "rule", preset: str = "timeout", reset: bool = False,
          record_to: str | None = None, real_clock: bool = False) -> Agent:
    """FakeClock by default (starts Mon 9 Nov 2026 08:00 IST, simulated latency is instant and
    `advance` works); --real-clock uses wall time and real sleeps."""
    if reset and os.path.isdir(data_dir):
        for f in os.listdir(data_dir):
            p = os.path.join(data_dir, f)
            if os.path.isfile(p):
                os.remove(p)
    os.makedirs(data_dir, exist_ok=True)
    db = os.path.join(data_dir, "agent.db")
    fresh = not os.path.exists(db)
    if fresh:
        st = Store(db)
        seed_memories(st, "u_demo")
        seed_history(st, "u_demo")
        st.db.close()
    clock = SystemClock() if real_clock else FakeClock()
    svc = MockProviderService(PRESETS[preset], clock, state_path=os.path.join(data_dir, "provider_service.json"))
    return Agent(service=svc, model=make_model(model, record_to), db_path=db,
                 trace_dir=os.path.join(data_dir, "traces"), clock=clock,
                 limits=Limits(max_wall_clock_s=90))
