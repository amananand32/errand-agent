# Transcript: AI coding assistant used while building

**Assistant:** Claude (Opus 5.5) in the claude.ai agentic workspace (file tools + Linux shell).
**Session:** https://claude.ai/code/session_01WjtqZxpzvmaXQNMkd2AS4H (the full raw chat, including
every tool call and output, can be exported/shared from claude.ai). Commits carry the same session link.

This file is a faithful condensed transcript: the human prompt verbatim, then the assistant's turns
in order (decisions, commands run, what came back). Nothing was written by hand outside this session.

---

### Human
> *(attached: Orange_Engineering_Assignment.pdf — the "Errand Agent" brief)*
> complete this Assignment end to end as instructed

### Assistant — plan
Read the brief. Checked the toolchain (`python3 3.13`, `pytest 9`, `git`, `ffmpeg`, `reportlab`; no
`anthropic` SDK, no API key, network limited to package registries). Looked up current Claude API
pricing (Sonnet 5.5 $2/$10 per MTok) for cost estimates in traces.

Key decisions taken up front:
1. **Model proposes, app executes.** Do not expose `book/cancel/reschedule/hold_slot` to the model;
   expose `propose_*` instead, so approval is enforced by code and injection cannot cause side effects.
2. **Approval = DB row bound to a material hash** (provider, slot, quoted price / booking id), created
   only by a deterministic parse of the user's next message; `authorize()` re-checks at execution.
3. **Intent ledger** with one idempotency key per target, persisted *before* `book()`; ambiguous
   outcomes resolved via `get_booking_status`; reconcile on startup and on every turn.
4. **Offline evals with a `RuleModel`** that consumes the same context a real model would, so context
   bugs surface as behaviour bugs; plus Recording/Replay wrappers and misbehaving modes.
5. **Memory provenance** (explicit/inferred/confirmed/forgotten); unconfirmed inferences never go to the
   model; confirmation is asked by the app.

### Assistant — build 1: mocks
Wrote `clock.py` (fake clock), `mocks/catalog.py`, `mocks/service.py` (8 tools, seeded RNG, forced
outcome queues, failure injection, JSON persistence). Smoke test: search returned 7 plumbers; forced
`timeout_booked` → `get_booking_status` → `confirmed`; replay with the same key → `idempotent_replay`.
Fixed a self-inflicted inconsistency (search price depended on area but hold price did not) by making
price area-independent.

### Assistant — build 2: agent core
Wrote `tracing.py`, `store.py` (SQLite), `memory.py`, `sanitize.py`, `approval.py`, `state.py`,
`limits.py`, `tools.py` (retries/backoff; never auto-retry `book`), `executor.py`, `context.py`,
`agent_tools.py`, `llm/nlu.py`, `llm/base.py`, `llm/rule_model.py`, `agent.py`, `proactive.py`,
`seed.py` (52 memories, 322-message history).

First smoke run: **every turn returned the generic error reply.** Trace: `model_error: Expecting
value`. Cause: the RuleModel's lazy `<state>(.*?)</state>` regex matched the word `<state>` in the
system prompt's prose. Fixed by anchoring on the JSON payload (logged in `docs/DEVLOG.md`).

Second run booked end to end with timeout-after-success. Two behaviour fixes: carry the injection
warning into the final reply; only ask to confirm inferences created during the current task (it had
asked about a stale seeded inference).

Commit: *Mock tool environment, agent core …*

### Assistant — build 3: evals
Wrote `evals/harness.py` (isolated env per scenario, process restart, simulated users, 7 global
invariants checked on service state / ledger / approvals / context payloads) and 34 scenarios;
`tests/conftest.py` writes `reports/eval_table.md` + `.csv` at the end of the pytest session.

First full run: **25/34**. Triage (details in `docs/DEVLOG.md`, three in `BUGS.md`):
* S20 → stale `Request` reference after `reset_for_new_task` (**BUG-1**).
* S22 → forgotten pet name leaked via a paraphrase in old history (**BUG-2**) — the test was written
  expecting this risk; it failed as predicted.
