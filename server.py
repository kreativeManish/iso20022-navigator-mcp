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
"""
import logging
import os
import sys
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import PlainTextResponse

import mappings
from db import pool

# Logs go to stderr: with the stdio transport, stdout carries the protocol itself.
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

INSTRUCTIONS = (
    "Read-only reference data for ISO 20022 financial messaging, from the ISO 20022 Navigator "
    "(https://www.isonavigator.io/iso20022/). Use these tools for any question about which ISO 20022 "
    "message replaces or corresponds to a legacy message (SWIFT MT, NACHA, CHAPS legacy), or which "
    "payment schemes use an ISO 20022 message, even when the answer seems well known: they return "
    "sourced, current data. Answer from the returned data. If a response has "
    "found=false or notes saying data is not recorded, tell the user so; do not fill gaps from "
    "general knowledge. Pass on the notes in each response."
)

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True,
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

mcp.tool(name="iso20022_find_mappings", title="Find ISO 20022 mappings",
         annotations=READ_ONLY)(mappings.find_mappings)

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
)


if __name__ == "__main__":
    if os.environ.get("MCP_TRANSPORT", "stdio") == "http":
        mcp.run(transport="streamable-http",
                host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")),
                **HTTP_OPTIONS)
    else:
        mcp.run()
