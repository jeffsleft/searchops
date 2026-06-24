"""Modal-free ASGI entrypoint (WP-H — hosting decouple).

Exposes a module-level ``app`` that any standard ASGI server can serve, so the
engine can run beyond Modal (a container, a VM, Vercel, Cloudflare, etc.):

    uvicorn app.asgi:app --host 0.0.0.0 --port 8000
    gunicorn app.asgi:app -k uvicorn.workers.UvicornWorker

Run from the repo root (templates and ``app/static`` are resolved relative to
the working directory). Modal remains the reference deploy via ``app/main.py``;
this module shares the same ``create_app()`` factory and never imports ``modal``.

Secrets and config come from the process environment. On Modal they arrive via
``modal.Secret``; off Modal, set them in the shell or a ``.env`` file (auto-loaded
below). See ``docs/hosting.md`` for per-host setup and DB portability.

Background work caveat: no Modal functions are injected here, so company research
and Sheets sync run **in-process and synchronously** (``create_app`` handles the
``batch_research_fn=None`` / ``run_sync_fn=None`` case). The scheduled discovery
scan + weekly digest are Modal crons (``app/main.py``); off Modal, drive them with
your platform's own scheduler against the same routes/functions.
"""
import os
from pathlib import Path


def _load_dotenv() -> None:
    """Load a repo-root ``.env`` into os.environ if present.

    Mirrors run_local.py's tiny parser so there's no hard dependency on
    python-dotenv. Uses setdefault, so a real environment variable always wins
    over the file (the right precedence for production hosts).
    """
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


# Load .env BEFORE importing anything that reads os.environ at import time
# (app.config captures DATABASE_PATH, LLM_PROVIDER, etc. when first imported).
_load_dotenv()

from app.config import DATABASE_PATH  # noqa: E402  (must follow _load_dotenv)

# Ensure the SQLite parent directory exists. DATABASE_PATH defaults to the Modal
# Volume mount (/data/recruiting.db); off Modal, point it somewhere writable, e.g.
# DATABASE_PATH=./recruiting.db. Best-effort — if the path is genuinely unwritable,
# sqlite still raises a clear error at first connect.
try:
    Path(DATABASE_PATH).resolve().parent.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

from app.models import init_db  # noqa: E402
from app.routes import create_app  # noqa: E402

init_db()

# Module-level ASGI application. No Modal background fns injected (see caveat above).
app = create_app()
