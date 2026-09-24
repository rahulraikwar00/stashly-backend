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

from .auth import CodeStore
from .config import load_settings
from .connectors import build_connector
from .poller import run_link_poller
from .routers import auth as auth_router
from .routers import debug as debug_router
from .routers import health as health_router
from .routers import messages as messages_router
from .routers import metadata as metadata_router
from .state import SeenStore

logging.basicConfig(level=logging.WARNING, format="%(levelname)s [%(name)s] %(message)s")

_settings = load_settings()

app = FastAPI(title="Bookmark Backend", version="2.0.0")

# The Expo app fetches this over the network; CORS only matters for web dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.settings = _settings
app.state.connector = build_connector(_settings)
app.state.code_store = CodeStore(_settings.links_file, ttl_seconds=_settings.code_ttl_seconds)
app.state.seen_store = SeenStore(_settings.seen_file)

app.include_router(health_router.router)
app.include_router(auth_router.router)
app.include_router(messages_router.router)
app.include_router(metadata_router.router)
app.include_router(debug_router.router)

_poller_stop = threading.Event()
_poller_thread: threading.Thread | None = None


@app.on_event("startup")
def on_startup() -> None:
    global _poller_stop, _poller_thread
    connector = app.state.connector
    if connector is None:
        logging.getLogger("startup").warning(
            "No messenger connector configured — Instagram relays are disabled."
            " Set IG_USERNAME/IG_PASSWORD to enable."
        )
        return
    connector.prepare()
    _poller_stop = threading.Event()
    _poller_thread = threading.Thread(
        target=run_link_poller,
        args=(
            _poller_stop,
            connector,
            app.state.code_store,
            _settings.link_scan_seconds,
        ),
        name="link-poller",
        daemon=True,
    )
    _poller_thread.start()
    logging.getLogger("startup").warning(
        "Instagram connector ready; link poller started (every %ss).",
        _settings.link_scan_seconds,
    )


@app.on_event("shutdown")
def on_shutdown() -> None:
    global _poller_stop, _poller_thread
    _poller_stop.set()
    if _poller_thread is not None:
        _poller_thread.join(timeout=3)
        _poller_thread = None


if __name__ == "__main__":
    from .config import load_settings
    import uvicorn

    s = load_settings()
    uvicorn.run("app.main:app", host="0.0.0.0", port=s.port)