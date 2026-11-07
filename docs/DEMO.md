# Demo script (3–5 min, chat and trace side by side)

`docs/demo/errand_demo.mp4` is a real screen recording of this UI driven by `scripts/record_demo.py`
(headless Chrome, offline model). To record the Loom with a live model (Groq is free; put
`GROQ_API_KEY=...` in `.env`, or use `XAI_API_KEY` with `--model grok`):

```bash
python -m errand_agent.check_llm --model groq
python -m errand_agent.web --model groq --preset timeout --reset --record data/cassette.jsonl
# open http://localhost:8765, start the screen recorder on the browser window
```

The page already shows chat (left) and the live trace for each run (right).

| Time | Do | Point out in the trace |
|---|---|---|
| 0:00 | One sentence: what the agent does; "the model can only propose, the app executes." | — |
| 0:20 | Type *Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.* | `context` (tokens / budget, which memories were sent, history dropped), `memory_defaults_applied` (area), model calls with tokens and cost, `search_providers` |
| 1:00 | Read the reply: injection warning + comparison + app-rendered card | `injection_flagged $.reviews[1].text`; `hold_slot` → `approval_requested` with provider/slot/quoted price |
| 1:30 | Type *yes* (**live failure: timeout after success**) | `ledger in_flight` before `book`, `book ERROR timeout 8600ms`, `EXECUTING → VERIFYING`, `get_booking_status → confirmed`, `booking_verified verified_by=get_booking_status` |
| 2:15 | Type *yes* again | `duplicate_confirmation_ignored`, zero tool calls |
| 2:30 | Click **Provider cancels…** → `BK-2002`, then **Run monitor** | `provider_cancellation_detected`, `decision notify urgent`, replacement search excluding the cancelled provider, new card |
| 3:10 | Type *yes* | fresh approval → booking |
| 3:30 | *What do you remember about me?*, then *Please forget about my dog* | model only receives a count; `memory_forgotten`; next `context` has no forgotten values |
| 4:10 | Close: `pytest` output + `reports/eval_table.md` | 34/34, global invariants |

Optional extra failure: `--preset price-drift` and wait 11 minutes before saying *yes* (or use the
**+11 min** button with the default fake clock) to show approval invalidation on a price change.
