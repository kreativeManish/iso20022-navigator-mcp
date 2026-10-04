"""
db.py — database access for the ISO 20022 MCP server.

One shared connection pool for the whole server. Tools never open their own
connections; they call fetch_all(), which borrows a connection from the pool
and returns it when done.

Safety rules (see MCP Tool Release Checklist, sections 1 and 5):
  - SQL is always a fixed string written in code.
  - Caller values go ONLY through placeholders (%s or %(name)s), passed
    separately as params.
  - Never build SQL with f-strings, .format(), % or +.
"""
import os

from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# Reads .env locally. On Railway there is no .env file; DATABASE_URL comes
# from the service's environment variables and this call does nothing.
# override=True: values in .env win over any system variable of the same name.
load_dotenv(override=True)

pool = ConnectionPool(
    conninfo=os.environ["DATABASE_URL"],
    min_size=1,          # keep one connection warm
    max_size=5,          # hard cap: the server can never hold more than 5
    timeout=10,          # seconds a request waits for a free connection
    check=ConnectionPool.check_connection,  # test each connection before lending it; replaces ones the DB host dropped while idle
    kwargs={"row_factory": dict_row},   # rows come back as dicts
    open=False,          # opened explicitly when the server starts
)


def fetch_all(sql: str, params: tuple | dict = ()) -> list[dict]:
    """Run one read-only query and return all rows as a list of dicts.

    params: a tuple for %s placeholders, or a dict for %(name)s placeholders.
    """
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()
