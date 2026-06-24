"""
After re-authenticating, run this to push the new token to Modal.
    python3 scripts/update_modal_token.py
"""
import subprocess
from pathlib import Path

token = (Path(__file__).parent.parent / "token.json").read_text()
result = subprocess.run(
    ["modal", "secret", "create", "google-token-file",
     f"TOKEN_JSON_CONTENT={token}", "--force"],
    capture_output=True, text=True,
)
print(result.stdout)
print(result.stderr)
