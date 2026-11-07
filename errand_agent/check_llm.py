"""Check that your LLM key works with tool calling before running the agent.

    python -m errand_agent.check_llm --model groq
    python -m errand_agent.check_llm --model grok
"""
from __future__ import annotations

import argparse
import sys
import time

from .app import make_model


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="groq", help="groq | grok | groq:<id> | grok:<id> | openai-compat")
    ap.add_argument("--list", action="store_true", help="list the model ids your key can use")
    a = ap.parse_args()
    try:
        m = make_model(a.model)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Config problem: {e}")
    if a.list or hasattr(m, "list_models"):
        try:
            ids = m.list_models()
            print(f"models available to this key ({len(ids)}):", ", ".join(ids))
        except Exception as e:  # noqa: BLE001
            print(f"could not list models: {e}")
        if a.list:
            return
    tool = {"name": "update_request", "description": "Record request details.",
            "input_schema": {"type": "object", "properties": {"service": {"type": "string"},
                                                              "evidence": {"type": "string"}},
                             "required": ["evidence"]}}
    t0 = time.time()
    try:
        r = m.complete("Call update_request with the service the user needs and a verbatim evidence quote.",
                       [{"role": "user", "content": "I need a plumber"}], [tool])
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Call failed: {e}")
    print(f"model used={m.name}  latency={time.time() - t0:.1f}s  tokens in/out={r.tokens_in}/{r.tokens_out}")
    print("tool calls:", [(t["name"], t["input"]) for t in r.tool_uses] or "none")
    print("text:", r.text[:200] or "-")
    print("OK - tool calling works" if r.tool_uses else "WARNING: model answered without calling the tool")


if __name__ == "__main__":
    main()
