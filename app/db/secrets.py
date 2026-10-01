"""Postgres-backed secret storage: the D-018 replacement for session.json.

The Instagram session is encrypted before it reaches this table. When no
`IG_SESSION_KEY` is configured the caller must keep the value in memory instead
of calling `set` — see `app/connectors/instagram.py`.
"""

from __future__ import annotations

import logging
import time

from cryptography.exceptions import InvalidTag

log = logging.getLogger("db.secrets")


def _now_ms() -> int:
    return int(time.time() * 1000)


class PgSecretStore:
    """AES-256-GCM envelope encryption over `app_secrets`."""

    def __init__(self, pool, key: bytes | None) -> None:
        self._pool = pool
        self._key = key

    @property
    def persistence_allowed(self) -> bool:
        """False when no key is configured, which forbids writing anything."""
        return self._key is not None

    def get(self, name: str) -> bytes | None:
        """Decrypted bytes, or None when absent."""
        if self._key is None:
            return None
        from ..crypto import decrypt

        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT ciphertext FROM app_secrets WHERE name = %s", (name,)
            ).fetchone()
        if not row:
            return None
        try:
            return decrypt(self._key, bytes(row["ciphertext"]), aad=name.encode())
        except InvalidTag:
            # Wrong key, or the row was written under a rotated key. Either way
            # the caller re-authenticates; do not log either value.
            log.warning("secret %r failed to decrypt; re-authenticating", name)
            return None

    def set(self, name: str, plaintext: bytes) -> bool:
        """Encrypt and store. Returns False when persistence is not allowed."""
        if self._key is None:
            return False
        from ..crypto import encrypt

        blob = encrypt(self._key, plaintext, aad=name.encode())
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO app_secrets (name, ciphertext, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    ciphertext = EXCLUDED.ciphertext,
                    updated_at = EXCLUDED.updated_at
                """,
                (name, blob, _now_ms()),
            )
            conn.commit()
        return True

    def delete(self, name: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM app_secrets WHERE name = %s", (name,))
            conn.commit()