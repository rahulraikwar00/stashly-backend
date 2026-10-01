"""Postgres-backed SeenStore: the D-018 replacement for seen_messages.json."""

from __future__ import annotations

import logging
import time

log = logging.getLogger("db.seen_store")


def _now_ms() -> int:
    return int(time.time() * 1000)


class PgSeenStore:
    """Same surface as `app.state.SeenStore`, backed by `seen_cursors`."""

    def __init__(self, pool) -> None:
        self._pool = pool

    def get(self, thread_key: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT cursor FROM seen_cursors WHERE thread_id = %s", (thread_key,)
            ).fetchone()
        return row["cursor"] if row else None

    def set(self, thread_key: str, cursor: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO seen_cursors (thread_id, cursor, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (thread_id) DO UPDATE SET
                    cursor = EXCLUDED.cursor, updated_at = EXCLUDED.updated_at
                """,
                (thread_key, cursor, _now_ms()),
            )
            conn.commit()

    def clear(self, thread_key: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM seen_cursors WHERE thread_id = %s", (thread_key,)
            )
            conn.commit()

    def all_cursors(self) -> dict[str, str]:
        """Every thread's cursor, for the poller's batch ingest."""
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT thread_id, cursor FROM seen_cursors").fetchall()
        return {r["thread_id"]: r["cursor"] for r in rows}

    def bound_threads(self) -> dict[str, str | None]:
        """`{thread_id: cursor}` for every currently linked thread.

        The poller ingests exactly these threads, so an unlinked thread's DMs are
        never buffered, persisted, or relayed (D-016's "unregistered DMs are
        invisible" rule).
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT l.thread_id, s.cursor FROM thread_links l"
                " LEFT JOIN seen_cursors s ON s.thread_id = l.thread_id"
            ).fetchall()
        return {r["thread_id"]: r["cursor"] for r in rows}