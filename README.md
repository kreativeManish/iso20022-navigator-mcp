# ISO 20022 Navigator MCP server

An [MCP](https://modelcontextprotocol.io) server that gives AI assistants grounded answers
about ISO 20022 financial messaging, from the reference data behind the
[ISO 20022 Navigator](https://www.isonavigator.io/iso20022/).

> Status: in development. A public endpoint is not available yet.

## Tools

| Tool | What it answers |
|---|---|
| `iso20022_find_mappings` | Which ISO 20022 message replaces a legacy message (e.g. MT103 → pacs.008), and which payment schemes use an ISO 20022 message. Message-level only, not field-level. |

All tools are read-only.

## Run locally

Requires Python 3.14 and access to the Navigator database (not public).

```
python -m venv .venv
.venv\Scripts\activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Put the connection string in a `.env` file as `DATABASE_URL=...`, then run `python server.py`
(stdio transport, for Claude Desktop or MCP Inspector).

## Tests

```
pip install -r requirements-dev.txt
bandit -q -r . -x ./tests,./.venv
python -m pytest -q
```

Database tests are skipped when no `DATABASE_URL` is available.

## License

The code is under the [MIT License](LICENSE). The reference data served by the tools is not
part of this repository and is not covered by that license.
