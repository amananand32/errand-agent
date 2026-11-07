"""Interactive chat in the terminal.

  python -m errand_agent.cli                      # offline scripted model, 'timeout' preset
  python -m errand_agent.cli --model anthropic    # real Claude (needs ANTHROPIC_API_KEY)

Commands: /tick  /cancel-by-provider <BK-id>  /advance <minutes>  /bookings  /trace  /outbox  /quit
"""
from __future__ import annotations

import argparse
import json

from .app import PRESETS, build
from .proactive import ProactiveMonitor


def short(e: dict) -> str:
    t = e["type"]
    if t == "tool_call":
        return f"  tool {e['tool']}({json.dumps(e['args'])[:80]}) {'ERR ' + e['error'] if e['error'] else 'ok'} {e['latency_ms']}ms"
    if t == "model_call":
        return f"  model {e['model']} in={e['tokens_in']} out={e['tokens_out']} ${e['est_cost_usd']} -> {e['tool_uses']}"
    if t == "state_transition":
        return f"  state {e['from']} -> {e['to']} ({e['reason']})"
    if t == "context":
        return f"  context {e['tokens_total']}/{e['budget_tokens']} tok, memories={e['memories_included']}"
    keep = {k: v for k, v in e.items() if k not in ("seq", "run_id", "ts", "t_ms", "type", "tb")}
    return f"  {t} {json.dumps(keep, default=str)[:140]}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="rule", help="rule | anthropic | <claude model id>")
    ap.add_argument("--preset", default="timeout", choices=sorted(PRESETS))
    ap.add_argument("--data", default="data")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--record", help="record model calls to a JSONL cassette")
    ap.add_argument("--real-clock", action="store_true")
    ap.add_argument("--show-trace", action="store_true", help="print a compact trace after each turn")
    a = ap.parse_args()
    agent = build(a.data, a.model, a.preset, a.reset, a.record, a.real_clock)
    print(f"Errand agent ({a.model}, preset={a.preset}). Today is {agent.clock.now():%a %d %b %Y %H:%M}. "
          "Type /quit to exit.")
    for n in agent.notices:
        print("agent>", n)
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/tick":
            for n in ProactiveMonitor(agent).tick() or ["(no changes)"]:
                print("agent>", n)
            continue
        if text.startswith("/cancel-by-provider"):
            agent.tools.svc.simulate_provider_cancellation(text.split()[1])
            print("(simulated provider cancellation; run /tick)")
            continue
        if text.startswith("/advance"):
            agent.clock.advance(minutes=float(text.split()[1]))
            print(f"(clock now {agent.clock.now():%a %H:%M})")
            continue
        if text == "/bookings":
            for b in agent.tools.svc.bookings_for():
                print(f"  {b['booking_id']} {b['provider_name']:<26} {b['slot']} {b['status']}")
            continue
        if text == "/outbox":
            for r in agent.store.outbox(agent.user_id):
                print(" ", r["ts"][:16], r["kind"], r["text"][:100])
            continue
        if text == "/trace":
            print(f"trace file: {agent.tracer.path}")
            for e in agent.tracer.events:
                print(short(e))
            continue
        r = agent.handle(text)
        print("agent>", r.reply)
        if a.show_trace:
            print(f"--- trace {r.run_id} ({agent.tracer.path})")
            for e in agent.tracer.events:
                print(short(e))


if __name__ == "__main__":
    main()
