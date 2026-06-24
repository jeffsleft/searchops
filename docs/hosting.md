# Hosting beyond Modal

Modal is the **reference deploy** (`app/main.py` → `modal deploy`), and it's the
easiest path: serverless containers, a built-in Volume for SQLite, secrets, and
cron all in one file. But nothing in the request/response path is Modal-specific.
The app is a plain [Starlette](https://www.starlette.io/) ASGI application built by
`app.routes.create_app()`, exposed as an importable module at **`app/asgi.py`**:

```python
# app/asgi.py
app = create_app()   # standard ASGI callable — no `import modal`
```

So any host that can run an ASGI app can run this engine.

```bash
uvicorn app.asgi:app --host 0.0.0.0 --port 8000
gunicorn app.asgi:app -k uvicorn.workers.UvicornWorker
```

Run from the repo root — Jinja templates and `app/static` are resolved relative to
the working directory.

---

## What you give up off Modal

Modal provides three things for free that you replace yourself elsewhere:

| Modal feature | What it does | Off-Modal replacement |
|---|---|---|
| **Volume** (`/data`) | Persistent disk for `recruiting.db` | A mounted volume / persistent disk, or a real DB (see [DB portability](#database-portability)) |
| **Secret** | Injects env vars into the container | Your host's env-var / secrets manager, or a `.env` file |
| **Cron** | Scheduled discovery scan + weekly digest + DB backup (`scheduler()` in `app/main.py`) | Your platform's scheduler hitting the same functions, or an external cron |

**Background-task caveat.** On Modal, "research this batch" and "sync Sheets" run as
spawned background functions. `app/asgi.py` injects no Modal functions, so those run
**in-process and synchronously** (`create_app` already falls back when
`batch_research_fn` / `run_sync_fn` are `None`). That's fine for a single-user app on
a long-lived server; a request that triggers research just blocks until it's done.
The scheduled jobs (`scheduler()`) are **not** wired off Modal — drive them with your
host's cron against an authenticated endpoint or a CLI invocation if you need them.

---

## Configuration (all hosts)

Everything is read from the process environment (`app/config.py`). Provide these via
your host's secrets mechanism or a `.env` file at the repo root (`app/asgi.py`
auto-loads `.env`; a real env var always wins over the file):

```
APP_PASSWORD=...            # login password (required)
SESSION_SECRET=...          # 32+ random chars for signed sessions
LLM_PROVIDER=gemini         # gemini | anthropic | openai
GEMINI_API_KEY=...          # (or ANTHROPIC_API_KEY / OPENAI_API_KEY to match provider)
DATABASE_PATH=./recruiting.db   # writable path — see DB portability below
# Optional: GOOGLE_SHEET_ID, SLACK_WEBHOOK_URL, JINA_API_KEY, FIRECRAWL_API_KEY
```

`DATABASE_PATH` defaults to `/data/recruiting.db` (the Modal Volume mount). Off Modal,
**set it to a writable location.** `app/asgi.py` creates the parent directory if it
can; point it at a mounted persistent disk so data survives restarts.

> **Secrets hygiene.** When you use a `.env` file (rather than your host's secrets
> manager), it holds `APP_PASSWORD`, `SESSION_SECRET`, and API keys in plaintext —
> lock it down to the app user only: `chmod 600 .env`. Keep it out of the image and
> out of git (`.gitignore` already covers `.env`). On a managed platform (Vercel,
> Render, Fly, Cloud Run), prefer the platform's env/secrets store over a committed
> file. Rotate `SESSION_SECRET` and `APP_PASSWORD` if a `.env` is ever exposed.

---

## Option A — Container / VM (recommended off Modal)

The most direct port. A long-lived process with a persistent disk for the SQLite file.

```dockerfile
FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATABASE_PATH=/srv/data/recruiting.db
EXPOSE 8000
CMD ["gunicorn", "app.asgi:app", "-k", "uvicorn.workers.UvicornWorker", \
     "-b", "0.0.0.0:8000", "--workers", "1", "--timeout", "120"]
```

- Mount a volume at `/srv/data` so `recruiting.db` persists across restarts.
- Keep `--workers 1` while on SQLite (WAL handles concurrent reads, but multiple
  worker processes writing one SQLite file invites lock contention). Scale out only
  after moving to Postgres.
- The long `--timeout` accommodates the in-process research/sync described above.
- Works on Fly.io, Render, Railway, a plain VPS, ECS/Cloud Run, etc.

---

## Option B — Vercel

Vercel runs Python as serverless functions, which fits the request path but **not** a
persistent SQLite file (the filesystem is ephemeral and per-invocation). Use Vercel
**only** with an external database (Postgres — see below).

- Add a function entrypoint that re-exports the ASGI app, e.g. `api/index.py`:
  ```python
  from app.asgi import app  # Vercel's Python runtime serves this ASGI app
  ```
- Route everything to it in `vercel.json` (`"rewrites": [{ "source": "/(.*)",
  "destination": "/api/index" }]`).
- Set `DATABASE_PATH` to a path that points at Postgres via the portability shim, or
  migrate `app/models.py` to a Postgres driver (see below). **Do not** rely on local
  SQLite on Vercel.
- Scheduled jobs: use Vercel Cron to hit an authenticated route.

---

## Option C — Cloudflare

Cloudflare Workers run JavaScript/WASM, not native CPython, so the Starlette app does
**not** run as-is on Workers. Two realistic paths:

1. **Cloudflare Containers** (or a container host fronted by Cloudflare) — deploy the
   Option A image and put Cloudflare in front for TLS/CDN/WAF. This keeps the Python
   app intact. Recommended.
2. **Full rewrite to Workers + D1** — only if you specifically want edge execution.
   This is a port, not a config change: the request handlers, templating, and the
   SQLite access layer would be reimplemented on the Workers runtime with
   [D1](https://developers.cloudflare.com/d1/) as the database. Out of scope here.

For most users wanting "Cloudflare," Option C.1 (a container behind Cloudflare) is the
answer.

---

## Database portability

The data layer is hand-written SQL through one chokepoint — `get_db()` in
`app/models.py` (`sqlite3.connect(DATABASE_PATH)`). That's deliberately easy to swap.

- **SQLite (default).** Zero-config. A single file at `DATABASE_PATH`. Great for one
  user on one server with a persistent disk. This is what Modal's Volume gives you.
- **Postgres.** For multiple workers, multiple hosts, or a serverless platform with no
  persistent disk (Vercel). The migration is contained:
  1. Repoint `get_db()` at a Postgres driver (`psycopg`), keeping the
     `contextmanager` + `row_factory`-style dict rows shape so call sites don't change.
  2. Translate the schema in `app/models.py` (`SCHEMA` + the `ALTER TABLE` migrations):
     `INTEGER PRIMARY KEY AUTOINCREMENT` → `SERIAL`/`IDENTITY`, `CURRENT_TIMESTAMP`
     stays, `last_insert_rowid()` → `RETURNING id`.
  3. Swap parameter style (`?` → `%s`) — the one mechanical find-and-replace across
     queries.
  Supabase/Neon/RDS all work; set the connection string via env.
- **Cloudflare D1.** Only relevant on the full-Workers rewrite (Option C.2). D1 speaks
  SQLite SQL, so the schema ports cleanly, but access goes through the Workers binding
  API rather than `sqlite3` — it's part of that rewrite, not a drop-in for the Python app.

Whichever you choose, the app boots and initializes the schema on first run via
`init_db()`, called for you in `app/asgi.py`.
