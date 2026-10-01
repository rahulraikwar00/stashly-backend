"""Database-backed tests for the D-018 stores.

Run against a throwaway database (a Neon branch, or the docker container in
CI). Skipped entirely when `TEST_DATABASE_URL` is unset so the suite still runs
with no database configured.

    docker run -d --name d018test -e POSTGRES_PASSWORD=testpass \
      -e POSTGRES_DB=testdb -p 55432:5432 postgres:16-alpine
    TEST_DATABASE_URL=postgresql://postgres:testpass@localhost:55432/testdb \
      python -m pytest tests/test_pg_stores.py -q
"""

from __future__ import annotations

import pytest

from conftest import TEST_DATABASE_URL, requires_db

pytestmark = requires_db


# ── CodeStore ──────────────────────────────────────────────────────

def _directive(code: str, thread: str = "42"):
    from app.connectors.base import LinkDirective

    return LinkDirective(
        thread_key=thread, code=code, sender_id="7", username="tester"
    )


def test_register_bind_resolve_roundtrip(pool):
    from app.db.code_store import PgCodeStore

    store = PgCodeStore(pool, ttl_seconds=600)
    store.register("123456")
    assert store.resolve("123456") is None, "pending codes must not authenticate"

    assert store.attempt_bind(_directive("123456")) == "bound"
    account = store.resolve("123456")
    assert account is not None
    assert account.thread_id == "42"
    assert account.username == "tester"


def test_first_touch_wins(pool):
    from app.db.code_store import PgCodeStore

    store = PgCodeStore(pool, ttl_seconds=600)
    store.register("111111")
    store.register("222222")
    assert store.attempt_bind(_directive("111111", thread="A")) == "bound"
    assert store.attempt_bind(_directive("111111", thread="B")) == "already_linked"
    # B stays unbound: the first thread keeps the code.
    assert store.resolve("111111").thread_id == "A"
    assert store.attempt_bind(_directive("222222", thread="B")) == "bound"


def test_thread_takes_over_from_another_code(pool):
    from app.db.code_store import PgCodeStore

    store = PgCodeStore(pool, ttl_seconds=600)
    store.register("111111")
    store.register("222222")
    store.attempt_bind(_directive("111111", thread="A"))
    assert store.attempt_bind(_directive("222222", thread="A")) == "rebound"
    assert store.resolve("111111") is None, "the old code's binding is revoked"
    assert store.resolve("222222").thread_id == "A"


def test_unknown_and_expired_are_ignored(pool):
    from app.db.code_store import PgCodeStore

    store = PgCodeStore(pool, ttl_seconds=600)
    assert store.attempt_bind(_directive("999999")) == "unknown"

    expiring = PgCodeStore(pool, ttl_seconds=-1)
    expiring.register("888888")
    assert expiring.attempt_bind(_directive("888888")) == "expired"


def test_register_conflicts_on_a_linked_code(pool):
    from fastapi import HTTPException

    from app.db.code_store import PgCodeStore

    store = PgCodeStore(pool, ttl_seconds=600)
    store.register("123456")
    store.attempt_bind(_directive("123456"))
    with pytest.raises(HTTPException) as exc:
        store.register("123456")
    assert exc.value.status_code == 409


def test_clear_removes_the_thread_link_too(pool):
    from app.db.code_store import PgCodeStore

    store = PgCodeStore(pool, ttl_seconds=600)
    store.register("123456")
    store.attempt_bind(_directive("123456"))
    assert store.clear("123456") is True
    assert store.resolve("123456") is None
    with pool.connection() as conn:
        rows = conn.execute("SELECT count(*) AS n FROM thread_links").fetchone()
    assert rows["n"] == 0, "orphaned thread_links must not survive"


# ── SeenStore ──────────────────────────────────────────────────────