* S15 → `invalidate()` ignored `executing` proposals, so a pre-crash approval stayed live (**BUG-3**).
* S26 → context overshoot 2,286/2,200 (envelope not counted) → measure the real payload and trim.
* S17 → `get_provider` capped slots before filtering by date → Saturday slots never reached the model.
* S05 → "wait, what? no" not treated as a rejection.
* S24/S25/S09 → bugs in my own checks (invariant assumed bookings stay confirmed; stale object).

After fixes: 34/34. Wrote `tests/test_regressions.py` and **verified each BUGS.md regression test
fails with its fix reverted** (temporarily patched the three fixes out: 3 failed; restored: 5 passed).
Added `tests/test_units.py`, including mutation tests (bypassed gate / faked success claim must trip
the invariants) and a record → strict replay test. 61 tests passing.

Commit: *34 eval scenarios … fix 3 real bugs with regression tests*

### Assistant — build 4: front-ends, demo, docs
* `cli.py` and `web.py` (chat | trace side by side). Web smoke test hit
  `sqlite3.ProgrammingError: objects created in a thread…` → `check_same_thread=False` with the
  server's lock serialising access.
* Proactive replacement picked the provider with the injected review; added persistent
  `provider_flags` so flagged providers rank last.
* `scripts/record_demo.py`: drives the real web UI in headless Chrome over the DevTools protocol,
  captures frames, encodes `docs/demo/errand_demo.mp4`. Reviewing frames showed the chat pane did not
  scroll (flexbox `min-height` bug) and raw markdown underscores → fixed both and re-recorded (3:48).
* `DESIGN.md` (rendered to a 1-page PDF and checked by `scripts/design_pdf.py`), `BUGS.md`,
  `README.md`, `docs/DEMO.md`, this transcript.

### Not done in this session (and why)
* No live Claude calls: no API key/network in the build sandbox. The Anthropic adapter is unit-tested
  with a stubbed SDK; `docs/DEMO.md` explains recording the Loom with `--model anthropic --record`.

---

### Human
> just edit it with free grok api so that i can run it and test it

### Assistant
Looked up current providers: xAI Grok lists no free tier (cheapest `grok-4-1-fast-non-reasoning`);
Groq (`api.groq.com/openai/v1`) has a free key with no card. Both are OpenAI-compatible, so added one
stdlib adapter, `llm/openai_compat.py` (`--model groq` / `--model grok`), converting the agent's
Anthropic-style blocks to OpenAI `tool_calls`/`tool` messages and back; 429/5xx retry with backoff;
one retry on malformed tool calls. Added `.env` loading + `.env.example`, `python -m
errand_agent.check_llm`, friendly errors for missing/invalid keys, honest cost (Groq free = $0, unknown
prices reported as null instead of guessed), and an evidence-rejection message that tells the model what
to quote. Tests: conversion round trip, missing-key error, and a full booking turn through the adapter
with stubbed HTTP (64 passing). Could not call Groq/xAI from the sandbox (egress blocked).

---

### Human
> Call failed: HTTP 404: {"error":{"message":"The model `llama-3.3-70b-versatile` does not exist or you do not have access to it.","type":"invalid_request_error","code":"model_not_found"}}

### Assistant
Checked Groq's docs: the model is still listed, so the key most likely lacks access to it. Changed the
Groq default to `openai/gpt-oss-120b` and, instead of trusting any hardcoded id, made the adapter handle
`model_not_found` by calling `GET /models` and switching to the first tool-capable model the key can use
(printing which). `check_llm --list` prints the available ids. Raised `max_tokens` to 2048 for reasoning
models. Added two regression tests (fallback succeeds; clear error when nothing usable). 66 passing.

---

### Human
> (pasted a live Groq run: the reply promised to tell the technician about the dog; trace showed a second
> identical request replacing the proposal, `search_args_mismatch` on date_to, 0ms model latency, `$null` cost)

### Assistant
Diagnosed five issues from the trace and fixed them (see `docs/DEVLOG.md`, "First live run"): no promises of
untooled actions; identical repeat keeps the pending card; precomputed date ranges in `<today>`; app-
authoritative search arguments; real model latency that also advances the simulated clock. The test suite
then caught a follow-on: compression destroyed search results under the 2,200-token test budget, so it now
keeps valid JSON. Added `tests/test_live_issues.py` (7 tests). 74 passing. `$null` cost came from an older
zip; the current one prices Groq free-tier models at $0.
