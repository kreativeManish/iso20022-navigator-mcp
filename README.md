# ISO 20022 Navigator MCP server

An [MCP](https://modelcontextprotocol.io) server that gives AI assistants grounded answers
about ISO 20022 financial messaging, from the reference data behind the
[ISO 20022 Navigator](https://www.isonavigator.io/iso20022/).

Free and public. The endpoint URL is all a client needs.

**Endpoint:** `https://mcp.isonavigator.io/iso20022` (Streamable HTTP, open access)

## Tools

All tools are read-only.

| Tool | What it answers |
|---|---|
| [`iso20022_legacy_and_scheme_mappings`](#iso20022_legacy_and_scheme_mappings) | Which ISO 20022 message replaces a legacy message, and which payment schemes use an ISO 20022 message. |

### `iso20022_legacy_and_scheme_mappings`

- **Answers:** which ISO 20022 message replaces a legacy message (SWIFT MT, NACHA, CHAPS legacy), for example MT103 → pacs.008, and which payment schemes use an ISO 20022 message.
- **Inputs:** `message` (a legacy or ISO 20022 message, such as `MT103`, `940` or `pacs.008`) and an optional `standard` (narrows the answer to one standard or scheme, such as `SEPA`).
- **Returns:** message-level mappings with status, scheme usage, notes and a `provenance` block.
- **Example questions:**
  - What replaced MT103?
  - What is the ISO 20022 equivalent of MT940?
  - Which payment schemes use pacs.008?
  - Is tsin.004 still current?
- **Notes:**
  - Latest message version only. A version in the input (for example `pacs.008.001.08`) maps to its message (`pacs.008`).
  - Retired messages are returned with the data load in which they were retired.
  - Responses list what the Navigator has recorded. When a mapping is missing, the response says so and directs assistants to answer from the recorded data.

## Getting the assistant to use it

Some assistants answer from general knowledge unless asked to use a tool, a choice made by the assistant. Adding the following line to a Project's instructions helps the assistant use the tool where it applies:

> For any question about ISO 20022 messages, their legacy equivalents (SWIFT MT, NACHA) or which payment schemes use a message, call the ISO Navigator connector first and answer from its data.

Other assistants may behave differently. If an answer comes without a tool call, ask again with "according to the ISO Navigator".

## Usage limits

- **Shared rate limit.** The limit is 600 calls per minute across all users together. When it is reached, the response says how long to wait before retrying.
- **Request size.** Requests up to 64 KB are accepted, and the server handles up to 20 requests at the same time.

## Data

Every response carries a `provenance` block. Its `data_baseline` (for example `4Q2025`) is the quarter of the Navigator's baseline data load, which marks when the data was added to the Navigator and differs from any ISO 20022 publication date. Check the scheme's or SWIFT's own current documentation before relying on a migration date.

Some explanations are AI-generated. Verify against official ISO 20022 and scheme documentation before use in production.

## Connect

Use the endpoint URL above. Menu names change between releases; if a label
below differs from what you see, the client's own MCP documentation is the reference.

### Claude (web and desktop app)

1. Open **Customize → Connectors**, choose **+ Add → Add custom connector**.
2. Enter a name (for example `ISO 20022 Navigator`) and the endpoint URL. Leave authentication off.
3. In a chat, use the **+** button at the lower left, then **Connectors**, and switch the connector on.

On Team and Enterprise plans an owner first adds it under **Organization settings → Connectors → Add → Custom → Web**;
members then connect it from **Customize → Connectors**. The Free plan allows one custom connector.
See [Anthropic's guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

### Claude Code

```
claude mcp add --transport http iso20022 https://mcp.isonavigator.io/iso20022
```

Check it with `claude mcp list`, or `/mcp` inside Claude Code. Add `--scope user` to make it available in every project.

### VS Code with GitHub Copilot

1. Run **MCP: Open User Configuration** from the Command Palette (or create `.vscode/mcp.json` in a project).
2. Add:

   ```json
   {
     "servers": {
       "iso20022": {
         "type": "http",
         "url": "https://mcp.isonavigator.io/iso20022"
       }
     }
   }
   ```

3. Run **MCP: List Servers**, select `iso20022` and start (or restart) it.
4. Use Copilot Chat in **Agent** mode; the tool appears in the tools list.

### Cursor

Add to `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project):

```json
{
  "mcpServers": {
    "iso20022": {
      "url": "https://mcp.isonavigator.io/iso20022"
    }
  }
}
```

Tools can be switched on and off from the chat's tools list.

### ChatGPT

Custom MCP connectors need developer mode and are available on the web. Availability depends on your plan; see
[OpenAI's guide](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt).
In short: turn on developer mode under **Settings → Apps → Advanced settings**, choose **Create**, enter the endpoint URL
and leave authentication off, select **Scan Tools**, then **Create**. This server reads data, so read-only access is enough.

### Any other MCP client

Any client that supports remote MCP servers over Streamable HTTP can use the endpoint URL. A database-independent
health check is at `https://mcp.isonavigator.io/iso20022/health`.

## Run locally

Requires Python 3.14 and access to the Navigator database (private).

```
python -m venv .venv
.venv\Scripts\activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Put the connection string in a `.env` file as `DATABASE_URL=...`, then either:

- `python server.py` — stdio transport, for Claude Desktop or MCP Inspector
- set `MCP_TRANSPORT=http`, then `python server.py` — HTTP transport as deployed, at
  `http://localhost:8000/iso20022` (health check: `/iso20022/health`)

See the top of `server.py` for the other settings.

## Tests

```
pip install -r requirements-dev.txt
bandit -q -r . -x ./tests,./.venv
python -m pytest -q
```

Database tests run when `DATABASE_URL` is available.

## License

The code is under the [MIT License](LICENSE). The reference data served by the tools sits outside this repository and its license.