def test_seen_cursor_roundtrip_and_bound_threads(pool):
    from app.db.code_store import PgCodeStore
    from app.db.seen_store import PgSeenStore

    code_store = PgCodeStore(pool, ttl_seconds=600)
    code_store.register("123456")
    code_store.attempt_bind(_directive("123456", thread="42"))

    seen = PgSeenStore(pool)
    assert seen.get("42") is None
    seen.set("42", "msg-99")
    assert seen.get("42") == "msg-99"
    assert seen.bound_threads() == {"42": "msg-99"}
    seen.clear("42")
    assert seen.get("42") is None


def test_unlinked_threads_are_never_ingested(pool):
    """D-016: unregistered DMs are invisible."""
    from app.db.seen_store import PgSeenStore

    seen = PgSeenStore(pool)
    assert seen.bound_threads() == {}, "no links => nothing to ingest"


# ── Mailbox ────────────────────────────────────────────────────────

def test_mailbox_ingest_drain_cycle(pool):
    from app.db.mailbox import PgMailboxStore

    mailbox = PgMailboxStore(pool)
    inserted = mailbox.ingest(
        "42", [("m1", {"id": "m1", "timestamp": 1}), ("m2", {"id": "m2", "timestamp": 2})]
    )
    assert inserted == 2
    assert mailbox.depth("42") == 2

    drained = mailbox.drain("42")
    assert [row["id"] for row in drained] == ["m1", "m2"], "oldest first"
    assert mailbox.depth("42") == 0, "drain consumes"
    assert mailbox.drain("42") == [], "a second drain returns nothing"


def test_mailbox_drain_returns_oldest_first_by_timestamp(pool):
    from app.db.mailbox import PgMailboxStore

    mailbox = PgMailboxStore(pool)
    mailbox.ingest("42", [("c", {"id": "c", "timestamp": 300}),
                          ("a", {"id": "a", "timestamp": 100}),
                          ("b", {"id": "b", "timestamp": 200})])
    assert [r["id"] for r in mailbox.drain("42")] == ["a", "b", "c"]


def test_mailbox_ingest_is_idempotent(pool):
    from app.db.mailbox import PgMailboxStore

    mailbox = PgMailboxStore(pool)
    rows = [("m1", {"id": "m1", "timestamp": 1})]
    assert mailbox.ingest("42", rows) == 1
    assert mailbox.ingest("42", rows) == 0, "replay must not duplicate"
    assert mailbox.depth("42") == 1


def test_mailbox_threads_are_isolated(pool):
    from app.db.mailbox import PgMailboxStore

    mailbox = PgMailboxStore(pool)
    mailbox.ingest("A", [("m1", {"id": "m1", "timestamp": 1})])
    mailbox.ingest("B", [("m2", {"id": "m2", "timestamp": 1})])

    # Draining A must not consume B. This is the isolation guarantee: one user
    # polling must never receive another user's bookmarks.
    assert [r["id"] for r in mailbox.drain("A")] == ["m1"]
    assert mailbox.depth("B") == 1, "A's drain must not touch B"
    assert [r["id"] for r in mailbox.drain("B")] == ["m2"]
    assert mailbox.depth("B") == 0


def test_mailbox_drain_respects_the_limit(pool):
    from app.db.mailbox import PgMailboxStore

    mailbox = PgMailboxStore(pool)
    mailbox.ingest("42", [(f"m{i}", {"id": f"m{i}", "timestamp": i}) for i in range(10)])
    assert len(mailbox.drain("42", limit=4)) == 4
    assert mailbox.depth("42") == 6, "the remainder stays buffered"


def test_mailbox_prune_caps_the_oldest_rows(pool):
    from app.db.mailbox import PgMailboxStore

    mailbox = PgMailboxStore(pool, cap_per_thread=3)
    mailbox.ingest("42", [(f"m{i}", {"id": f"m{i}", "timestamp": i}) for i in range(10)])
    mailbox.prune()
    remaining = mailbox.peek("42")
    assert len(remaining) == 3
    assert [r["id"] for r in remaining] == ["m7", "m8", "m9"], "newest kept"


