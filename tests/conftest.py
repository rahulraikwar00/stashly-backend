"""Shared test fixtures.

Database-backed tests run against a throwaway Postgres — a Neon `test` branch in
CI, or the docker container documented in tests/test_pg_stores.py. Everything
database-backed skips when `TEST_DATABASE_URL` is unset, so the suite still runs
with no database configured.
"""

from __future__ import annotations

import os

import pytest

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set"
)

# Order matters only for readability: CASCADE handles the FK from thread_links
# to link_codes.
_TABLES = (
    "pending_items, thread_links, link_codes, seen_cursors,"
    " failed_auth, app_secrets, metadata_cache"
)


def truncate_all(pool) -> None:
    with pool.connection() as conn:
        conn.execute(f"TRUNCATE TABLE {_TABLES} CASCADE")
        conn.commit()


@pytest.fixture(scope="module")
def pool():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")

    from app.db.migrate import run_migrations
    from app.db.pool import close_pool, get_pool

    run_migrations(TEST_DATABASE_URL)
    p = get_pool(TEST_DATABASE_URL)
    yield p
    close_pool()


@pytest.fixture(autouse=True)
def clean():
    """Reset the tables around every test that uses them.

    Autouse but deliberately NOT dependent on the `pool` fixture: an autouse
    fixture that skipped would skip the whole module, taking the JSON-store
    tests down with it when no database is configured. This resolves the pool
    itself and no-ops when `TEST_DATABASE_URL` is unset.
    """
    if not TEST_DATABASE_URL:
        yield
        return

    from app.db.migrate import run_migrations
    from app.db.pool import get_pool

    run_migrations(TEST_DATABASE_URL)
    truncate_all(get_pool(TEST_DATABASE_URL))
    yield