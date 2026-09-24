"""Single-writer plumbing for the web container.

SQLite on a Modal Volume is last-writer-wins at the file level: any second
container that mounts the Volume and later commits can silently erase writes
made elsewhere (memory/lessons_learned.md, Sessions 47/51 and 2026-09-16).
The fix is structural. Only the web container mounts the Volume, and it runs
as exactly one container (app/main.py). Everything that used to run in its own
container (discovery scan, company research, backups, admin one-offs) now runs
here, on BackgroundRunner's threads.

Running as one container also means a slow request must never hold the event
loop, or every other page freezes behind it. `offload` moves each route onto a
worker thread with its own event loop.

Nothing here imports modal, so the local ASGI app and the tests use it as-is.
"""
import asyncio
import contextvars
import functools
import inspect
import logging
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional

log = logging.getLogger("app.background")


class BackgroundRunner:
    """Runs named jobs on a small thread pool, one run per name at a time,
    and commits the Volume after each job finishes."""

    def __init__(self, commit_fn: Optional[Callable[[], None]] = None, max_workers: int = 2):
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="bg-job")
        self._commit_fn = commit_fn
        self._running: set[str] = set()
        self._lock = threading.Lock()

    def is_running(self, name: str) -> bool:
        with self._lock:
            return name in self._running

    def submit(self, name: str, fn: Callable, *args, **kwargs) -> Optional[Future]:
        """Start `fn` in the background. Returns None if a job with this name
        is already running, so double-clicks and overlapping cron ticks are
        no-ops instead of duplicate work."""
        with self._lock:
            if name in self._running:
                return None
            self._running.add(name)

        def _run():
            try:
                return fn(*args, **kwargs)
            except Exception:
                log.exception("background job %s failed", name)
                raise
            finally:
                self.commit()
                with self._lock:
                    self._running.discard(name)

        try:
            return self._pool.submit(_run)
        except Exception:
            # e.g. pool shut down during container teardown; don't leave the
            # name stuck as "running" forever.
            with self._lock:
                self._running.discard(name)
            raise

    def commit(self) -> None:
        if self._commit_fn is None:
            return
        try:
            self._commit_fn()
        except Exception:
            log.exception("volume commit failed after background job")


class _ThreadLocalTee:
    """sys.stdout stand-in: everything still reaches the real stream (container
    logs), and a thread that registered a buffer also gets its own copy."""

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def write(self, s):
        buf = getattr(self._local, "buf", None)
        if buf is not None:
            buf.append(s)
        return self._real.write(s)

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


_tee_lock = threading.Lock()


def run_capturing_output(fn: Callable, *args, **kwargs) -> tuple:
    """Run fn and return (result, everything it printed). Admin one-offs report
    through print(); this sends a dry run's preview back to the operator's
    terminal instead of leaving it in the web container's logs."""
    with _tee_lock:
        if not isinstance(sys.stdout, _ThreadLocalTee):
            sys.stdout = _ThreadLocalTee(sys.stdout)
        tee = sys.stdout
    tee._local.buf = []
    try:
        result = fn(*args, **kwargs)
        return result, "".join(tee._local.buf)
    finally:
        tee._local.buf = None


# Request threads. Sized for a single user with a few concurrent 30-40s LLM
# calls; the default executor would be ~5 threads on a small Modal container.
_request_pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="req")


def offload(endpoint: Callable) -> Callable:
    """Wrap an async route so its body runs on a worker thread.

    The handlers do blocking work (SQLite, HTTP fetches, LLM calls) inside
    `async def`. On one container that would stall every other request. The
    body is read on the server loop first, so `await request.form()` inside
    the handler parses the cached bytes and never touches the server's
    receive channel from the worker loop.

    Each request gets a fresh event loop that closes when the response
    returns, so a handler must not use asyncio.create_task for work meant to
    outlive the request. Use request.app.state.background.submit instead.
    """
    if not inspect.iscoroutinefunction(endpoint):
        return endpoint  # Starlette already runs sync endpoints in a threadpool.

    @functools.wraps(endpoint)
    async def wrapper(request):
        await request.body()
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()  # keeps the request-id log correlation
        return await loop.run_in_executor(_request_pool, ctx.run, asyncio.run, endpoint(request))

    return wrapper
