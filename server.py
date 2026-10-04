"""
server.py — ISO 20022 Navigator MCP server.

Run locally (stdio, for Claude Desktop / MCP Inspector):   python server.py
"""
import logging
import sys
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

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


if __name__ == "__main__":
    mcp.run()
