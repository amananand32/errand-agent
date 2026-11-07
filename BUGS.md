# Three real bugs hit while building

All three were caught by the eval suite on its first full run (25/34 passing). For each, the
regression test in `tests/test_regressions.py` was verified to **fail with the fix reverted** and
pass with it applied. Smaller issues from the same triage are in `docs/DEVLOG.md`.

---

## BUG-1 — A new request after a finished booking was silently written into a discarded object

**What broke.** Scenario S20: after booking a carpenter, the user said *"Also find a cleaner in
Jayanagar on Friday, no budget limit."* The model called `update_request` (ok), then
`search_providers`, which the app rejected with *"request incomplete: service, area, date window,
budget"* — even though the user had just given all four. The user got a confusing error.

**Why.** `_t_update_request` bound `wf, r = self.wf, self.wf.request` at the top. A new task after
`DONE` triggers `wf.reset_for_new_task()`, which *replaces* `wf.request` with a fresh `Request()`.
Every field was then written to the old object `r`, which was no longer referenced by the workflow.
The trace showed `request_updated changed=[service, date_from, date_to]` followed by an empty
request — the classic stale-alias bug.

**Fix.** Bind `r = wf.request` only *after* the possible reset (`agent.py`, comment references BUG-1).

**Regression test.** `test_bug1_new_request_after_completed_task_is_not_lost` — books once, then
sends a complete second request and asserts the workflow holds it and the turn ends `proposed`.

---

## BUG-2 — "Forget" removed the stored value but the fact still reached the model

**What broke.** Scenario S22 asserts that after *"forget about my dog"* the dog's name never
appears in any later model context. It failed: `"Bruno"` was still in the payload.

**Why.** Forgetting redacted the memory row and scrubbed the *exact* value string from stored
conversation history. But the long prior conversation contained a paraphrase written by the
assistant — *"Got it, I'll mention Bruno to technicians."* — which matches neither the value
(`Friendly dog named Bruno; keep the gate closed`) nor the user's original sentence. Because recent
history is sent to the model, the forgotten fact leaked back in through the transcript.

**Fix.** On forget, scrub (a) the value, (b) the user's original statements that created the memory
(stored as `evidence`), and (c) *distinctive tokens* from both — proper nouns and identifiers such
as `Bruno`, `Indiranagar`, `4B` — using word-boundary matching with a small stoplist
(`memory.py: distinctive_tokens`). Limitation, stated honestly: a paraphrase with no shared
distinctive token ("your pet") would survive; a production system would add an LLM-assisted
redaction pass or store derived summaries with provenance links so they can be invalidated.

**Regression test.** `test_bug2_forget_scrubs_paraphrased_mentions` — seeds the long history,
forgets the pet, runs a booking turn, asserts `Bruno` is absent from every later context payload
**and** from the stored messages table.

---

## BUG-3 — After a crash mid-booking, the old approval stayed live in the database

**What broke.** Scenario S15 crashes the process after the intent ledger says `in_flight` but
*before* `book()` is sent. On restart, recovery correctly asked `get_booking_status` (→
`not_found`) and told the user it did not go through. But when the user then said *"yes"* again,
the agent fell through to the model ("Happy to help — what do you need?") instead of saying there
was nothing to confirm — and the DB still held an **unconsumed approval** for the proposal.

**Why.** `ApprovalGate.authorize()` moves a proposal to `executing`. `invalidate()` only updated
rows `WHERE status IN ('pending','approved')`, so after a crash the `executing` proposal was never
invalidated and its approval row was never consumed. `authorize()` accepts `executing`, so any later
code path that re-entered execution for that proposal would have passed the approval check on a
stale, pre-crash approval — exactly what the recovery design forbids ("after an interruption we
verify; if nothing happened we ask again").

**Fix.** `invalidate()` now also covers `executing` and always consumes the approval rows
(`approval.py`, comment references BUG-3).

**Regression test.** `test_bug3_crash_leaves_no_live_approval` — crash before `book`, restart,
assert the proposal is `invalidated`, there are no unconsumed approvals, the repeated *"yes"* yields
`nothing_to_confirm`, and `book()` was never called.
