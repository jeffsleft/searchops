"""HTTP + Keychain logic for the SearchOps notes MCP adapter.

Deliberately does not import `mcp` — this module is plain business logic
(read a token, call the deployed /api/notes/ routes) so it can be unit-tested
without a running MCP transport. server.py is the thin FastMCP wrapper around
it, mirroring the voice-engine adapter's split (adapters/mcp_server.py +
adapters/voice_tools.py).

All writes go through app.services.job_actions.add_job_note on the server
side (see app/routes.py's Notes API section) — this client never has its own
notion of "the note," it just calls the one endpoint.
"""
from __future__ import annotations

import os
import subprocess

import httpx

DEFAULT_BASE_URL = "https://jeffsleft--recruiting-engine-web.modal.run"
KEYCHAIN_SERVICE = "searchops-notes-api"
KEYCHAIN_ACCOUNT = "jeff"


class KeychainTokenError(RuntimeError):
    """Raised when the notes API token can't be read from macOS Keychain."""


def get_token(service: str = KEYCHAIN_SERVICE, account: str = KEYCHAIN_ACCOUNT) -> str:
    """Read the notes API token from macOS Keychain via `security`.

    Never stored in claude_desktop_config.json — only this Keychain lookup.
    One-time setup:
        security add-generic-password -a jeff -s searchops-notes-api -w '<token>'
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except FileNotFoundError as e:
        raise KeychainTokenError(
            "`security` command not found — this adapter is macOS-only."
        ) from e
    except subprocess.CalledProcessError as e:
        raise KeychainTokenError(
            f"No Keychain entry for service={service!r} account={account!r}. Add one with:\n"
            f"  security add-generic-password -a {account} -s {service} -w '<token>'"
        ) from e

    token = result.stdout.strip()
    if not token:
        raise KeychainTokenError(f"Keychain entry for {service!r}/{account!r} is empty.")
    return token


class NotesClient:
    """Thin client for the SearchOps /api/notes/ routes."""

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 15.0):
        self.base_url = (base_url or os.environ.get("SEARCHOPS_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._token = token
        self.timeout = timeout

    def _headers(self) -> dict:
        token = self._token or get_token()
        return {
            "Authorization": f"Bearer {token}",
            "X-Requested-With": "XMLHttpRequest",
        }

    def find_target(self, query: str) -> list[dict]:
        """Fuzzy match on company + job title. Read-only. Returns candidates,
        each {"job_id", "company", "title", "status", "updated_at"}."""
        resp = httpx.get(
            f"{self.base_url}/api/notes/jobs/search",
            params={"q": query},
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json().get("matches", [])

    def add_note(self, job_id: int, text: str, source: str = "claude-desktop") -> dict:
        """Append a note to an exact job_id. Never fuzzy-matches — call
        find_target first and resolve ambiguity before calling this."""
        resp = httpx.post(
            f"{self.base_url}/api/notes/jobs/{job_id}/notes",
            json={"text": text, "source": source},
            headers=self._headers(),
            timeout=self.timeout,
        )
        if resp.status_code in (400, 404):
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            return {"status": "error", "message": body.get("message", f"HTTP {resp.status_code}")}
        resp.raise_for_status()
        return resp.json()

    def recent_notes(self, job_id: int, limit: int = 5) -> list[dict]:
        """Read-back for confirming a note landed. Empty list for an unknown job_id."""
        resp = httpx.get(
            f"{self.base_url}/api/notes/jobs/{job_id}/notes",
            params={"limit": limit},
            headers=self._headers(),
            timeout=self.timeout,
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json().get("notes", [])
