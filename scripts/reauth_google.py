"""
One-time script to re-authenticate Google OAuth with Sheets + Drive scopes.
Run from project root:
    python3 scripts/reauth_google.py

This overwrites token.json with a new token that covers both APIs.
After running, update the Modal secret:
    python3 scripts/update_modal_token.py
"""
from google_auth_oauthlib.flow import InstalledAppFlow
import json
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",  # full Drive access — needed for Docs API
]

ROOT = Path(__file__).parent.parent
CREDS_FILE = ROOT / "credentials.json"
TOKEN_FILE = ROOT / "token.json"

if not CREDS_FILE.exists():
    print("ERROR: credentials.json not found.")
    print("Download it from Google Cloud Console → APIs & Services → Credentials")
    print("(OAuth 2.0 Client ID → Desktop app → Download JSON → save as credentials.json)")
    exit(1)

flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
creds = flow.run_local_server(port=0)

token_data = {
    "token": creds.token,
    "refresh_token": creds.refresh_token,
    "token_uri": creds.token_uri,
    "client_id": creds.client_id,
    "client_secret": creds.client_secret,
    "scopes": list(creds.scopes),
    "universe_domain": "googleapis.com",
    "account": "",
    "expiry": creds.expiry.isoformat() if creds.expiry else None,
}

TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
print(f"New token saved to {TOKEN_FILE}")
print(f"Scopes: {list(creds.scopes)}")
