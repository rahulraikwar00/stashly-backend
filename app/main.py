"""Bookmark backend — FastAPI app factory.

Structured relay (see my-expo-app/DesingDecision/decisions.md D-016 and
my-expo-app/docs/02-Architecture.md):

    app/
      main.py                this file — factory, CORS, routers, startup, poller
      config.py              env-driven settings
      models.py              Pydantic request/response shapes
      auth.py                link-code store + require_code dependency
      state.py               per-thread seen cursors
      enrich.py              InboundItem -> BookmarkResponse (contract §3.3)
      extract.py / guards.py metadata extractor + SSRF guard
      poller.py              background /link <code> scanner
      connectors/            platform-independent Connector ABC + instagram impl
      routers/               health / auth / messages / metadata / debug

Run:
    cd backend
    .venv/bin/python -m app    # port from PORT in .env (default 8000)
"""

from __future__ import annotations

import logging
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .auth import build_code_store
from .config import load_settings
from .connectors import build_connector
from .db.pool import close_pool, get_pool
from .db.migrate import run_migrations
from .poller import run_link_poller
from .routers import auth as auth_router
from .routers import debug as debug_router
from .routers import health as health_router
from .routers import messages as messages_router
from .routers import metadata as metadata_router
from .state import build_seen_store

logging.basicConfig(level=logging.WARNING, format="%(levelname)s [%(name)s] %(message)s")

_settings = load_settings()

app = FastAPI(title="Bookmark Backend", version="3.0.0")

# The Expo app fetches this over the network and sends no browser Origin, so the
# default is to allow nothing. Previously this was "*" for methods and headers too,
# which is a needless wildcard behind an unauthenticated endpoint. Set
# CORS_ORIGINS (comma-separated) to opt back in.
if _settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(_settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key", "Authorization"],
    )
else:
    logging.getLogger("main").warning(
        "CORS disabled: no CORS_ORIGINS configured (expected for native clients)"
    )

app.state.settings = _settings

# ── Postgres (D-018) ───────────────────────────────────────────────
# Neon is the primary store (D-018): every durable fact lives in the branch named
# by DATABASE_URL. The JSON-file stores are no longer a runtime path, so there is
# no pool-less degradation left to fall into — `load_settings()` already rejects a
# missing DSN, and `_settings.postgres_configured` is guaranteed True here.
_pool = get_pool(_settings.database_url)
run_migrations(
    _settings.database_url,
    drop_channel_binding=_settings.drop_channel_binding,
)
if _settings.using_local_postgres:
    logging.getLogger("main").warning(
        "DB_BACKEND=local — writing to the local Postgres at %s. Link codes will "
        "NOT survive a container recreate.",
        _settings.database_url.split("@")[-1].split("?")[0],
    )

from .db.failed_auth import PgFailedAuthStore
from .db.mailbox import PgMailboxStore
from .db.metadata_cache import PgMetadataCache
from .db.secrets import PgSecretStore

# A missing IG_SESSION_KEY is not an error: it means the Instagram session
# is kept in memory only and never written anywhere.
_session_key = None
if _settings.ig_session_key:
    from .crypto import KeyError_, load_key

    try:
        _session_key = load_key(_settings.ig_session_key)
    except KeyError_ as exc:
        logging.getLogger("main").error(
            "IG_SESSION_KEY invalid (%s) — session will not be persisted", exc
        )
else:
    logging.getLogger("main").warning(
        "IG_SESSION_KEY unset — Instagram session stays in memory. On Render "
        "that means a fresh login on every deploy, which risks the account."
    )

_secret_store = PgSecretStore(_pool, _session_key)
_mailbox = PgMailboxStore(_pool, cap_per_thread=_settings.mailbox_cap_per_thread)
_metadata_cache = PgMetadataCache(_pool, ttl_days=_settings.metadata_cache_days)
_failed_auth = PgFailedAuthStore(_pool)

app.state.secret_store = _secret_store
app.state.mailbox = _mailbox
app.state.metadata_cache = _metadata_cache
app.state.failed_auth = _failed_auth
app.state.connector = build_connector(_settings, secret_store=_secret_store)
app.state.code_store = build_code_store(_settings, _pool)
app.state.seen_store = build_seen_store(_settings, _pool)

app.include_router(health_router.router)
app.include_router(auth_router.router)
app.include_router(messages_router.router)
app.include_router(metadata_router.router)
app.include_router(debug_router.router)

_poller_stop = threading.Event()
_poller_wake = threading.Event()
_poller_thread: threading.Thread | None = None


def _prune_jobs() -> tuple:
    """Periodic maintenance the poller runs every few cycles.

    Keeps Neon inside its 0.5 GB ceiling and stops expired codes, cached metadata
    and expired throttles accumulating forever.
    """
    jobs = []
    if _mailbox is not None:
        jobs.append(("mailbox", _mailbox.prune))
        jobs.append(("expired codes", _mailbox.prune_expired_codes))
    if _metadata_cache is not None:
        jobs.append(("metadata cache", _metadata_cache.prune_expired))
    if _failed_auth is not None:
        jobs.append(("auth throttles", _failed_auth.prune_expired))
    return tuple(jobs)


@app.on_event("startup")
def on_startup() -> None:
    global _poller_stop, _poller_wake, _poller_thread
    connector = app.state.connector
    if connector is None:
        logging.getLogger("startup").warning(
            "No messenger connector configured — Instagram relays are disabled."
            " Set IG_USERNAME/IG_PASSWORD to enable."
        )
        return
    connector.prepare()
    _poller_stop = threading.Event()
    # A distinct event, because the poller's wake hook is stored on the code
    # store and fired by every registration. Reusing the stop event would make
    # the first POST /auth/codes permanently stop the poller.
    _poller_wake = threading.Event()
    _poller_thread = threading.Thread(
        target=run_link_poller,
        args=(
            _poller_stop,
            connector,
            app.state.code_store,
            _settings.link_scan_seconds,
        ),
        kwargs={
            "seen_store": app.state.seen_store,
            "mailbox": app.state.mailbox,
            "caption_window_seconds": _settings.caption_window_seconds,
            "settle_seconds": _settings.effective_settle_seconds,
            "prune_jobs": _prune_jobs(),
            "idle_interval_seconds": _settings.link_scan_idle_seconds,
            "jitter_pct": _settings.link_jitter_pct,
            "error_backoff_seconds": _settings.link_error_backoff_seconds,
            "wake_event": _poller_wake,
        },
        name="link-poller",
        daemon=True,
    )
    _poller_thread.start()
    logging.getLogger("startup").warning(
        "Instagram connector ready; link poller started "
        "(active ~%ss, idle ~%ss, ±%s%% jitter).",
        _settings.link_scan_seconds,
        _settings.link_scan_idle_seconds,
        _settings.link_jitter_pct,
    )


@app.on_event("shutdown")
def on_shutdown() -> None:
    global _poller_thread
    _poller_stop.set()
    # Cut the poller's current wait short: it may be idling for up to ~14
    # minutes, and a 3s join would not get past that.
    _poller_wake.set()
    if _poller_thread is not None:
        _poller_thread.join(timeout=3)
        _poller_thread = None
    close_pool()


if __name__ == "__main__":
    from .config import load_settings
    import uvicorn

    s = load_settings()
    uvicorn.run("app.main:app", host="0.0.0.0", port=s.port)