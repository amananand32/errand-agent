"""Model interface + real Anthropic adapter + record/replay wrappers."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field

DEFAULT_MODEL = os.environ.get("ERRAND_MODEL", "claude-sonnet-5-5")


@dataclass
class ModelResponse:
    content: list[dict]          # Anthropic-style blocks: {"type":"text"} / {"type":"tool_use"}
    tokens_in: int
    tokens_out: int
    stop_reason: str
    model: str
    estimated: bool = False      # True when token counts are estimates (offline models)
    meta: dict = field(default_factory=dict)

    @property
    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(b["text"] for b in self.content if b.get("type") == "text").strip()


class Model:
    name = "abstract"

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse:
        raise NotImplementedError


class AnthropicModel(Model):
    """Real LLM. Requires `pip install anthropic` and ANTHROPIC_API_KEY."""

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = 1024, temperature: float = 0.0):
        import anthropic  # lazy: evals never need it

        self.client = anthropic.Anthropic()
        self.name = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def complete(self, system, messages, tools):
        r = self.client.messages.create(model=self.name, max_tokens=self.max_tokens, system=system,
                                        messages=messages, tools=tools, temperature=self.temperature)
        blocks = []
        for b in r.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        return ModelResponse(blocks, r.usage.input_tokens, r.usage.output_tokens, r.stop_reason, self.name)


def request_hash(system: str, messages: list[dict], tools: list[dict]) -> str:
    payload = json.dumps({"s": system, "m": messages, "t": [t["name"] for t in tools]}, sort_keys=True,
                         default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


class RecordingModel(Model):
    """Wraps any model and appends (request_hash -> response) to a JSONL cassette."""

    def __init__(self, inner: Model, path: str):
        self.inner, self.path, self.name = inner, path, inner.name

    def complete(self, system, messages, tools):
        r = self.inner.complete(system, messages, tools)
        with open(self.path, "a") as f:
            f.write(json.dumps({"hash": request_hash(system, messages, tools), "response": r.__dict__}) + "\n")
        return r


class ReplayModel(Model):
    """Replays a cassette. Strict mode fails on any request not seen at record time,
    which catches prompt/context drift; sequential mode replays in order."""

    def __init__(self, path: str, strict: bool = True):
        self.name = "replay"
        self.strict = strict
        with open(path) as f:
            self.rows = [json.loads(line) for line in f if line.strip()]
        self.by_hash = {r["hash"]: r["response"] for r in self.rows}
        self.i = 0

    def complete(self, system, messages, tools):
        if self.strict:
            h = request_hash(system, messages, tools)
            if h not in self.by_hash:
                raise KeyError(f"replay miss for request {h}: context drifted from the recording")
            d = self.by_hash[h]
        else:
            d = self.rows[self.i]["response"]
            self.i += 1
        return ModelResponse(**d)
