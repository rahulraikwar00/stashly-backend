"""Apply schema.sql on startup.

No Alembic: there is exactly one migration file, and a migration framework
would be a dependency with nothing to manage. `schema.sql` is written entirely
with IF NOT EXISTS, so re-running it is a no-op.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("db.migrate")

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def run_migrations(database_url: str | None, *, drop_channel_binding: bool = True) -> bool:
    """Apply the schema. Returns False when no database is configured."""
    from .pool import get_pool

    pool = get_pool(database_url, drop_channel_binding=drop_channel_binding)
    if pool is None:
        return False
    sql = SCHEMA_PATH.read_text()
    with pool.connection() as conn:
        conn.execute(sql)
        conn.commit()
    log.warning("Schema applied (idempotent)")
    return True
