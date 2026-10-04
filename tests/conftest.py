"""
conftest.py — one connection pool for the whole test run.

The server's lifespan opens and closes the pool for each MCP client session. The
tests open many sessions, so the lifespan's open/close are made no-ops and the
pool is opened once here (only when a real database is configured).
"""
import pytest

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
