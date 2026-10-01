"""Environment-driven settings for the bookmark backend."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # ── Serving ──
    # Which TCP port to serve on. Read from .env (`PORT`), default 8000.
    # On Render this is injected as 10000; do not set it by hand.
    port: int = 8000

    # ── Instagram (the official account that receives /link <code> DMs) ──
    ig_username: str = ""
    ig_password: str = ""

    # ── Relay / linking ──
    code_ttl_seconds: int = 600  # pending link codes expire after 10 min
    link_scan_seconds: int = 20  # poller cadence for detecting /link directives
    threads_per_fetch: int = 10
    thread_message_limit: int = 40
    burst_fetch_limit: int = 200
    caption_window_seconds: int = 120

    # ── Postgres (D-018) ──
    # Unset => the JSON-file stores are used (local development). Set on Render.
    database_url: str = ""
    # Neon sends `channel_binding=require`; the pooler does not negotiate it
    # reliably, so the DSN is stripped by default.
    drop_channel_binding: bool = True

    # ── Ingest / mailbox ──
    # How long a message must sit before it is buffered. Defaults to the caption
    # window because pair_captions() only pairs a link with text at ADJACENT
    # indices in one batch: buffering a reel before its caption arrives drops the
    # caption (and its hashtags) permanently. Set to 0 to disable settling, which
    # knowingly trades caption fidelity for lower latency.
    ingest_settle_seconds: int = 0  # 0 => follow caption_window_seconds
    # Oldest rows are dropped past this per thread, so a user who never opens the
    # app cannot push Neon past its 0.5 GB ceiling.
    mailbox_cap_per_thread: int = 500
    mailbox_drain_limit: int = 200
    # /metadata cache lifetime, in days.
    metadata_cache_days: int = 7

    # ── Auth throttling ──
    auth_max_failures: int = 5
    auth_lock_seconds: int = 900  # 15 minutes

    # ── Secrets ──
    # 64 hex chars or base64, decoded to a 32-byte AES-256-GCM key. Unset =>
    # the Instagram session is kept in memory only, never written anywhere.
    ig_session_key: str = ""

    # ── CORS ──
    # Defaults to disabled: the Expo client sends no browser Origin, so there is
    # no legitimate reason to allow any. Comma-separated to opt back in.
    cors_origins: tuple[str, ...] = ()

    # ── Paths (all in the app folder, gitignored) ──
    session_file: Path = field(default_factory=lambda: APP_DIR / "session.json")
    seen_file: Path = field(default_factory=lambda: APP_DIR / "seen_messages.json")
    links_file: Path = field(default_factory=lambda: APP_DIR / "links.json")

    @property
    def instagram_configured(self) -> bool:
        return bool(self.ig_username and self.ig_password)

    @property
    def postgres_configured(self) -> bool:
        return bool(self.database_url)

    @property
    def session_persistence_allowed(self) -> bool:
        """False when no key is configured, which forbids writing the session."""
        return bool(self.ig_session_key)

    @property
    def effective_settle_seconds(self) -> int:
        """Settle window actually used by the ingest loop."""
        if self.ingest_settle_seconds > 0:
            return self.ingest_settle_seconds
        return self.caption_window_seconds


def load_settings() -> Settings:
    from dotenv import load_dotenv

    load_dotenv(APP_DIR.parent / ".env")

    caption_window = max(5, _env_int("CAPTION_WINDOW_SECONDS", 120))
    settle = _env_int("INGEST_SETTLE_SECONDS", 0)

    return Settings(
        port=max(1, min(65535, _env_int("PORT", 8000))),
        ig_username=_env_str("IG_USERNAME"),
        ig_password=_env_str("IG_PASSWORD"),
        code_ttl_seconds=_env_int("CODE_TTL_SECONDS", 600),
        link_scan_seconds=max(5, _env_int("LINK_SCAN_SECONDS", 20)),
        threads_per_fetch=max(1, _env_int("THREADS_PER_FETCH", 10)),
        thread_message_limit=max(5, _env_int("THREAD_MESSAGE_LIMIT", 40)),
        burst_fetch_limit=max(50, _env_int("BURST_FETCH_LIMIT", 200)),
        caption_window_seconds=caption_window,
        database_url=_env_str("DATABASE_URL"),
        drop_channel_binding=_env_bool("DROP_CHANNEL_BINDING", True),
        ingest_settle_seconds=max(0, settle),
        mailbox_cap_per_thread=max(10, _env_int("MAILBOX_CAP_PER_THREAD", 500)),
        mailbox_drain_limit=max(1, _env_int("MAILBOX_DRAIN_LIMIT", 200)),
        metadata_cache_days=max(1, _env_int("METADATA_CACHE_DAYS", 7)),
        auth_max_failures=max(1, _env_int("AUTH_MAX_FAILURES", 5)),
        auth_lock_seconds=max(60, _env_int("AUTH_LOCK_SECONDS", 900)),
        ig_session_key=_env_str("IG_SESSION_KEY"),
        cors_origins=tuple(
            o.strip()
            for o in _env_str("CORS_ORIGINS").split(",")
            if o.strip()
        ),
    )
