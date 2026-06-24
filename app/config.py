import json
import os
import yaml
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _load_yaml_profile() -> dict:
    # candidate_profile.yaml is personal config, untracked from git and absent in a
    # fresh clone. Fall back to an empty profile so the app still boots; a real or
    # example profile (see WP-G) re-populates it.
    profile_path = ROOT / "candidate_profile.yaml"
    if not profile_path.exists():
        return {}
    with open(profile_path) as f:
        return yaml.safe_load(f) or {}


def load_profile() -> dict:
    """Load candidate profile. DB row takes precedence; seeds from YAML on first run."""
    try:
        from app.models import get_db
        with get_db() as conn:
            row = conn.execute("SELECT profile_json FROM candidate_settings WHERE id=1").fetchone()
            if not row:
                profile = _load_yaml_profile()
                conn.execute(
                    "INSERT INTO candidate_settings (id, profile_json) VALUES (1, ?)",
                    (json.dumps(profile),),
                )
                return profile
            return json.loads(row["profile_json"])
    except Exception:
        return _load_yaml_profile()


def save_profile(profile: dict) -> None:
    """Persist updated candidate profile to DB."""
    from app.models import get_db
    with get_db() as conn:
        conn.execute(
            "INSERT INTO candidate_settings (id, profile_json, updated_at) VALUES (1, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(id) DO UPDATE SET profile_json=excluded.profile_json, updated_at=excluded.updated_at",
            (json.dumps(profile),),
        )

# Environment variables (set in Modal Secrets)
# LLM provider selection (WP-B). Default: "gemini" (with Anthropic fallback on 429).
# Set to "anthropic" or "openai" to use that provider as primary.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "")  # empty → OpenAI default endpoint

# Voice add-on (WP-F). Optional de-AI post-processor for generated prose
# (cover letters today). Ships a working built-in guide; point VOICE_GUIDE_PATH
# at your own forbidden-words YAML to override. Set VOICE_ENABLED=0 to turn it
# off entirely (generation still works, prose just isn't polished).
VOICE_ENABLED = os.environ.get("VOICE_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off", "")
VOICE_GUIDE_PATH = os.environ.get("VOICE_GUIDE_PATH", "").strip()

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
INTERVIEW_PREP_DOC_ID = os.environ.get("INTERVIEW_PREP_DOC_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/data/recruiting.db")
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")


def is_google_configured() -> bool:
    """True when both GOOGLE_SHEET_ID and a Google OAuth token are present.

    Used to gate all Google Sheets calls (WP-M: Sheets optional).
    The token can come from the TOKEN_JSON_CONTENT env var (Modal Secret)
    or from a local token.json file (dev).
    """
    if not GOOGLE_SHEET_ID:
        return False
    token_present = bool(os.environ.get("TOKEN_JSON_CONTENT"))
    if not token_present:
        token_path = Path(__file__).parent.parent / "token.json"
        token_present = token_path.exists()
    return token_present


HIGH_SCORE_THRESHOLD = 8.0
STALE_RECRUITER_DAYS = 30
RESEARCH_CACHE_TTL_DAYS = 7
WATCHDOG_SCHEDULE = "0 * * * *"       # every hour
WEEKLY_DIGEST_SCHEDULE = "0 8 * * 1"  # Monday 8am
