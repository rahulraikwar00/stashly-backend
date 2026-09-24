"""Background thread that scans for `/link <code>` directives and binds codes.

Runs the `Connector.scan_for_link_directives()` loop and feeds results to
`CodeStore.attempt_bind`, which does first-touch-wins binding and silently
ignores unknown/expired/already-bound codes.
"""

from __future__ import annotations

import logging
import threading

from .auth import CodeStore
from .connectors.base import Connector

log = logging.getLogger("link-poller")


def run_link_poller(
    stop_event: threading.Event,
    connector: Connector,
    code_store: CodeStore,
    interval_seconds: float,
) -> None:
    log.warning("link poller started (scan every %ss)", interval_seconds)
    while not stop_event.wait(interval_seconds):
        try:
            directives = connector.scan_for_link_directives()
        except Exception:
            log.exception("link scan failed; retrying next cycle")
            continue
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
    log.warning("link poller stopped")