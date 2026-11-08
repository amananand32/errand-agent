# Dev log (raw notes while building)

- BUG-1 (found in first smoke run): every turn ended in the generic error reply. Trace showed
  `model_error: Expecting value: line 1 column 1`. The RuleModel extracted state with the lazy regex
  `<state>(.*?)</state>`, but SYSTEM_PROMPT itself says "see <state>.missing" in prose, so the match began at
  the prose mention and swallowed half the prompt. Fix: anchor on the JSON payload `<state>(\{.*?\})</state>`.
  Side lesson: a model *code* bug was retried and reported as "model unavailable" - acceptable for a remote
  model, but the trace now carries the exception text so it is diagnosable.

- First full eval run: 25/34 passed. Triage:
  - S20 -> BUG-1 (stale `Request` reference after `reset_for_new_task`) - see BUGS.md.
  - S22 -> BUG-2 (forget left paraphrased mentions such as "Bruno" in history) - see BUGS.md.
  - S15 -> BUG-3 (`invalidate()` ignored `executing` proposals; approval stayed live after crash) - BUGS.md.
  - S26: context overshot the 2,200-token cap (2,286). Section estimates ignored the JSON message envelope
    and the "older messages omitted" line. Fix: measure the assembled payload, drop oldest history until it
    fits. Regression: `test_context_budget_counts_full_payload`.
  - S17: `get_provider` results were capped to 12 slots *before* filtering by the requested date, so the
    Saturday slots never reached the model. Fix: filter to the request window, then cap.
  - S05: "wait, what? no" was classified as `other`, not a rejection. Fix: short negative replies reject.
  - S24/S25: my own invariant was wrong - it required the booking to still be `confirmed` at the end, but
    the provider had (correctly, per scenario) cancelled it. Fixed the invariant, not the agent.
  - S09: check read a stale `Proposal` object. Test bug.
- Proved the 3 BUGS.md regression tests fail on the pre-fix code (temporarily reverted each fix) and pass after.

## First live run (Groq, openai/gpt-oss-120b) - issues found and fixed
Regression tests: `tests/test_live_issues.py`.
- Model promised an untooled action ("I'll let the technician know about Bruno"). The contract has no notes
  field. Prompt now forbids promising actions without a tool and says home details are context only.
- Re-sending the identical request while a card was pending replaced a ₹650 option with a ₹1,100 one from a
  different provider. Pre-pass now detects an identical repeat and keeps the pending proposal.
- Date arithmetic: "this week" on Mon 9 Nov became ..12 Nov, then ..13 Nov. `<today>` now carries
  precomputed ranges (this week, next week, weekend, next 8 days).
- `search_providers` rejected mismatched model args, and the model then proposed from stale results. The
  recorded request is now authoritative: the search always uses it and tells the model what it searched with.
- Model latency showed 0ms: it was measured on the FakeClock. Now measured in real time, and the FakeClock is
  advanced by it, so hold expiry and the wall-clock limit see real model latency.
- Follow-on found by the suite: the longer prompt triggered in-run compression, which truncated the search
  result to 300 raw chars and destroyed `ranked_options`. Compression now keeps valid JSON (top 3, bulky
  fields dropped) and never compresses the latest tool results.
