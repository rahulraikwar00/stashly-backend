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
    # Honours `default`; a bare os.getenv(name, "") silently discarded it, which
    # only became visible once DB_BACKEND was the first caller to pass one.
    return (os.getenv(name) or default).strip()


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

    # Poller cadence. The scan only does useful work while a user is mid-link, so
    # it is event-driven rather than a fixed metronome: a fixed interval is both
    # wasteful (nothing arrives outside a link window) and machine-shaped to
    # Instagram. `link_scan_seconds` is the ACTIVE base, used only while a pending
    # code exists; idle backs off to `link_scan_idle_seconds`.
    link_scan_seconds: int = 30  # active base: someone is about to DM /link
    link_scan_idle_seconds: int = 600  # idle base: nothing pending, back off
    # Every wait is multiplied by a random factor in [1-jitter, 1+jitter], so no
    # two cycles are evenly spaced. A constant cadence is a bot signature.
    link_jitter_pct: int = 40
    # Held after a failed cycle, and doubled while failures keep coming. Hammering
    # an endpoint that is already rejecting is the worst response to a throttle.
    link_error_backoff_seconds: int = 300
    threads_per_fetch: int = 10
    thread_message_limit: int = 40
    burst_fetch_limit: int = 200
    caption_window_seconds: int = 120

    # ── Postgres (D-018) ──
    # Which Postgres is authoritative. "neon" (the default) means the only valid
    # store is the Neon branch named by DATABASE_URL, and startup fails if that
    # variable is missing rather than silently degrading to ephemeral storage.
    # "local" is the explicit opt-in for a throwaway Postgres (docker compose
    # override) and is never inferred from a hostname — a typo must not look like
    # a request for the local one.
    db_backend: str = "neon"
    # Required when db_backend == "neon".
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
    def using_local_postgres(self) -> bool:
        return self.db_backend == "local"

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


DB_BACKENDS = ("neon", "local")


def load_settings() -> Settings:
    from dotenv import load_dotenv

    load_dotenv(APP_DIR.parent / ".env")

    caption_window = max(5, _env_int("CAPTION_WINDOW_SECONDS", 120))
    settle = _env_int("INGEST_SETTLE_SECONDS", 0)
    db_backend = _env_str("DB_BACKEND", "neon").lower()

    # Reject unknown values rather than treating them as "neon": DB_BACKEND=Local
    # or DB_BACKEND=postgres would otherwise look like the local override while
    # actually writing to Neon.
    if db_backend not in DB_BACKENDS:
        raise ValueError(
            f"DB_BACKEND={db_backend!r} is not a valid backend; "
            f"expected one of {', '.join(DB_BACKENDS)}"
        )

    database_url = _env_str("DATABASE_URL")

    # Neon is the primary store, so a missing DSN is a misconfiguration that would
    # otherwise boot the app onto storage it silently loses on restart.
    if not database_url:
        raise ValueError(
            "DATABASE_URL is not set. The backend stores link codes, seen cursors "
            "and the encrypted Instagram session in Postgres, so it refuses to "
            "start without it. Copy .env.example to .env and set the Neon pooled "
            "DSN (hostname must contain '-pooler')."
        )

    return Settings(
        port=max(1, min(65535, _env_int("PORT", 8000))),
        ig_username=_env_str("IG_USERNAME"),
        ig_password=_env_str("IG_PASSWORD"),
        code_ttl_seconds=_env_int("CODE_TTL_SECONDS", 600),
        # Floor is 15s, not 5: an over-eager LINK_SCAN_SECONDS used to be able to
        # turn this into a fast poller, which is exactly the load pattern the
        # jitter and idle backoff exist to avoid.
        link_scan_seconds=max(15, _env_int("LINK_SCAN_SECONDS", 30)),
        # Kept under the 600s code TTL on purpose: a user who DMs /link as their
        # code is about to expire should still find it bound.
        link_scan_idle_seconds=max(15, _env_int("LINK_SCAN_IDLE_SECONDS", 600)),
        link_jitter_pct=min(90, max(0, _env_int("LINK_JITTER_PCT", 40))),
        link_error_backoff_seconds=max(
            15, _env_int("LINK_ERROR_BACKOFF_SECONDS", 300)
        ),
        threads_per_fetch=max(1, _env_int("THREADS_PER_FETCH", 10)),
        thread_message_limit=max(5, _env_int("THREAD_MESSAGE_LIMIT", 40)),
        burst_fetch_limit=max(50, _env_int("BURST_FETCH_LIMIT", 200)),
        caption_window_seconds=caption_window,
        db_backend=db_backend,
        database_url=database_url,
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
