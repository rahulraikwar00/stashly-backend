"""Tests for the settle logic (app/ingest.py).

The caption-window contract is the thing these protect: a reel and the caption
the user types seconds later must arrive as ONE bookmark with the caption and its
hashtags attached.
"""

from __future__ import annotations

from app.connectors.base import InboundItem
from app.enrich import enrich
from app.ingest import (
    advance_cursor,
    drop_boundary,
    partition_by_settle,
    settle_batch,
    settle_cutoff,
)

CAPTION_WINDOW = 120.0


def _item(item_id: str, kind: str, content: str, ts: float, sender: str = "7") -> InboundItem:
    return InboundItem(
        id=item_id,
        thread_key="42",
        sender_id=sender,
        username="tester",
        type=kind,
        content=content,
        timestamp=ts,
        preview_url="",
        author="",
    )


def _reel(item_id: str, ts: float) -> InboundItem:
    return _item(item_id, "link", "https://www.instagram.com/reel/ABC/", ts)


def _text(item_id: str, content: str, ts: float) -> InboundItem:
    return _item(item_id, "text", content, ts)


# ── the regression that matters ────────────────────────────────────

def test_reel_plus_later_caption_becomes_one_merged_bookmark():
    """A reel buffered early would lose its caption AND its hashtags forever.

    With settling, both messages land in the same batch and pair up.
    """
    t0 = 10_000.0
    reel_ts = t0 - 60.0
    caption_ts = t0 - 30.0  # user typed the caption 30s after sharing

    # Cycle 1, shortly after the share: the reel is inside the settle window,
    # so it is held rather than buffered.
    ready1, cursor1 = settle_batch([_reel("m1", reel_ts)], None, CAPTION_WINDOW, now=t0)
    assert ready1 == [], "a 60s-old reel is not bufferable yet"
    assert cursor1 is None, "the cursor must not move past un-buffered items"

    # Cycle 2, one poll interval plus the settle window later: the reel and the
    # caption that followed it are both settled and land in one batch.
    t1 = t0 + 130.0
    cycle2 = [_text("m2", "best pizza #food", caption_ts), _reel("m1", reel_ts)]
    ready2, cursor2 = settle_batch(cycle2, cursor1, CAPTION_WINDOW, now=t1)

    assert [i.id for i in ready2] == ["m2", "m1"]
    assert cursor2 == "m2", "cursor advances to the newest buffered item"

    links, texts = enrich(ready2, CAPTION_WINDOW)
    assert texts == [], "the caption is consumed by the link, not emitted alone"
    assert len(links) == 1, "exactly one bookmark row"
    assert links[0].customDescription == "best pizza #food"
    assert links[0].tags == ["food"], "hashtags derive from the caption"


def test_without_settling_the_caption_would_be_lost():
    """The counterfactual: proving settling is load-bearing, not decoration."""
    t0 = 10_000.0
    reel_ts = t0 - 60.0
    caption_ts = t0 - 30.0

    # settle_seconds=0 -> everything is immediately bufferable.
    cycle1_ready, cursor1 = settle_batch([_reel("m1", reel_ts)], None, 0, now=t0)
    assert [i.id for i in cycle1_ready] == ["m1"]
    assert cursor1 == "m1"

    # The caption arrives in the NEXT cycle; the reel is already delivered.
    cycle2_ready, _ = settle_batch(
        [_text("m2", "best pizza #food", caption_ts)], cursor1, 0, now=t0
    )
    links, texts = enrich(cycle2_ready, CAPTION_WINDOW)
    assert links == [], "the reel was already delivered without its caption"
    assert texts and texts[0].tags == ["food"]
    # The hashtag survives only on a stray text row — the bookmark lost it.


# ── partition mechanics ────────────────────────────────────────────

def test_partition_keeps_ready_contiguous_and_drops_boundary():
    now = 10_000.0
    items = [
        _text("m4", "now", now - 1.0),      # too new
        _text("m3", "recent", now - 60.0),  # too new
        _reel("m2", now - 200.0),           # bufferable
        _reel("m1", now - 300.0),           # bufferable
    ]
    ready, held = partition_by_settle(items, settle_cutoff(CAPTION_WINDOW, now=now))
    assert [i.id for i in held] == ["m4", "m3"]
    assert [i.id for i in ready] == ["m2", "m1"], "ready is a contiguous suffix"


def test_boundary_item_is_excluded():
    now = 10_000.0
    items = [_reel("m1", now - 300.0), _reel("m0", now - 400.0)]
    cleaned = drop_boundary(items, "m0")
    assert [i.id for i in cleaned] == ["m1"]


def test_unknown_timestamp_is_bufferable_not_held():
    """A None timestamp can never pair, and holding it would stall the cursor."""
    item = _reel("m1", 0.0)
    item.timestamp = None
    ready, held = partition_by_settle([item], settle_cutoff(CAPTION_WINDOW))
    assert [i.id for i in ready] == ["m1"]
    assert held == []


def test_advance_cursor_holds_when_nothing_buffered():
    assert advance_cursor([]) is None


def test_settle_is_idempotent_across_cycles():
    """Re-reading the same batch must not duplicate or lose anything.

    A poller restart mid-cycle re-reads the batch with the same cursor, so the
    same input has to produce the same output.
    """
    now = 10_000.0
    items = [_reel("m2", now - 200.0), _reel("m1", now - 300.0)]
    ready1, cursor1 = settle_batch(items, None, CAPTION_WINDOW, now=now)
    ready2, cursor2 = settle_batch(items, None, CAPTION_WINDOW, now=now)
    assert [i.id for i in ready1] == [i.id for i in ready2]
    assert cursor1 == cursor2 == "m2"


def test_cursor_never_moves_backwards_across_cycles():
    """Regression: choosing the cursor before excluding the boundary.

    When the newest item in the batch IS the cursor, picking the cursor after
    stripping it would move the cursor back to an older message, so that message
    would be re-fetched and re-buffered on every cycle forever.

    (In production `fetch_new` only returns messages newer than the cursor, and
    `ON CONFLICT (thread_id, source_msg)` absorbs anything re-offered. What must
    hold unconditionally is: the cursor never regresses, and the boundary is
    never re-buffered.)
    """
    now = 10_000.0
    batch = [_reel("m2", now - 200.0), _reel("m1", now - 300.0)]

    _bufferable, cursor = settle_batch(batch, None, CAPTION_WINDOW, now=now)
    assert cursor == "m2"

    for _ in range(3):
        bufferable, cursor = settle_batch(batch, cursor, CAPTION_WINDOW, now=now)
        assert cursor == "m2", "the cursor must not regress"
        assert "m2" not in [i.id for i in bufferable], "boundary is never re-buffered"


def test_already_buffered_cursor_is_not_rebuffered():
    """Once the cursor is on an item, that item is never re-emitted.

    `fetch_new` already filters its own boundary, and `drop_boundary` is the
    second line of defence for connectors that forget to.
    """
    now = 10_000.0
    items = [_reel("m2", now - 200.0), _reel("m1", now - 300.0)]
    ready, cursor = settle_batch(items, "m2", CAPTION_WINDOW, now=now)
    assert [i.id for i in ready] == ["m1"]
    assert cursor == "m2", "the cursor still points at the newest known message"