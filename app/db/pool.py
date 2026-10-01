"""Postgres access: connection pool + idempotent schema migration.

Everything here is a no-op unless `DATABASE_URL` is set, so the JSON-file path
stays the default for local development (D-018).
"""

from __future__ import annotations

import logging
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger("db")

_pool = None
_pool_lock = threading.Lock()


def normalize_dsn(dsn: str, drop_channel_binding: bool = True) -> str:
    """Return `dsn` with query params cleaned up for PgBouncer compatibility.

    Neon appends `channel_binding=require` to its connection strings. SCRAM
    channel binding negotiated through the transaction-mode pooler is not
    reliably supported, and when it does fail it fails at the auth stage with an
    error that says nothing useful. Dropping it is the first thing to try and
    costs nothing over TLS, so we do it by default.
    """
    parts = urlsplit(dsn)
    params = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (drop_channel_binding and k == "channel_binding")
    ]
    return urlunsplit(parts._replace(query=urlencode(params)))


def get_pool(database_url: str | None, *, drop_channel_binding: bool = True):
    """Return the process-wide connection pool, creating it on first use.

    Returns None when no `database_url` is configured, which is how the app
    selects the JSON-file stores instead.
    """
    global _pool
    if not database_url:
        return None
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        dsn = normalize_dsn(database_url, drop_channel_binding)
        pool = ConnectionPool(
            dsn,
            min_size=1,
            # Neon's smallest compute allows far more, but the relay only ever
            # has the poller plus a handful of app requests in flight.
            max_size=5,
            open=True,
            timeout=10,
            # Neon suspends after 5 idle minutes; a short recycle avoids
            # handing a dead socket to the first request after a quiet period.
            max_idle=240,
            # Every store reads rows by column name.
            kwargs={"row_factory": dict_row},
        )
        pool.wait(timeout=15)
        _pool = pool
        log.warning("Postgres pool opened (max_size=5)")
        return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None
            log.warning("Postgres pool closed")


# Tables the relay cannot function without. A database that answers but is
# missing these is reachable and useless — `SELECT 1` cannot tell the
# difference, which is how a service once reported "database: ok" against a
# completely empty schema.
REQUIRED_TABLES = (
    "link_codes",
    "thread_links",
    "seen_cursors",
    "pending_items",
    "metadata_cache",
    "app_secrets",
    "failed_auth",
)


def db_status(database_url: str | None, *, drop_channel_binding: bool = True) -> dict:
    """Report reachability AND whether the schema is actually there.

    `SELECT 1` alone is not a health check for this service: it succeeds against
    any Postgres, including one that has never had the schema applied. Link
    codes, cursors and the buffered mailbox all live in those tables, so a
    missing schema means state is silently not being persisted.

    Returns `{"reachable", "schema", "missing"}` where `schema` is one of
    "ok" | "missing" | "unknown" | "not-configured".
    """
    pool = get_pool(database_url, drop_channel_binding=drop_channel_binding)
    if pool is None:
        return {"reachable": False, "schema": "not-configured", "missing": []}

    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
    except Exception:
        log.exception("database unreachable")
        return {"reachable": False, "schema": "unknown", "missing": []}

    try:
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'public'"
            ).fetchall()
    except Exception:
        log.exception("could not read schema state")
        return {"reachable": True, "schema": "unknown", "missing": []}

    present = {r["table_name"] for r in rows}
    missing = [t for t in REQUIRED_TABLES if t not in present]
    return {
        "reachable": True,
        "schema": "missing" if missing else "ok",
        "missing": missing,
    }
