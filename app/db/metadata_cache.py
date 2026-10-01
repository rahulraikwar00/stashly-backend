"""/metadata read-through cache.

The og:-tag extraction is the highest-cardinality, slowest, most repeated
operation in the service and the only endpoint that could see a traffic spike,
so it is the one thing worth caching. Keyed by the app's djb2 `urlHash` so a
cache hit costs a single primary-key lookup.

Deliberately NOT cached: link-code resolution (already one indexed lookup, and
an in-memory map would break on restart and on a second instance) and the
mailbox (which is already the hot set). No Redis — Render's free Key Value is
25 MB and non-persistent, which is a cache that vanishes while appearing to help.
"""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("db.metadata_cache")


def _now_ms() -> int:
    return int(time.time() * 1000)


class PgMetadataCache:
    def __init__(self, pool, ttl_days: int = 7) -> None:
        self._pool = pool
        self._ttl_ms = ttl_days * 24 * 60 * 60 * 1000

    def get(self, url_hash: str) -> dict | None:
        """Cached payload, or None when absent or expired."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT payload, expires_at FROM metadata_cache WHERE url_hash = %s",
                (url_hash,),
            ).fetchone()
        if not row:
            return None
        if row["expires_at"] is not None and row["expires_at"] < _now_ms():
            return None
        payload = row["payload"]
        return json.loads(payload) if isinstance(payload, str) else payload

    def put(self, url_hash: str, url: str, payload: dict) -> None:
        now = _now_ms()
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO metadata_cache
                    (url_hash, url, payload, fetched_at, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (url_hash) DO UPDATE SET
                    url        = EXCLUDED.url,
                    payload    = EXCLUDED.payload,
                    fetched_at = EXCLUDED.fetched_at,
                    expires_at = EXCLUDED.expires_at
                """,
                (url_hash, url, json.dumps(payload), now, now + self._ttl_ms),
            )
            conn.commit()

    def prune_expired(self) -> int:
        with self._pool.connection() as conn:
            result = conn.execute(
                "DELETE FROM metadata_cache WHERE expires_at < %s", (_now_ms(),)
            )
            conn.commit()
            return max(0, result.rowcount)