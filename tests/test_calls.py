"""
test_calls.py — the per-call guard: rate limit and logging. No database needed.

Release checklist references are in square brackets, e.g. [5.1].
"""
import logging

import pytest

import calls
import shared
import support


class _FakeDatabase:
    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return []


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDatabase()
    monkeypatch.setattr(shared, "fetch_all", db)
    return db


@pytest.fixture
def tight_limit(monkeypatch):
    """A lookup tier that allows exactly 3 calls, then refills slowly."""
    monkeypatch.setitem(calls.LIMITERS, "lookup", calls.RateLimiter(3))


def _call_records(caplog):
    return [r for r in caplog.records if r.name == "iso20022_mcp.calls"]


def test_rate_limit_refuses_with_retry_hint(fake_db, tight_limit):
    """[5.1][4.2] Over the limit, the call is refused before any database work, with a retry hint."""
    results = support.call_tool(*[{"message": "MT103"}] * 4)
    assert not any(r.is_error for r in results[:3])
    refused = results[3]
    assert refused.is_error
    assert "Rate limit reached" in refused.content[0].text and "Retry in" in refused.content[0].text
    queries_for_three_calls = fake_db.calls
    support.call_tool({"message": "MT103"})            # still refused: no new queries
    assert fake_db.calls == queries_for_three_calls


def test_limiter_refills():
    limiter = calls.RateLimiter(60)                    # 1 per second
    for _ in range(60):
        assert limiter.acquire() == 0
    wait = limiter.acquire()
    assert 0 < wait <= 1.0


def test_log_line_has_declared_inputs_and_outcome(fake_db, caplog):
    caplog.set_level(logging.INFO, logger="iso20022_mcp.calls")
    support.call_tool({"message": "MT103", "standard": "SWIFT_MT"})
    [record] = _call_records(caplog)
    f = record.fields
    assert f["tool"] == support.MAPPINGS_TOOL
    assert f["inputs"] == {"message": "MT103", "standard": "SWIFT_MT"}
    assert f["outcome"] == "not_found"
    assert isinstance(f["ms"], int) and isinstance(f["chars"], int)


def test_undeclared_inputs_never_logged(monkeypatch, fake_db, caplog):
    """Only inputs listed in log_inputs reach the log."""
    caplog.set_level(logging.INFO, logger="iso20022_mcp.calls")
    wrapped = calls.guarded("t", tier="lookup", log_inputs=("message",))(lambda **kw: None)
    monkeypatch.setattr(calls, "_outcome", lambda result: ("found", 0))
    wrapped(message="MT103", standard="SECRET-ISH")
    [record] = _call_records(caplog)
    assert record.fields["inputs"] == {"message": "MT103"}
    assert "SECRET-ISH" not in record.getMessage() + str(record.fields)


def test_refusal_and_errors_are_logged(monkeypatch, tight_limit, caplog):
    caplog.set_level(logging.INFO, logger="iso20022_mcp.calls")
    def broken(*args, **kwargs):
        raise RuntimeError("db down")
    monkeypatch.setattr(shared, "fetch_all", broken)
    support.call_tool(*[{"message": "MT103"}] * 4)
    outcomes = [r.fields["outcome"] for r in _call_records(caplog)]
    assert outcomes == ["error", "error", "error", "rate_limited"]


def test_btc_log_line_has_code_and_outcome(fake_db, caplog):
    """The code is an identifier, so it is logged; the outcome comes from the top-level found."""
    caplog.set_level(logging.INFO, logger="iso20022_mcp.calls")
    support.call_tool({"code": "PMNT-RCDT-ESCT"}, tool=support.BTC_TOOL)
    [record] = _call_records(caplog)
    f = record.fields
    assert f["tool"] == support.BTC_TOOL
    assert f["inputs"] == {"code": "PMNT-RCDT-ESCT"}
    assert f["outcome"] == "not_found"


def test_both_tools_share_the_lookup_limit(fake_db, tight_limit):
    """[5.1] One bucket for all callers and both tools: calls on either tool use the same allowance."""
    support.call_tool({"code": "PMNT"}, {"code": "PMNT"}, tool=support.BTC_TOOL)
    results = support.call_tool(*[{"message": "MT103"}] * 2)
    assert not results[0].is_error
    assert results[1].is_error and "Rate limit reached" in results[1].content[0].text
