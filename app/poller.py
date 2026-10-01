"""Background thread that scans for `/link <code>` directives and ingests new
bookmarks into the mailbox.

Runs `Connector.scan_for_link_directives()` plus a batched ingest of every linked
thread, and feeds link results to `CodeStore.attempt_bind`, which does
first-touch-wins binding and silently ignores unknown/expired/already-bound
codes.

The ingest half exists so `/messages/links` never calls Instagram (D-018). It
buffers only messages old enough to be settled, because `enrich.pair_captions`
pairs a link with its caption by *adjacent index* — buffering a reel before the
user's caption arrives would drop that caption, and its hashtags, permanently.
"""

from __future__ import annotations

import logging
import threading

from .auth import CodeStore
from .connectors.base import Connector
from .enrich import enrich
from .ingest import settle_batch

log = logging.getLogger("link-poller")


def run_link_poller(
    stop_event: threading.Event,
    connector: Connector,
    code_store: CodeStore,
    interval_seconds: float,
    seen_store=None,
    mailbox=None,
    caption_window_seconds: int = 120,
    settle_seconds: int | None = None,
    prune_jobs: tuple = (),
    prune_every: int = 12,
) -> None:
    """Poll loop: directive scan + buffered ingest.

    `seen_store` and `mailbox` are optional so the JSON-only local setup still
    runs; without a mailbox there is nothing to ingest and only the directive
    scan happens. `prune_jobs` is a tuple of `(label, callable)` maintenance
    tasks — mailbox caps, expired link codes, expired metadata, expired
    throttles — run every `prune_every` cycles.
    """
    settle = caption_window_seconds if settle_seconds is None else settle_seconds
    log.warning(
        "link poller started (scan every %ss, settle %ss)", interval_seconds, settle
    )
    cycles = 0
    while not stop_event.wait(interval_seconds):
        try:
            directives = connector.scan_for_link_directives()
        except Exception:
            log.exception("link scan failed; retrying next cycle")
            directives = []

        for directive in directives:
            outcome = code_store.attempt_bind(directive)
            if outcome == "bound":
                log.warning(
                    "linked code %s -> %s (%s)",
                    directive.code,
                    directive.thread_key,
                    directive.username,
                )
            elif outcome == "rebound":
                log.warning(
                    "re-linked code %s -> %s (%s)",
                    directive.code,
                    directive.thread_key,
                    directive.username,
                )
            elif outcome in ("unknown", "expired", "already_linked"):
                log.info(
                    "ignored /link attempt code=%s outcome=%s thread=%s from=%s",
                    directive.code,
                    outcome,
                    directive.thread_key,
                    directive.username,
                )
            else:  # pragma: no cover - defensive
                log.info(
                    "unhandled bind outcome=%s code=%s thread=%s",
                    outcome,
                    directive.code,
                    directive.thread_key,
                )
        log.info("link scan: %d directive(s) found", len(directives))

        if mailbox is not None and seen_store is not None:
            cycles += 1
            try:
                _ingest_once(connector, seen_store, mailbox, settle, caption_window_seconds)
            except Exception:
                log.exception("ingest cycle failed; retrying next cycle")

            if prune_every and cycles % prune_every == 0:
                for label, fn in prune_jobs:
                    try:
                        fn()
                    except Exception:
                        log.exception("prune %s failed", label)

    log.warning("link poller stopped")


def _ingest_once(
    connector: Connector,
    seen_store,
    mailbox,
    settle_seconds: int,
    caption_window_seconds: int,
) -> None:
    """Buffer every linked thread's settled new items. Never raises upward."""
    threads = seen_store.bound_threads()
    if not threads:
        return

    results = connector.fetch_many(list(threads.keys()), threads)

    total_buffered = 0
    for thread_id, result in results.items():
        if not result.items:
            continue
        bufferable, new_cursor = settle_batch(
            result.items, threads.get(thread_id), settle_seconds
        )
        if not bufferable:
            continue  # everything is still settling; cursor deliberately unmoved

        # enrich() requires oldest-first input.
        links, texts = enrich(bufferable, caption_window_seconds)
        rows = [(item.id, row.model_dump(mode="json"))
                for item, row in _zip(bufferable, links, texts)]
        if not rows:
            continue
        new = mailbox.ingest(thread_id, rows)
        total_buffered += new
        # The cursor moves only after a successful insert, so a crash mid-cycle
        # redoes the work rather than skipping it.
        if new_cursor:
            seen_store.set(thread_id, new_cursor)

    if total_buffered:
        log.warning("ingest buffered %d item(s) across %d thread(s)",
                    total_buffered, len(threads))


def _zip(items, links, texts):
    """Re-associate each enriched row with the message id it came from.

    `enrich()` returns link rows and standalone-text rows in separate lists, so
    the original ordering has to be recovered by id (each row carries `id`).
    """
    by_id = {row.id: row for row in list(links) + list(texts)}
    for item in items:
        row = by_id.get(item.id)
        if row is not None:
            yield item, row