"""One-off: migrate links.json + seen_messages.json into Postgres (D-018).

Run once, locally, before pointing Render at the database — otherwise every
already-linked user has to re-link (the pending-code TTL is only 10 minutes, so
a forgotten migration during onboarding is awkward to recover from).

    DATABASE_URL=postgresql://... .venv/bin/python -m scripts.seed_from_json

Idempotent: re-running overwrites with the same values and never duplicates.
The Instagram session is deliberately NOT migrated — paste it back by logging in
once, which will re-encrypt it under your `IG_SESSION_KEY`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.db.migrate import run_migrations  # noqa: E402
from app.db.pool import get_pool  # noqa: E402


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        print(f"  (no {path.name})")
        return {}


def main() -> int:
    settings = load_settings()
    if not settings.postgres_configured:
        print("DATABASE_URL is not set. Nothing to do.")
        return 1

    run_migrations(
        settings.database_url,
        drop_channel_binding=settings.drop_channel_binding,
    )
    pool = get_pool(settings.database_url)

    links = _load(settings.links_file)
    seen = _load(settings.seen_file)
    codes = links.get("codes", {})
    threads = links.get("threads", {})

    if not codes and not seen:
        print("No local state found — nothing to migrate.")
        return 0

    with pool.connection() as conn:
        with conn.transaction():
            for code, entry in codes.items():
                conn.execute(
                    """
                    INSERT INTO link_codes
                        (code, status, thread_id, sender_id, username,
                         created_at, expires_at, linked_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (code) DO UPDATE SET
                        status=EXCLUDED.status, thread_id=EXCLUDED.thread_id,
                        sender_id=EXCLUDED.sender_id,
                        username=EXCLUDED.username,
                        created_at=EXCLUDED.created_at,
                        expires_at=EXCLUDED.expires_at,
                        linked_at=EXCLUDED.linked_at
                    """,
                    (
                        code,
                        entry.get("status", "pending"),
                        entry.get("threadId", "") or "",
                        entry.get("senderId", "") or "",
                        entry.get("username", "") or "",
                        entry.get("createdAt", 0),
                        entry.get("expiresAt"),
                        entry.get("linkedAt"),
                    ),
                )
            for thread_id, code in threads.items():
                # Only migrate thread links whose code actually exists.
                if code in codes:
                    conn.execute(
                        """
                        INSERT INTO thread_links (thread_id, code) VALUES (%s, %s)
                        ON CONFLICT (thread_id) DO UPDATE SET code = EXCLUDED.code
                        """,
                        (str(thread_id), code),
                    )
            for thread_id, cursor in seen.items():
                conn.execute(
                    """
                    INSERT INTO seen_cursors (thread_id, cursor, updated_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (thread_id) DO UPDATE SET
                        cursor=EXCLUDED.cursor, updated_at=EXCLUDED.updated_at
                    """,
                    (str(thread_id), cursor, 0),
                )

    print(f"Migrated {len(codes)} code(s), {len(threads)} thread link(s), "
          f"{len(seen)} cursor(s).")
    print("Note: the Instagram session was not migrated. It will be re-created "
          "and encrypted under IG_SESSION_KEY on the first login.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())