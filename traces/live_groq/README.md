# Live run: Groq `openai/gpt-oss-120b` (8 Nov 2026)

Real model and mock providers, `timeout` preset. These traces were recorded **before** the fixes in
`docs/DEVLOG.md` ("First live run"); the issues they reveal are what those fixes address.

| File | Turn | Outcome |
|---|---|---|
| `run-…-0000` | plumber request | proposed (live model: update_request → search → propose) |
| `run-…-0001` | proactive monitor tick | no change |
| `run-…-0002`, `-0003` | same request re-sent | re-proposed (bug: replaced the pending card; now fixed) |
| `run-…-0004` | **yes** | **`book` timed out after succeeding → `get_booking_status` → confirmed BK-2004, exactly one booking** |
| `run-…-0005` | yes again | already_done, no duplicate |

Known artefacts of the pre-fix build visible here: model `latency_ms` = 0 (measured on the simulated clock)
and `est_cost_usd` = null (gpt-oss was not yet in the free-model price table).
