"""stdio MCP server exposing SearchOps job notes to Claude Desktop.

Thin adapter. All HTTP/Keychain logic lives in notes_client.py, which does
not import `mcp` and does not know it is being called over a protocol —
mirrors the voice-engine adapter's split (adapters/mcp_server.py + voice_tools.py).

Run standalone to smoke-test:

    python3 server.py

Register in Claude Desktop's claude_desktop_config.json — see README.md in
this directory for the exact block and the one-time Keychain setup command.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import notes_client  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - environment problem, not logic
    sys.stderr.write(
        "searchops-notes MCP: the `mcp` SDK is not installed for this interpreter.\n"
        f"Interpreter: {sys.executable}\n"
        "Install with: python3 -m pip install -r requirements.txt\n"
    )
    raise

mcp = FastMCP("searchops-notes")
_client = notes_client.NotesClient()


@mcp.tool()
def find_target(query: str) -> dict:
    """Find a SearchOps job by fuzzy-matching company name or job title.

    Always call this before add_note — add_note takes an exact job_id and
    never does its own matching. Returns every candidate above the match
    threshold, best match first; it never picks one for you and never writes
    anything.

    If there is exactly one match, you can proceed to add_note with its
    job_id. If there are multiple matches, or the results don't look like
    what the user meant, ask the user to confirm which one before adding a
    note — do not guess.

    Args:
        query: Company name or job title, as the user said it (e.g. "Synthesia").
    """
    try:
        matches = _client.find_target(query)
    except notes_client.KeychainTokenError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Search failed: {e}"}
    return {"status": "ok", "matches": matches}


@mcp.tool()
def add_note(job_id: int, text: str) -> dict:
    """Add a note to a specific SearchOps job. Requires an exact job_id from
    find_target — never pass a company name or guess an ID.

    The note is appended to that job's history (never overwrites an existing
    note) and shows up immediately in the SearchOps web UI's "My notes" panel.

    Args:
        job_id: The exact job_id from a find_target result. Required.
        text: The note text (max 5000 characters).
    """
    try:
        result = _client.add_note(job_id, text, source="claude-desktop")
    except notes_client.KeychainTokenError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Add note failed: {e}"}
    return result


@mcp.tool()
def recent_notes(job_id: int, limit: int = 5) -> dict:
    """Read back the most recent notes for a job — use this to confirm a note
    landed after calling add_note, or to check what's already been noted
    before adding a new one.

    Args:
        job_id: The exact job_id (from a find_target result).
        limit: Max notes to return, newest first. Default 5.
    """
    try:
        notes = _client.recent_notes(job_id, limit=limit)
    except notes_client.KeychainTokenError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Fetch failed: {e}"}
    return {"status": "ok", "notes": notes}


if __name__ == "__main__":
    sys.stderr.write(f"searchops-notes MCP: base_url={_client.base_url}\n")
    mcp.run()
