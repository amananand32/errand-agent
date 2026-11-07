# Dev log (raw notes while building)

- BUG-1 (found in first smoke run): every turn ended in the generic error reply. Trace showed
  `model_error: Expecting value: line 1 column 1`. The RuleModel extracted state with the lazy regex
  `<state>(.*?)</state>`, but SYSTEM_PROMPT itself says "see <state>.missing" in prose, so the match began at
  the prose mention and swallowed half the prompt. Fix: anchor on the JSON payload `<state>(\{.*?\})</state>`.
  Side lesson: a model *code* bug was retried and reported as "model unavailable" - acceptable for a remote
  model, but the trace now carries the exception text so it is diagnosable.
