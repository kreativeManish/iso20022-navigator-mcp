"""
server.py — ISO 20022 Navigator MCP server.

Run locally (stdio, for Claude Desktop / MCP Inspector):   python server.py
Run over HTTP (as deployed):        MCP_TRANSPORT=http python server.py

Settings (environment variables):
  MCP_TRANSPORT  stdio (default) or http
  MCP_PATH       URL path of the MCP endpoint over HTTP (default /iso20022);
                 the health check is served at <MCP_PATH>/health
  HOST           address to listen on over HTTP (default 127.0.0.1; 0.0.0.0 on a host like Railway)
  PORT           port to listen on over HTTP (default 8000; hosts like Railway set it)
  RATE_LIMIT_LOOKUP_PER_MINUTE   calls per minute across all callers, lookup tools (default 600)
  MAX_CONCURRENT_REQUESTS        simultaneous HTTP requests before 503 (default 20)
  MAX_REQUEST_BYTES              largest accepted request body (default 65536)
"""
import json
import logging
import os
import sys
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import PlainTextResponse

import bank_transaction_codes
import mappings
from calls import guarded
from db import pool


class JsonFormatter(logging.Formatter):
    """One JSON object per line, so the host's log viewer can filter on fields
    (e.g. tool, outcome). Structured fields come from extra={"fields": {...}}."""

    def format(self, record: logging.LogRecord) -> str:
        line = {"time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                "logger": record.name, "msg": record.getMessage()}
        line |= getattr(record, "fields", {})
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, ensure_ascii=False, default=str)


# Logs go to stderr: with the stdio transport, stdout carries the protocol itself.
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])

INSTRUCTIONS = (
    "Read-only reference data for ISO 20022 financial messaging, from the ISO 20022 Navigator "
    "(https://www.isonavigator.io/iso20022/). Use these tools for any question about what an ISO 20022 "
    "Bank Transaction Code (domain, family, subfamily) means, which ISO 20022 message replaces or "
    "corresponds to a legacy message (SWIFT MT, NACHA, CHAPS legacy), or which payment schemes use an "
    "ISO 20022 message, even when the answer seems well known: they return sourced, current data. "
    "For a bank transaction code, send the full code to get its meaning; to find a code for a transaction "
    "type, browse from the domain down. For 'which schemes use X', call once without a standard filter. "
    "Answer from the returned data. If a response has found=false or notes saying data is not recorded, "
    "tell the user so; do not fill gaps from general knowledge. A bank transaction code that is not found "
    "may be a bank-specific code outside the ISO list; say that, not that it is invalid. Pass on the notes "
    "in each response. Some explanations are AI-generated. Verify against official ISO 20022 and scheme "
    "documentation before use in production."
)

READ_ONLY = dict(read_only_hint=True, idempotent_hint=True,
                 open_world_hint=False, destructive_hint=False)


@asynccontextmanager
async def lifespan(server: MCPServer):
    pool.open(wait=True, timeout=15)       # one shared pool for the server's lifetime
    try:
        yield {}
    finally:
        pool.close()


mcp = MCPServer(name="iso20022-navigator", version="0.1.0",
                instructions=INSTRUCTIONS, lifespan=lifespan)

# Each tool: its rate-limit tier, and the inputs that are safe to log (identifiers only, never free text).
# Registered in this order, which is the order clients list them: bank transaction codes first.
BTC_TITLE = "ISO 20022 bank transaction codes"
mcp.tool(name=bank_transaction_codes.TOOL_NAME, title=BTC_TITLE,
         annotations=ToolAnnotations(title=BTC_TITLE, **READ_ONLY))(
    guarded(bank_transaction_codes.TOOL_NAME, tier="lookup", log_inputs=("code",))(
        bank_transaction_codes.find_transaction_codes))

MAPPINGS_TITLE = "ISO 20022 legacy and scheme mappings"
mcp.tool(name="iso20022_legacy_and_scheme_mappings", title=MAPPINGS_TITLE,
         annotations=ToolAnnotations(title=MAPPINGS_TITLE, **READ_ONLY))(
    guarded("iso20022_legacy_and_scheme_mappings", tier="lookup", log_inputs=("message", "standard"))(
        mappings.find_mappings))

MCP_PATH = "/" + os.environ.get("MCP_PATH", "/iso20022").strip("/")


@mcp.custom_route(MCP_PATH + "/health", methods=["GET"], include_in_schema=False)
async def health(request: Request) -> PlainTextResponse:
    """Liveness only: never touches the database, so frequent checks cannot keep
    an idle database awake or restart the server over a database problem."""
    return PlainTextResponse("ok")


# Shared by the deployed server and the tests, so both exercise the same settings.
HTTP_OPTIONS = dict(
    streamable_http_path=MCP_PATH,
    stateless_http=True,       # no per-client session: restarts never break a client
    json_response=True,        # one JSON reply per call; nothing to stream
    max_request_body_size=int(os.environ.get("MAX_REQUEST_BYTES", "65536")),   # 413 above this
)


if __name__ == "__main__":
    if os.environ.get("MCP_TRANSPORT", "stdio") == "http":
        import uvicorn
        host = os.environ.get("HOST", "127.0.0.1")
        uvicorn.run(
            mcp.streamable_http_app(host=host, **HTTP_OPTIONS),
            host=host,
            port=int(os.environ.get("PORT", "8000")),
            limit_concurrency=int(os.environ.get("MAX_CONCURRENT_REQUESTS", "20")),   # 503 above this
            log_config=None,              # keep the JSON logging configured above
        )
    else:
        mcp.run()