def test_mailbox_clear_is_per_thread(pool):
    from app.db.mailbox import PgMailboxStore

    mailbox = PgMailboxStore(pool)
    mailbox.ingest("A", [("m1", {"id": "m1", "timestamp": 1})])
    mailbox.ingest("B", [("m2", {"id": "m2", "timestamp": 1})])
    assert mailbox.clear("A") == 1
    assert mailbox.depth("A") == 0
    assert mailbox.depth("B") == 1


# ── Auth throttling ────────────────────────────────────────────────

def test_code_locks_after_max_failures(pool):
    from app.db.failed_auth import PgFailedAuthStore

    auth = PgFailedAuthStore(pool)
    assert not auth.is_locked("123456")
    for i in range(4):
        assert auth.record_failure("123456", max_failures=5, lock_seconds=900) is False
    assert auth.record_failure("123456", max_failures=5, lock_seconds=900) is True
    assert auth.is_locked("123456")


def test_success_clears_the_failure_history(pool):
    from app.db.failed_auth import PgFailedAuthStore

    auth = PgFailedAuthStore(pool)
    auth.record_failure("123456", 5, 900)
    auth.clear("123456")
    assert auth.record_failure("123456", 1, 900) is True


def test_locking_is_per_code_not_global(pool):
    from app.db.failed_auth import PgFailedAuthStore

    auth = PgFailedAuthStore(pool)
    for _ in range(5):
        auth.record_failure("111111", 5, 900)
    assert auth.is_locked("111111")
    assert not auth.is_locked("222222"), "one code's lock must not affect another"


# ── Secrets ────────────────────────────────────────────────────────

def test_secret_roundtrip(pool):
    from app.crypto import load_key
    from app.db.secrets import PgSecretStore

    store = PgSecretStore(pool, load_key("a" * 64))
    assert store.set("instagram", b"session-bytes") is True
    assert store.get("instagram") == b"session-bytes"


def test_secret_is_ciphertext_on_disk(pool):
    from app.crypto import load_key
    from app.db.secrets import PgSecretStore

    store = PgSecretStore(pool, load_key("a" * 64))
    store.set("instagram", b"sessionid=abc123")
    with pool.connection() as conn:
        raw = conn.execute(
            "SELECT ciphertext FROM app_secrets WHERE name = 'instagram'"
        ).fetchone()["ciphertext"]
    assert b"abc123" not in bytes(raw), "plaintext must never reach the column"


def test_secret_store_without_a_key_refuses_to_persist(pool):
    from app.db.secrets import PgSecretStore

    store = PgSecretStore(pool, None)
    assert store.persistence_allowed is False
    assert store.set("instagram", b"session") is False
    assert store.get("instagram") is None


def test_secret_under_a_different_key_reads_as_absent(pool):
    from app.crypto import load_key
    from app.db.secrets import PgSecretStore

    PgSecretStore(pool, load_key("a" * 64)).set("instagram", b"session")
    rotated = PgSecretStore(pool, load_key("b" * 64))
    assert rotated.get("instagram") is None, "rotation forces re-auth, not a crash"


# ── Metadata cache ─────────────────────────────────────────────────

def test_metadata_cache_roundtrip_and_ttl(pool):
    from app.db.metadata_cache import PgMetadataCache

    cache = PgMetadataCache(pool, ttl_days=7)
    assert cache.get("abc123") is None
    cache.put("abc123", "https://example.com/a", {"title": "Hello"})
    assert cache.get("abc123") == {"title": "Hello"}


def test_metadata_cache_overwrites_on_put(pool):
    from app.db.metadata_cache import PgMetadataCache

    cache = PgMetadataCache(pool, ttl_days=7)
    cache.put("abc123", "https://example.com/a", {"title": "Old"})
    cache.put("abc123", "https://example.com/a", {"title": "New"})
    assert cache.get("abc123") == {"title": "New"}


