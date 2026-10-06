"""
support.py — shared helpers for the tests and tools/access_snapshot.py.

Release checklist references are in square brackets, e.g. [8.5].
"""
import asyncio
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Static tests run without a database. db.py needs DATABASE_URL at import time,
# so a placeholder is set when none is supplied (in CI, an absent secret arrives
# as an empty string). Locally, db.py then loads .env, which replaces it.
PLACEHOLDER_URL = "postgresql://placeholder@127.0.0.1:1/none"
if not os.environ.get("DATABASE_URL"):
    os.environ["DATABASE_URL"] = PLACEHOLDER_URL

import db  # noqa: E402
import server  # noqa: E402
from mcp import Client  # noqa: E402

HAS_DB = os.environ["DATABASE_URL"] != PLACEHOLDER_URL
TOOL = "iso20022_legacy_and_scheme_mappings"


def call_tool(*arg_sets: dict) -> list:
    """Call the tool once per argument dict through a real in-process MCP client."""
    async def go():
        async with Client(server.mcp) as client:
            return [await client.call_tool(TOOL, args) for args in arg_sets]
    return asyncio.run(go())


def tool_definition() -> dict:
    """The tool definition exactly as a client model sees it."""
    async def go():
        async with Client(server.mcp) as client:
            tool = (await client.list_tools()).tools[0]
            return tool.model_dump(mode="json", exclude_none=True, by_alias=True)
    return asyncio.run(go())


# [8.5] Every table and column the connected role can read, outside system schemas.
ACCESS_SQL = """
SELECT n.nspname AS schema_name, c.relname AS table_name, a.attname AS column_name
FROM   pg_class c
JOIN   pg_namespace n ON n.oid = c.relnamespace
JOIN   pg_attribute a ON a.attrelid = c.oid
WHERE  c.relkind IN ('r', 'v', 'm', 'p', 'f')
AND    n.nspname NOT IN ('pg_catalog', 'information_schema')
AND    n.nspname NOT LIKE 'pg_toast%%'
AND    a.attnum > 0 AND NOT a.attisdropped
AND    has_column_privilege(c.oid, a.attnum, 'SELECT')
ORDER  BY 1, 2, 3
"""


def access_snapshot() -> tuple[list[str], str]:
    """Readable columns as 'schema.table.column' lines, and their SHA-256.

    Only the hash is kept in the public repository; the list itself is reviewed
    privately (checklist 8.2) so the repository does not publish the schema.
    """
    lines = [f"{r['schema_name']}.{r['table_name']}.{r['column_name']}" for r in db.fetch_all(ACCESS_SQL)]
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    return lines, digest
