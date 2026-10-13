"""
conftest.py — one connection pool for the whole test run.

The server's lifespan opens and closes the pool for each MCP client session. The
tests open many sessions, so the lifespan's open/close are made no-ops and the
pool is opened once here (only when a real database is configured).
"""
import pytest

import calls
import shared
import support


@pytest.fixture(scope="session", autouse=True)
def shared_pool():
    pool = support.db.pool
    real_open, real_close = pool.open, pool.close
    pool.open = lambda *args, **kwargs: None
    pool.close = lambda *args, **kwargs: None
    if support.HAS_DB:
        real_open(wait=True, timeout=30)
    yield
    if support.HAS_DB:
        real_close()


@pytest.fixture(autouse=True)
def fresh_release_cache():
    """Tests with a fake database must not leave a cached data baseline behind."""
    shared._release_cache = None
    yield
    shared._release_cache = None


@pytest.fixture(autouse=True)
def fresh_rate_limits(monkeypatch):
    """Every test starts with full rate-limit buckets.

    The limiter is one shared bucket per tier and the suite sends several hundred calls in bursts, so a test's
    outcome would otherwise depend on how long the tests before it took to run (a push to main failed twice on
    'Rate limit reached' in test_message_details.py after 2026-10-12). Tests that exercise the limit set their own
    limiter after this fixture runs.
    """
    for tier, limiter in list(calls.LIMITERS.items()):
        monkeypatch.setitem(calls.LIMITERS, tier, calls.RateLimiter(round(limiter.capacity)))