def test_metadata_cache_prune_drops_expired(pool):
    from app.db.metadata_cache import PgMetadataCache

    cache = PgMetadataCache(pool, ttl_days=7)
    cache.put("abc123", "https://example.com/a", {"title": "Hello"})
    with pool.connection() as conn:
        conn.execute("UPDATE metadata_cache SET expires_at = 1")
        conn.commit()
    assert cache.get("abc123") is None
    assert cache.prune_expired() == 1
    with pool.connection() as conn:
        n = conn.execute("SELECT count(*) AS n FROM metadata_cache").fetchone()["n"]
    assert n == 0


# ── DSN handling ───────────────────────────────────────────────────

def test_channel_binding_is_stripped_by_default():
    from app.db.pool import normalize_dsn

    dsn = "postgresql://u:p@h-db/db?sslmode=require&channel_binding=require"
    out = normalize_dsn(dsn)
    assert "channel_binding" not in out
    assert "sslmode=require" in out, "TLS must survive"


def test_channel_binding_kept_when_requested():
    from app.db.pool import normalize_dsn

    dsn = "postgresql://u:p@h-db/db?sslmode=require&channel_binding=require"
    assert "channel_binding=require" in normalize_dsn(dsn, drop_channel_binding=False)


# ── health / schema verification ───────────────────────────────────
#
# Regression: `SELECT 1` succeeds against ANY Postgres, so the original health
# check reported "database: ok" against a completely empty schema, while link
# codes, cursors and the mailbox were being persisted nowhere.

def test_db_status_ok_when_schema_is_applied(pool):
    from app.db.pool import db_status

    state = db_status(TEST_DATABASE_URL)
    assert state["reachable"] is True
    assert state["schema"] == "ok"
    assert state["missing"] == []


def test_db_status_detects_a_missing_table(pool):
    from app.db.pool import db_status

    with pool.connection() as conn:
        conn.execute("DROP TABLE metadata_cache")
        conn.commit()
    try:
        state = db_status(TEST_DATABASE_URL)
        assert state["reachable"] is True, "the database still answers"
        assert state["schema"] == "missing"
        assert state["missing"] == ["metadata_cache"]
    finally:
        from app.db.migrate import run_migrations

        run_migrations(TEST_DATABASE_URL)


def test_db_status_reports_not_configured_without_a_url():
    from app.db.pool import db_status

    state = db_status(None)
    assert state["reachable"] is False
    assert state["schema"] == "not-configured"


def test_required_tables_covers_the_relays_whole_state():
    from app.db.pool import REQUIRED_TABLES

    # Every table the relay reads or writes must be checked, or a partial
    # migration slips through as "healthy".
    assert set(REQUIRED_TABLES) == {
        "link_codes",
        "thread_links",
        "seen_cursors",
        "pending_items",
        "metadata_cache",
        "app_secrets",
        "failed_auth",
    }


def test_health_endpoint_stays_200_when_degraded(pool):
    """A restart means a fresh Instagram login, so a DB blip must not 5xx.

    Render restarts an instance after 60s of failed health checks, and the
    restart path re-authenticates to Instagram. That is the ban risk, so
    `/health` reports degradation in the body rather than via status code.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import health as health_router

    with pool.connection() as conn:
        conn.execute("DROP TABLE pending_items")
        conn.commit()
    try:
        app = FastAPI()
        app.state.settings = type(
            "S", (), {"postgres_configured": True, "database_url": TEST_DATABASE_URL,
                      "drop_channel_binding": True}
        )()
        app.include_router(health_router.router)
        client = TestClient(app)

        body = client.get("/health")
        assert body.status_code == 200
        assert body.json()["status"] == "degraded"
        assert body.json()["schema"] == "missing"
        assert "pending_items" in body.json()["missing_tables"]

        strict = client.get("/health?strict=1")
        assert strict.status_code == 503, "strict=1 is the opt-in hard gate"
    finally:
        from app.db.migrate import run_migrations

        run_migrations(TEST_DATABASE_URL)