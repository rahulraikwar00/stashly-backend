"""Brute-force throttling for the 6-digit link code (D-018).

`POST /auth/codes` is unauthenticated by design, and `require_code` accepts any
*linked* 6-digit code. The code space is 10^6, so without throttling an attacker
can walk it against indexed lookups and read another user's bookmark stream.

Locked per CODE, not per IP: the attack is "try many codes", and keying on IP
would punish unrelated users behind shared NAT or a carrier while doing nothing
about a distributed attacker with many IPs.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("db.failed_auth")


def _now_ms() -> int:
    return int(time.time() * 1000)


class PgFailedAuthStore:
    def __init__(self, pool) -> None:
        self._pool = pool

    def is_locked(self, code: str) -> bool:
        if not code:
            return False
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT locked_until FROM failed_auth WHERE code = %s", (code,)
            ).fetchone()
        if not row or row["locked_until"] is None:
            return False
        return row["locked_until"] > _now_ms()

    def record_failure(
        self,
        code: str,
        max_failures: int = 5,
        lock_seconds: int = 900,
    ) -> bool:
        """Count one failure. Returns True if this call locked the code."""
        if not code:
            return False
        now = _now_ms()
        lock_until = now + lock_seconds * 1000
        with self._pool.connection() as conn:
            row = conn.execute(
                # The INSERT branch has to apply the threshold too, not just the
                # DO UPDATE branch: after a successful auth clears the row, the
                # very next failure re-enters through INSERT.
                """
                INSERT INTO failed_auth (code, failures, locked_until)
                VALUES (%s, 1, CASE WHEN 1 >= %s THEN %s ELSE NULL END)
                ON CONFLICT (code) DO UPDATE SET
                    failures = failed_auth.failures + 1,
                    locked_until = CASE
                        WHEN failed_auth.failures + 1 >= %s THEN %s
                        ELSE failed_auth.locked_until
                    END
                RETURNING failures, locked_until
                """,
                (code, max_failures, lock_until, max_failures, lock_until),
            ).fetchone()
            conn.commit()
        locked = bool(row and row["locked_until"] and row["locked_until"] > now)
        if locked:
            log.warning("code throttled after %s failures", row["failures"])
        return locked

    def clear(self, code: str) -> None:
        """A successful authentication wipes the failure history."""
        if not code:
            return
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM failed_auth WHERE code = %s", (code,))
            conn.commit()

    def prune_expired(self) -> int:
        with self._pool.connection() as conn:
            result = conn.execute(
                "DELETE FROM failed_auth WHERE locked_until IS NOT NULL"
                " AND locked_until < %s",
                (_now_ms(),),
            )
            conn.commit()
            return max(0, result.rowcount)