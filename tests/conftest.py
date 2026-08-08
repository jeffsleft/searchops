"""
Shared test fixtures.

Root-causes a pre-existing test-suite isolation bug (see memory/lessons_learned.md,
"Pre-existing test-suite fragility surfaced by adding a new test file"): several
test files reassign `app.config.DATABASE_PATH` / `app.models.DATABASE_PATH`
directly, either at import time or inside a fixture. Since Python caches module
imports for the whole pytest process, those are the same two module-attribute
bindings for every test file — whichever file's assignment runs last (import
order during collection, or fixture order during execution) silently becomes
the DB every other file's `get_db()` calls hit next.

This autouse, module-scoped fixture gives every test module its own fresh,
isolated SQLite file immediately before that module's tests run — regardless of
what an individual file does internally (its own top-level assignment or
fixture becomes redundant but harmless, since this fixture's reset always runs
right before test execution for that module).
"""
import os
import tempfile

os.environ.setdefault("SESSION_SECRET", "0" * 64)
os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("DATABASE_PATH", tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)

import pytest

import app.config as _config
import app.models as _models


@pytest.fixture(autouse=True, scope="module")
def _isolated_test_db():
    """Point DATABASE_PATH at a fresh temp file for this test module only."""
    path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    os.environ["DATABASE_PATH"] = path
    _config.DATABASE_PATH = path
    _models.DATABASE_PATH = path
    _models.init_db()
    yield
    try:
        os.unlink(path)
    except OSError:
        pass
