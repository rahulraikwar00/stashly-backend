"""Postgres-backed CodeStore: the D-018 replacement for links.json.

`attempt_bind` is a real transaction with `SELECT ... FOR UPDATE`, so
first-touch-wins is enforced by the database rather than by a process-local
lock. That matters on Render because a container restart can otherwise land
mid-bind.
"""

from __future__ import annotations

import logging
import time


from ..auth import LinkedAccount
from ..connectors.base import LinkDirective

log = logging.getLogger("db.code_store")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_expired_pending(row: dict) -> bool:
    return (
        row.get("status") == "pending"
        and row.get("expires_at") is not None
        and _now_ms() > row["expires_at"]
    )


class PgCodeStore:
    """Same surface as `app.auth.CodeStore`, backed by `link_codes`/`thread_links`."""

    def __init__(self, pool, ttl_seconds: int = 600) -> None:
        self._pool = pool
        self._ttl = ttl_seconds
        # Set by the poller so a fresh registration interrupts its idle wait
        # instead of being missed until the next long sleep.
        self.on_register = None

    # ── persistence ────────────────────────────────────────────────

    def _prune(self, conn) -> None:
        conn.execute(
            "DELETE FROM link_codes WHERE status = 'pending' AND expires_at IS NOT NULL"
            " AND expires_at < %s",
            (_now_ms(),),
        )

    # ── code lifecycle ─────────────────────────────────────────────

    def register(self, code: str) -> dict:
        from fastapi import HTTPException

        now = _now_ms()
        with self._pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (code,))
            self._prune(conn)
            existing = conn.execute(
                "SELECT status FROM link_codes WHERE code = %s", (code,)
            ).fetchone()
            if existing and existing["status"] == "linked":
                raise HTTPException(status_code=409, detail="Code is already linked.")
            conn.execute(
                """
                INSERT INTO link_codes
                    (code, status, thread_id, sender_id, username,
                     created_at, expires_at, linked_at)
                VALUES (%s, 'pending', '', '', '', %s, %s, NULL)
                ON CONFLICT (code) DO UPDATE SET
                    status     = 'pending',
                    thread_id  = '',
                    sender_id  = '',
                    username   = '',
                    created_at = EXCLUDED.created_at,
                    expires_at = EXCLUDED.expires_at,
                    linked_at  = NULL
                """,
                (code, now, now + self._ttl * 1000),
            )
            conn.commit()
        if self.on_register is not None:
            self.on_register()
        return {"code": code, "status": "pending", "expiresAt": now + self._ttl * 1000}

    def has_pending(self) -> bool:
        """True when a live pending code is waiting for its `/link` DM.

        Expiry-aware so an abandoned code does not keep the poller at its fast
        cadence: this is the cheap indexed `link_codes_pending_expiry` probe
        rather than a full scan, and the poller calls it every cycle.
        """
        with self._pool.connection() as conn:
            self._prune(conn)
            return bool(
                conn.execute(
                    "SELECT 1 FROM link_codes WHERE status = 'pending' LIMIT 1"
                ).fetchone()
            )

    def attempt_bind(self, directive: LinkDirective) -> str:
        """Bind a `/link <code>` directive. Never raises.

        Returns one of: "bound" | "rebound" | "unknown" | "expired" |
        "already_linked". A thread that DMs a fresh pending code while already
        bound to a different code takes the thread over ("rebound"); the old
        code's binding is revoked. First-touch-wins still applies between
        unbound threads competing for the same pending code.
        """
        code = directive.code
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    # Serialize competing binders for this code.
                    conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))", (code,)
                    )
                    entry = conn.execute(
                        "SELECT status, expires_at FROM link_codes WHERE code = %s"
                        " FOR UPDATE",
                        (code,),
                    ).fetchone()
                    if entry is None:
                        return "unknown"
                    if _is_expired_pending(entry):
                        return "expired"
                    if entry["status"] == "linked":
                        return "already_linked"

                    bound = conn.execute(
                        "SELECT code FROM thread_links WHERE thread_id = %s"
                        " FOR UPDATE",
                        (directive.thread_key,),
                    ).fetchone()
                    bound_code = bound["code"] if bound else None

                    if bound_code and bound_code != code:
                        conn.execute(
                            """
                            UPDATE link_codes SET
                                status = 'unlinked', thread_id = '', sender_id = '',
                                username = '', linked_at = NULL, expires_at = NULL
                            WHERE code = %s
                            """,
                            (bound_code,),
                        )

                    now = _now_ms()
                    conn.execute(
                        """
                        UPDATE link_codes SET
                            status = 'linked', thread_id = %s, sender_id = %s,
                            username = %s, linked_at = %s, expires_at = NULL
                        WHERE code = %s
                        """,
                        (
                            directive.thread_key,
                            directive.sender_id,
                            directive.username,
                            now,
                            code,
                        ),
                    )
                    # The UNIQUE primary key on thread_id is a second, harder
                    # guarantee that one thread maps to exactly one code.
                    conn.execute(
                        """
                        INSERT INTO thread_links (thread_id, code) VALUES (%s, %s)
                        ON CONFLICT (thread_id) DO UPDATE SET code = EXCLUDED.code
                        """,
                        (directive.thread_key, code),
                    )
            return "rebound" if bound_code and bound_code != code else "bound"
        except Exception:
            # The JSON store never raises out of attempt_bind; the poller treats
            # a raised exception as "ignored" upstream, so keep that contract.
            log.exception("pg attempt_bind failed for code=%s", code)
            return "unknown"

    def resolve(self, code: str) -> LinkedAccount | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT code, thread_id, sender_id, username
                FROM link_codes
                WHERE code = %s AND status = 'linked' AND thread_id <> ''
                """,
                (code,),
            ).fetchone()
        if not row:
            return None
        return LinkedAccount(
            code=row["code"],
            thread_id=row["thread_id"],
            sender_id=row["sender_id"] or "",
            username=row["username"] or "",
        )

    def status(self, code: str) -> dict | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT status, thread_id, username, expires_at FROM link_codes"
                " WHERE code = %s",
                (code,),
            ).fetchone()
        if not row:
            return None
        if _is_expired_pending(row):
            return {"code": code, "status": "expired", "linked": False}
        linked = row["status"] == "linked"
        return {
            "code": code,
            "status": row["status"],
            "linked": linked,
            "username": row["username"] or "",
            "threadId": row["thread_id"] or "",
            "expiresAt": row["expires_at"],
        }

    def clear(self, code: str) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT thread_id FROM link_codes WHERE code = %s", (code,)
            ).fetchone()
            if not row:
                return False
            if row["thread_id"]:
                conn.execute(
                    "DELETE FROM thread_links WHERE thread_id = %s", (row["thread_id"],)
                )
            conn.execute("DELETE FROM link_codes WHERE code = %s", (code,))
            conn.commit()
        return True
