"""Tool gateway: the only path to the provider service. Adds budgets, bounded
retries with backoff for transient errors, and tracing of every attempt."""
from __future__ import annotations

from typing import Any

from .limits import Budget
from .mocks.service import MockProviderService, ToolError, ToolTimeout, TransientError

# book is never retried blindly here: an error after the request left us is
# ambiguous, so the executor consults get_booking_status instead.
NO_AUTO_RETRY = {"book"}


class ToolFailure(Exception):
    """Raised after retries are exhausted or for a non-retryable error."""

    def __init__(self, tool: str, code: str, message: str, attempts: int):
        super().__init__(f"{tool} failed ({code}) after {attempts} attempt(s): {message}")
        self.tool, self.code, self.message, self.attempts = tool, code, message, attempts


class ToolGateway:
    def __init__(self, service: MockProviderService, tracer, clock, limits):
        self.svc = service
        self.tracer = tracer
        self.clock = clock
        self.limits = limits
        self.budget: Budget | None = None

    def call(self, name: str, caller: str = "app", **args: Any) -> dict:
        fn = getattr(self.svc, name)
        attempt = 0
        while True:
            attempt += 1
            if self.budget:
                self.budget.charge_tool_call()
            t0 = self.clock.monotonic()
            try:
                res = fn(**args)
                self.tracer.tool_call(name, args, (self.clock.monotonic() - t0) * 1000, result=res,
                                      attempt=attempt, caller=caller)
                return res
            except ToolTimeout as e:
                self.tracer.tool_call(name, args, (self.clock.monotonic() - t0) * 1000,
                                      error=f"timeout: {e.message}", attempt=attempt, caller=caller)
                if name in NO_AUTO_RETRY:
                    raise
                if attempt > self.limits.max_retries:
                    raise ToolFailure(name, "timeout", e.message, attempt) from e
                self.clock.sleep(self.limits.backoff_base_s * 2 ** (attempt - 1))
            except TransientError as e:
                self.tracer.tool_call(name, args, (self.clock.monotonic() - t0) * 1000,
                                      error=f"{e.code}: {e.message}", attempt=attempt, caller=caller)
                if name in NO_AUTO_RETRY:
                    raise ToolTimeout(f"{e.code} on book; outcome ambiguous") from e
                if attempt > self.limits.max_retries:
                    raise ToolFailure(name, e.code, e.message, attempt) from e
                self.clock.sleep(self.limits.backoff_base_s * 2 ** (attempt - 1))
            except ToolError as e:
                self.tracer.tool_call(name, args, (self.clock.monotonic() - t0) * 1000,
                                      error=f"{e.code}: {e.message}", attempt=attempt, caller=caller)
                raise ToolFailure(name, e.code, e.message, attempt) from e
            except Exception as e:  # noqa: BLE001 - unexpected tool bug must not crash the agent
                self.tracer.tool_call(name, args, (self.clock.monotonic() - t0) * 1000,
                                      error=f"unexpected: {type(e).__name__}: {e}", attempt=attempt,
                                      caller=caller)
                raise ToolFailure(name, "unexpected_error", f"{type(e).__name__}: {e}", attempt) from e
