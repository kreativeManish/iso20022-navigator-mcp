"""
calls.py — what happens around every tool call: rate limit, then a log line.

Every tool is registered through guarded(), which:
  1. enforces its rate-limit tier before any work is done [5.1];
  2. writes one log line per call (JSON, see server.py): tool, outcome, duration, response size,
     and only the inputs the tool declares safe to log.

Inputs not declared in log_inputs are never logged. Free-text inputs must not be
declared: callers may paste personal data into them.
"""
import functools
import logging
import os
import threading
import time

from mcp.server.mcpserver.exceptions import ToolError

log = logging.getLogger("iso20022_mcp.calls")


class RateLimiter:
    """A token bucket shared by all callers of one tier.

    The bucket holds up to per_minute calls and refills continuously, so short
    bursts are allowed while the long-run rate stays at per_minute. Tools run in
    worker threads, hence the lock.
    """

    def __init__(self, per_minute: int):
        self.capacity = float(per_minute)
        self.tokens = float(per_minute)
        self.rate = per_minute / 60.0                  # tokens per second
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> float:
        """Take one call. Returns 0 if allowed, else the seconds to wait."""
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return 0.0
            return (1 - self.tokens) / self.rate


# One limiter per tier. Lookup tools are cheap indexed queries; a future search
# tier would get its own, lower limit.
LIMITERS = {
    "lookup": RateLimiter(int(os.environ.get("RATE_LIMIT_LOOKUP_PER_MINUTE", "600"))),
}


def _outcome(result) -> tuple[str, int]:
    """Classify a tool result for the log: (outcome, response size in characters)."""
    text = result.content[0].text if getattr(result, "content", None) else ""
    data = getattr(result, "structured_content", None) or {}
    return ("found" if data.get("found") else "not_found"), len(text)


def guarded(tool_name: str, tier: str, log_inputs: tuple[str, ...] = ()):
    """Wrap a tool function with its rate limit and per-call logging."""
    if tier not in LIMITERS:
        raise ValueError(f"unknown rate-limit tier: {tier}")

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(**kwargs):
            entry = {"tool": tool_name,
                     "inputs": {k: kwargs[k] for k in log_inputs if kwargs.get(k) is not None}}
            wait = LIMITERS[tier].acquire()
            if wait:
                log.warning("tool call", extra={"fields": entry | {"outcome": "rate_limited"}})
                # Tells the model what to do next [4.2]
                raise ToolError(f"Rate limit reached for this server. Retry in {max(1, round(wait))} seconds.")
            start = time.monotonic()
            try:
                result = fn(**kwargs)
            except ToolError:
                log.warning("tool call", extra={"fields": entry | {
                    "outcome": "error", "ms": round((time.monotonic() - start) * 1000)}})
                raise
            outcome, size = _outcome(result)
            log.info("tool call", extra={"fields": entry | {
                "outcome": outcome, "chars": size, "ms": round((time.monotonic() - start) * 1000)}})
            return result
        return wrapper
    return decorate
