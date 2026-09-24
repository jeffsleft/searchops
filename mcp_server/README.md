# SearchOps notes — Claude Desktop MCP server

Lets Claude Desktop add and read job notes in SearchOps directly, instead of
you doing it via terminal. Talks to the deployed app's `/api/notes/` routes
over HTTPS — it does not touch the SQLite file directly.

## One-time setup

**1. Generate a token and set it as a Modal Secret** (if not already done):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
modal secret create notes-api-token NOTES_API_TOKEN=<paste the token above>
```

Redeploy the app so it picks up the new secret: `modal deploy app/main.py` (run
from the `recruiting-engine/` root, not this directory).

**2. Store the same token in macOS Keychain** — never in
`claude_desktop_config.json`:

```bash
security add-generic-password -a jeff -s searchops-notes-api -w '<paste the same token>'
```

**3. Install this server's dependencies:**

```bash
cd mcp_server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**4. Register it in Claude Desktop.** Add to
`~/Library/Application Support/Claude/claude_desktop_config.json` under
`mcpServers` (mirrors the existing `voice-engine` entry in that file):

```json
"searchops-notes": {
  "command": "<path-to-repo>/recruiting-engine/mcp_server/.venv/bin/python",
  "args": [
    "<path-to-repo>/recruiting-engine/mcp_server/server.py"
  ]
}
```

Restart Claude Desktop. Ask it something like "Add a note to Synthesia: had a
great call with the VP Eng today" — it should call `find_target`, confirm the
match if there's more than one, then `add_note`.

## Troubleshooting

- **"No Keychain entry"**: re-run step 2. Check with
  `security find-generic-password -a jeff -s searchops-notes-api -w`.
- **401 from the API**: the Keychain token and the `notes-api-token` Modal
  Secret have drifted apart — regenerate and reset both.
- Logs land in `~/Library/Logs/Claude/mcp-server-searchops-notes.log`.

## Files

- `server.py` — FastMCP adapter (tool definitions, docstrings Claude reads to
  decide when/how to call each tool).
- `notes_client.py` — HTTP + Keychain logic. No `mcp` import, so it's
  unit-testable on its own (`tests/test_notes_client.py`).
