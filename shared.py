"""
shared.py — pieces every tool uses: provenance, the data baseline, database access
that never leaks raw errors, and the response writer.

Release checklist references are in square brackets, e.g. [3.6].
"""
import json
import logging
import time

from pydantic import BaseModel, Field

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from db import fetch_all

log = logging.getLogger("iso20022_mcp.shared")


class Provenance(BaseModel):
    """Where the data comes from [3.6]. Each tool supplies its own `scope`."""
    source: str = "ISO 20022 Navigator reference data (https://www.isonavigator.io/iso20022/)"
    scope: str
    data_baseline: str | None = Field(
        None, description="Quarter of the Navigator's baseline data load, e.g. 4Q2025: data reflects that load "
                          "unless stated otherwise. This is when data was added to the Navigator, not an "
                          "ISO 20022 publication.")


# Current data release for provenance [3.6]
# Baseline = earliest load. Revisit if a full data refresh is ever loaded.
SQL_RELEASE = "SELECT release_id FROM i22_release ORDER BY release_date ASC LIMIT 1"

_RELEASE_TTL = 600          # seconds; data loads are rare, so a short cache saves a query per call
_release_cache: tuple[float, dict] | None = None


def current_release() -> dict:
    """Baseline data load for provenance [3.6]. A failure here is logged, not fatal:
    the answer is still valid without it, and the gap shows up in tests."""
    global _release_cache
    if _release_cache and time.monotonic() - _release_cache[0] < _RELEASE_TTL:
        return _release_cache[1]
    try:
        rows = fetch_all(SQL_RELEASE)
    except Exception:
        log.exception("could not read i22_release for provenance")
        return {}
    if not rows:
        return {}                          # not cached: an empty answer must not stick
    info = {"data_baseline": rows[0]["release_id"]}
    _release_cache = (time.monotonic(), info)
    return info


def query(tool: str, sql: str, params: dict) -> list[dict]:
    """Run a query; never let raw database errors reach the client [4.1]."""
    try:
        return fetch_all(sql, params)
    except Exception:
        log.exception("database error in %s", tool)
        raise ToolError("The ISO 20022 reference database is temporarily unavailable. Try again in a minute.")


def respond(result: BaseModel, keep: tuple[str, ...] = ()) -> CallToolResult:
    """Send the result without null fields or empty top-level lists [3.4].
    `keep` names top-level lists that stay even when empty, for tools whose result
    always carries that list."""
    data = result.model_dump(mode="json", exclude_none=True)
    data = {k: v for k, v in data.items() if v != [] or k in keep}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
        structured_content=data,
    )
