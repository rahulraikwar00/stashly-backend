"""Ingest helpers: decide what may be buffered now, and what must wait.

Pure functions, no I/O, so the caption-window contract can be tested without a
database, a poller, or Instagram.

The problem this solves: `enrich.pair_captions` pairs a link with a text
message only when they are at *adjacent indices* of one batch, within the
caption window. Buffering a reel the instant it arrives and delivering it means
the user's caption — which they type seconds later as a separate DM — can never
be attached, because delivery already deleted the row. Worse, `build_response`
derives `tags` from the caption, so a dropped caption silently drops the user's
hashtags too.

So an item is only buffered once it is old enough that any caption destined for
it has almost certainly arrived, and the cursor advances only over what was
actually buffered.
"""

from __future__ import annotations

import time
from typing import Iterable, Sequence, TypeVar

from .connectors.base import InboundItem

T = TypeVar("T")

# InboundItems are "newest-first" throughout the connector layer.
_NEWEST_FIRST = "items are expected newest-first"


def now_epoch() -> float:
    return time.time()


def drop_boundary(items: Sequence[InboundItem], cursor: str | None) -> list[InboundItem]:
    """Remove the item that IS the cursor.

    `fetch_new` already filters its own boundary out of the returned items, so
    this is belt-and-braces: it protects the invariant that an already-ingested
    message is never re-enriched and re-buffered if a connector forgets to.
    """
    if not cursor:
        return list(items)
    return [i for i in items if i.id != cursor]


def settle_cutoff(settle_seconds: int, *, now: float | None = None) -> float:
    """Epoch-seconds boundary: items at or older than this are bufferable."""
    reference = now_epoch() if now is None else now
    return reference - max(0, settle_seconds)


def partition_by_settle(
    items: Sequence[InboundItem],
    cutoff: float,
) -> tuple[list[InboundItem], list[InboundItem]]:
    """Split into `(ready, held)` at the settle cutoff.

    Because the input is newest-first and the cutoff keeps the *older* items,
    `ready` is always a contiguous suffix and `held` a contiguous prefix. That
    contiguity is what keeps adjacent-neighbour caption pairing valid across
    successive cycles.

    Items with an unknown timestamp are treated as ready rather than held. They
    can never pair with anything anyway (`_within_window` rejects None), and
    holding them could stall the cursor indefinitely.
    """
    ready: list[InboundItem] = []
    held: list[InboundItem] = []
    for item in items:  # newest-first
        if item.timestamp is None or item.timestamp <= cutoff:
            ready.append(item)
        else:
            held.append(item)
    return ready, held


def advance_cursor(ready: Sequence[InboundItem]) -> str | None:
    """The cursor to persist after buffering `ready`: its newest item's id.

    Returns None when nothing was buffered, meaning the cursor must not move —
    otherwise the un-buffered tail would be skipped and lost.
    """
    if not ready:
        return None
    return ready[0].id  # newest-first: index 0 of `ready` is its newest item


def settle_batch(
    items: Iterable[InboundItem],
    cursor: str | None,
    settle_seconds: int,
    *,
    now: float | None = None,
) -> tuple[list[InboundItem], str | None]:
    """One cycle's decision: `(bufferable, new_cursor)`.

    Order matters here. The cursor is chosen from the settled items *including*
    the boundary, and only then is the boundary excluded from what gets buffered:

    - choosing first means the cursor can never move backwards when the newest
      item happens to be the boundary itself;
    - excluding second means an already-buffered message is never re-enriched.

    When nothing new has settled, the incoming cursor is retained untouched, so
    the unsettled tail is never skipped.
    """
    settled, _held = partition_by_settle(
        list(items), settle_cutoff(settle_seconds, now=now)
    )
    new_cursor = advance_cursor(settled) or cursor
    return drop_boundary(settled, cursor), new_cursor


def oldest_first(items: Sequence[InboundItem]) -> list[InboundItem]:
    """`enrich()` requires oldest-first input."""
    return list(reversed(items))