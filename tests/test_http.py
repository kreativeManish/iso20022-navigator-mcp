"""
test_http.py — the HTTP transport, as deployed. No database needed.

Runs the real HTTP app (same settings as the deployed server) on a local port.
"""
import asyncio
import socket
import threading
import time

import httpx2
import pytest
import uvicorn

import shared
import support
from mcp import Client


@pytest.fixture(scope="module")
def base_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = support.server.mcp.streamable_http_app(**support.server.HTTP_OPTIONS)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)


def test_mcp_endpoint_lists_tools(base_url):
    """An MCP client can connect at MCP_PATH and see the tool."""
    async def go():
        async with Client(base_url + support.server.MCP_PATH) as client:
            return [t.name for t in (await client.list_tools()).tools]
    assert asyncio.run(go()) == [support.TOOL]


def test_health_ok_without_database(base_url, monkeypatch):
    """The health check answers 'ok' and never touches the database."""
    calls = []
    monkeypatch.setattr(shared, "fetch_all", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(support.db, "fetch_all", lambda *a, **k: calls.append(a))
    r = httpx2.get(base_url + support.server.MCP_PATH + "/health")
    assert r.status_code == 200 and r.text == "ok"
    assert calls == []


def test_unknown_path_not_found(base_url):
    assert httpx2.get(base_url + "/nothing-here").status_code == 404


def test_oversized_request_rejected(base_url):
    """Bodies over MAX_REQUEST_BYTES are refused before any MCP handling."""
    big = b"x" * (support.server.HTTP_OPTIONS["max_request_body_size"] + 1)
    r = httpx2.post(base_url + support.server.MCP_PATH, content=big,
                    headers={"content-type": "application/json", "accept": "application/json, text/event-stream"})
    assert r.status_code == 413
