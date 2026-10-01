"""The buffered mailbox: `pending_items` is the queue (D-018).

No broker. One producer (the poller), one consumer (the Expo app), and the
throughput ceiling is Instagram's per-account rate limit — a message broker
would add a service and retry semantics for no throughput gain.

Draining returns the rows and marks them delivered in a single statement, so a
concurrent or retried request cannot double-deliver:

    DELETE FROM pending_items WHERE id IN (
        SELECT id FROM pending_items WHERE thread_id = $1
        ORDER BY id LIMIT $2 FOR UPDATE SKIP LOCKED
    ) RETURNING payload

The relay is not a bookmark store. The Expo app is the system of record and
Instagram remains the upstream source, so pruning a row is not data loss — it is
what keeps Neon inside its 0.5 GB ceiling.
"""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("db.mailbox")


def _now_ms() -> int:
    return int(time.time() * 1000)


class PgMailboxStore:
    def __init__(self, pool, cap_per_thread: int = 500) -> None:
        self._pool = pool
        self._cap = cap_per_thread

    # ── producer ───────────────────────────────────────────────────

    def ingest(
        self,
        thread_id: str,
        rows: list[tuple[str, object]],
    ) -> int:
        """Buffer `(source_msg, payload)` pairs. Returns how many were new.

        `payload` is a `BookmarkResponse` (or anything jsonb-serializable).
        `ON CONFLICT DO NOTHING` makes this idempotent, which is what lets the
        poller safely re-run after a crash mid-cycle: the cursor only advances
        after a successful insert, so a partial cycle is simply redone.
        """
        if not rows:
            return 0
        now = _now_ms()
        payloads = [(thread_id, src, json.dumps(p), now) for src, p in rows]
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO pending_items (thread_id, source_msg, payload, created_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (thread_id, source_msg) DO NOTHING
                    """,
                    payloads,
                )
                inserted = cur.rowcount
            conn.commit()
        return inserted if inserted and inserted > 0 else 0

    # ── consumer ───────────────────────────────────────────────────

    def drain(self, thread_id: str, limit: int = 200) -> list[dict]:
        """Deliver up to `limit` buffered rows, oldest first, consuming them."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                DELETE FROM pending_items WHERE id IN (
                    SELECT id FROM pending_items
                    WHERE thread_id = %s
                    ORDER BY id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING payload
                """,
                (thread_id, limit),
            ).fetchall()
            conn.commit()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(json.loads(payload) if isinstance(payload, str) else payload)
        out.sort(key=lambda p: p.get("timestamp") or 0)
        return out

    def peek(self, thread_id: str, limit: int = 200) -> list[dict]:
        """Buffered rows WITHOUT consuming. For diagnostics only."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM pending_items WHERE thread_id = %s"
                " ORDER BY id LIMIT %s",
                (thread_id, limit),
            ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(json.loads(payload) if isinstance(payload, str) else payload)
        return out

    # ── maintenance ────────────────────────────────────────────────

    def clear(self, thread_id: str) -> int:
        """Drop a thread's whole mailbox. Used by POST /debug/reset.

        This must be cleared alongside the seen cursor; leaving buffered rows
        behind would make a reset thread re-ingest everything it already
        received.
        """
        with self._pool.connection() as conn:
            result = conn.execute(
                "DELETE FROM pending_items WHERE thread_id = %s", (thread_id,)
            )
            conn.commit()
            return result.rowcount

    def prune(self, cap_per_thread: int | None = None) -> int:
        """Trim each thread to the newest `cap` rows. Returns rows dropped."""
        cap = cap_per_thread or self._cap
        with self._pool.connection() as conn:
            result = conn.execute(
                """
                DELETE FROM pending_items
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id, row_number() OVER (
                            PARTITION BY thread_id ORDER BY id DESC
                        ) AS rn
                        FROM pending_items
                    ) ranked
                    WHERE ranked.rn > %s
                )
                """,
                (cap,),
            )
            conn.commit()
            dropped = result.rowcount
        if dropped and dropped > 0:
            log.warning("mailbox prune dropped %d row(s) over cap=%d", dropped, cap)
        return max(0, dropped)

    def depth(self, thread_id: str) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM pending_items WHERE thread_id = %s",
                (thread_id,),
            ).fetchone()
        return int(row["n"])

    def prune_expired_codes(self) -> int:
        """Remove pending link codes whose TTL has passed."""
        with self._pool.connection() as conn:
            result = conn.execute(
                "DELETE FROM link_codes WHERE status = 'pending'"
                " AND expires_at IS NOT NULL AND expires_at < %s",
                (_now_ms(),),
            )
            conn.commit()
            return max(0, result.rowcount)