"""Background thread that scans for `/link <code>` directives and ingests new
bookmarks into the mailbox.

Runs `Connector.scan_for_link_directives()` plus a batched ingest of every linked
thread, and feeds link results to `CodeStore.attempt_bind`, which does
first-touch-wins binding and silently ignores unknown/expired/already-bound
codes.

The ingest half exists so `/messages/links` never calls Instagram (D-018). It
buffers only messages old enough to be settled, because `enrich.pair_captions`
pairs a link with its caption by *adjacent index* — buffering a reel before the
user's caption arrives would drop that caption (and its hashtags), permanently.

Cadence is event-driven, not fixed. A `/link` directive can only arrive while a
user is mid-link — they register a code, then DM it. Polling at a constant short
interval to catch that one event spends almost all its calls on nothing, and a
perfectly even interval is a machine signature. So the loop picks its wait from
the work actually outstanding: fast and jittered while a code is pending, backed
off and jittered when idle, held off entirely after failures.
"""

from __future__ import annotations

import logging
import random
import threading

from .auth import CodeStore
from .connectors.base import Connector
from .enrich import enrich
from .ingest import settle_batch

log = logging.getLogger("link-poller")


def next_interval(
    base_seconds: float,
    jitter_pct: int,
    rng: random.Random | None = None,
) -> float:
    """A wait drawn from `[base * (1 - j), base * (1 + j)]`.

    Jitter is the point: consecutive cycles must not be evenly spaced, or the
    poller emits a machine-shaped rhythm that is trivially distinguishable from
    a person opening the app. A fixed interval is the single most recognisable
    automation signal here, and it costs nothing to avoid.
    """
    if jitter_pct <= 0:
        return float(base_seconds)
    rand = (rng or random).uniform(0.0, 1.0)
    lo = base_seconds * (1.0 - jitter_pct / 100.0)
    return lo + (base_seconds * (jitter_pct / 100.0) * 2.0) * rand


def _has_pending(code_store) -> bool:
    """True when a user is mid-link, so the next `/link` DM could arrive.

    Best-effort: a store without the query is treated as "maybe pending", which
    only costs the faster cadence.
    """
    counter = getattr(code_store, "has_pending", None)
    if counter is None:
        return True
    try:
        return bool(counter())
    except Exception:  # pragma: no cover - defensive
        log.exception("pending-code check failed; assuming active")
        return True


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
    idle_interval_seconds: float | None = None,
    jitter_pct: int = 40,
    error_backoff_seconds: float = 300.0,
    rng: random.Random | None = None,
    wake_event: threading.Event | None = None,
) -> None:
    """Poll loop: directive scan + buffered ingest.

    `seen_store` and `mailbox` are optional so the JSON-only local setup still
    runs; without a mailbox there is nothing to ingest and only the directive
    scan happens. `prune_jobs` is a tuple of `(label, callable)` maintenance
    tasks — mailbox caps, expired link codes, expired metadata, expired
    throttles — run every `prune_every` cycles.

    `interval_seconds` is the active base; `idle_interval_seconds` is used when
    no code is pending. Both are jittered. A cycle that raises backs off for
    `error_backoff_seconds`, doubling on consecutive failures, because retrying
    an endpoint that is already rejecting escalates the risk rather than
    reducing it.

    `wake_event` is the reason the idle backoff is safe: `CodeStore.register`
    pokes it, so a code registered while the poller is sleeping on the idle
    cadence cuts the wait short instead of making the user wait out the
    remainder of it. Without that, a 10-minute code could sit nearly unpolled
    for most of its life.
    """
    settle = caption_window_seconds if settle_seconds is None else settle_seconds
    idle_base = interval_seconds if idle_interval_seconds is None else idle_interval_seconds
    rand = rng or random.Random()

    # One object can serve as both: it carries `set()`, which shutdown uses, and
    # an extra `wake()` alias the code store can call on registration.
    wake = wake_event if wake_event is not None else stop_event
    setattr(code_store, "on_register", wake.set)

    log.warning(
        "link poller started (active ~%ss, idle ~%ss, ±%s%% jitter, settle %ss)",
        interval_seconds,
        idle_base,
        jitter_pct,
        settle,
    )

    cycles = 0
    consecutive_failures = 0
    last_mode = None
    while True:
        # Pick the cadence from outstanding work, not from a constant. A user who
        # registered a code is about to DM it, so that window is scanned tightly;
        # outside it there is nothing to find, so we back off hard.
        active = _has_pending(code_store)
        base = interval_seconds if active else idle_base
        wait = next_interval(base, jitter_pct, rand)

        # At WARNING because the root logger is WARNING, so the per-cycle INFO
        # lines below are invisible in practice. Mode changes are rare and are
        # what an operator needs when asking "why is linking slow?".
        if active != last_mode:
            log.warning(
                "poller cadence -> %s (~%ss ±%s%%)",
                "active (code pending)" if active else "idle (nothing pending)",
                base,
                jitter_pct,
            )
            last_mode = active

        if consecutive_failures:
            # Hold off, growing each time. The doubling is capped at an hour so a
            # long outage cannot park the poller indefinitely.
            hold = error_backoff_seconds * (2 ** min(consecutive_failures - 1, 5))
            log.warning(
                "backing off %.0fs after %d consecutive failed cycle(s)",
                hold,
                consecutive_failures,
            )
            if wake.wait(hold) and stop_event.is_set():
                break

        # `wake.wait` returns early when register() pokes it, so a new code is
        # picked up on the next cycle rather than after the current wait.
        woken = wake.wait(wait)
        if stop_event.is_set():
            break
        if woken and not active and _has_pending(code_store):
            # A registration interrupted the idle wait; run now rather than
            # sleeping out the remaining interval.
            log.info("code registered during idle wait; scanning now")

        failed = False
        # One listing serves the scan and the ingest. A connector that does not
        # implement `fetch_cycle` falls back to the two separate calls, so this
        # is an optimisation rather than a new requirement.
        threads = None
        results = None
        if mailbox is not None and seen_store is not None:
            threads = seen_store.bound_threads()
            if not threads:
                # Nothing linked yet: only the directive scan can do useful work.
                threads = None

        try:
            cycle = getattr(connector, "fetch_cycle", None)
            if cycle is not None and threads is not None:
                directives, results = cycle(list(threads.keys()), threads)
            else:
                directives = connector.scan_for_link_directives()
        except Exception:
            log.exception("link scan failed; will back off")
            directives = []
            failed = True

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
                    directive.thread_key,
                    directive.username,
                )
        log.info("link scan: %d directive(s) found", len(directives))

        if mailbox is not None and seen_store is not None:
            cycles += 1
            try:
                if results is not None:
                    _ingest_results(
                        results, seen_store, mailbox, settle, caption_window_seconds
                    )
                else:
                    _ingest_once(
                        connector, seen_store, mailbox, settle, caption_window_seconds
                    )
            except Exception:
                log.exception("ingest cycle failed; will back off")
                failed = True

            if prune_every and cycles % prune_every == 0:
                for label, fn in prune_jobs:
                    try:
                        fn()
                    except Exception:
                        log.exception("prune %s failed", label)

        consecutive_failures = consecutive_failures + 1 if failed else 0

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
    _ingest_results(results, seen_store, mailbox, settle_seconds, caption_window_seconds)


def _ingest_results(
    results: dict,
    seen_store,
    mailbox,
    settle_seconds: int,
    caption_window_seconds: int,
) -> None:
    """Buffer pre-fetched results, so a cycle can share one inbox listing."""
    threads = seen_store.bound_threads()
    if not threads:
        return

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