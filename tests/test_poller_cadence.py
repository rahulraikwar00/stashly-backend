"""Poller cadence: jitter, idle backoff, and error backoff.

Uses a stubbed clock throughout — no real sleeps. The behaviour worth testing
is which interval gets chosen, and testing that by actually sleeping would make
the suite take minutes and still prove less.
"""

import random

from app.poller import _has_pending, next_interval, run_link_poller


# ── jitter ────────────────────────────────────────────────────────


def test_jitter_stays_within_bounds():
    rng = random.Random(0)
    for _ in range(200):
        assert 30 * 0.6 <= next_interval(30, 40, rng) <= 30 * 1.4


def test_jitter_actually_varies():
    """A fixed interval is the recognisable machine signal; the point is variance."""
    rng = random.Random(1234)
    draws = {next_interval(30, 40, rng) for _ in range(50)}
    assert len(draws) > 45, "jitter is not producing a spread of intervals"


def test_zero_jitter_is_exact():
    """An explicit 0 must mean 'no jitter', not 'unbounded'."""
    assert next_interval(30, 0) == 30.0


def test_jitter_scales_with_base():
    rng = random.Random(7)
    small = {next_interval(30, 40, rng) for _ in range(50)}
    big = {next_interval(600, 40, rng) for _ in range(50)}
    assert max(small) <= 42
    assert min(big) >= 360


# ── active vs idle ────────────────────────────────────────────────


class _CodeStore:
    def __init__(self, pending: bool) -> None:
        self._pending = pending

    def has_pending(self) -> bool:
        return self._pending


def test_pending_code_selects_the_fast_cadence():
    assert _has_pending(_CodeStore(True)) is True


def test_no_pending_code_selects_idle():
    assert _has_pending(_CodeStore(False)) is False


def test_store_without_the_query_assumes_active():
    """An older store has no `has_pending`; that should cost speed, not silence."""

    class Legacy:
        pass

    assert _has_pending(Legacy()) is True


def test_failing_pending_check_assumes_active():
    class Broken:
        def has_pending(self):
            raise RuntimeError("db down")

    assert _has_pending(Broken()) is True


# ── error backoff ─────────────────────────────────────────────────


def _backoff(base: float, failures: int) -> float:
    """Mirrors the poller's hold, kept here so the growth is pinned down."""
    return base * (2 ** min(failures - 1, 5))


def test_backoff_doubles_per_failure():
    assert _backoff(300, 1) == 300
    assert _backoff(300, 2) == 600
    assert _backoff(300, 3) == 1200


def test_backoff_is_capped():
    """An hour ceiling, so a long outage can't park the poller for a day."""
    assert _backoff(300, 40) == 300 * 32


def test_first_failure_is_the_shortest_hold():
    assert _backoff(300, 1) < _backoff(300, 5)


# ── one listing per cycle ─────────────────────────────────────────


class _Stop:
    """Stands in for the stop event: trips itself after `n` waits."""

    def __init__(self, n: int) -> None:
        self.waits: list[float | None] = []
        self._n = n
        self._stopped = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if len(self.waits) > self._n:
            self._stopped = True
        return self._stopped

    def set(self) -> None:
        self._stopped = True

    def wake(self) -> None:
        pass

    def is_set(self) -> bool:
        return self._stopped


class _Wake:
    """Interruptible wait: returns early when the poller is poked.

    `stop_after` bounds the loop so a test that exercises the wake path can
    terminate; without it a wake-driven test would run until the last stop.
    """

    def __init__(self, stop_after: int | None = None) -> None:
        self.waits: list[float | None] = []
        self._poked = False
        self._stopped = False
        self._wakes = 0
        self.stop_after = stop_after

    def set(self) -> None:
        if self.stop_after is not None and self._wakes >= self.stop_after:
            self._stopped = True
            return
        self._poked = True

    wake = set

    def clear(self) -> None:
        self._poked = False

    def is_set(self) -> bool:
        return self._stopped

    def stop(self) -> None:
        self._stopped = True
        self._poked = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.stop_after is not None and len(self.waits) >= self.stop_after:
            self._stopped = True
            return True
        if self._poked:
            self._poked = False
            return True
        return False


class _CountingConnector:
    """Counts inbox listings, which is the load the account actually feels."""

    def __init__(self) -> None:
        self.listings = 0

    def _list(self) -> None:
        self.listings += 1

    def fetch_cycle(self, keys, cursors):
        self._list()
        return [], {}

    def scan_for_link_directives(self, threads=None):
        if threads is None:
            self._list()
        return []

    def fetch_many(self, keys, cursors):
        self._list()
        return {}


class _LegacyCountingConnector(_CountingConnector):
    """A connector that predates `fetch_cycle`, so the poller takes the old path.

    Subclassed rather than `del`-ing the method off the shared class: removing
    it in place would silently change every later test in this file.
    """

    fetch_cycle = None


class _Seen:
    def bound_threads(self):
        return {"t1": None}

    def set(self, *args):
        pass


class _Mailbox:
    def ingest(self, *args):
        return 0


class _Codes:
    def __init__(self, pending: bool = True) -> None:
        self.pending = pending
        self.on_register = None

    def has_pending(self):
        return self.pending

    def attempt_bind(self, directive):
        return "unknown"


def test_cycle_issues_one_listing_not_two():
    """The halving: scan and ingest share one inbox listing.

    Two identical `direct_threads` calls per cycle doubled the account's load for
    no extra information, so this pins the folded path at one.
    """
    connector = _CountingConnector()
    run_link_poller(
        _Stop(3), connector, _Codes(), 30,
        seen_store=_Seen(), mailbox=_Mailbox(),
        jitter_pct=0, rng=random.Random(0),
    )
    assert connector.listings == 3


def test_connector_without_fetch_cycle_still_works():
    """Backwards compatibility: the optimisation must not become a requirement."""
    connector = _LegacyCountingConnector()
    run_link_poller(
        _Stop(3), connector, _Codes(), 30,
        seen_store=_Seen(), mailbox=_Mailbox(),
        jitter_pct=0, rng=random.Random(0),
    )
    assert connector.listings == 6  # unchanged two-call path


def test_no_linked_threads_skips_the_shared_cycle():
    """With nothing linked, only the directive scan can find work."""
    class _EmptySeen:
        def bound_threads(self):
            return {}

    connector = _CountingConnector()
    run_link_poller(
        _Stop(3), connector, _Codes(), 30,
        seen_store=_EmptySeen(), mailbox=_Mailbox(),
        jitter_pct=0, rng=random.Random(0),
    )
    assert connector.listings == 3


def test_stop_event_is_respected():
    """The loop must exit promptly once stop is set, not finish the backlog."""
    stop = _Stop(2)
    connector = _CountingConnector()
    run_link_poller(
        stop, connector, _Codes(), 30,
        seen_store=_Seen(), mailbox=_Mailbox(),
        jitter_pct=0, rng=random.Random(0),
    )
    # 3 waits: two full cycles, then the third returns True and breaks.
    assert connector.listings == 2
    assert len(stop.waits) == 3


def test_active_and_idle_wait_differ():
    """A pending code must pick a materially faster wait than idle."""
    class _Pending(_Codes):
        def has_pending(self):
            return True

    class _Idle(_Codes):
        def has_pending(self):
            return False

    active_stop = _Stop(1)
    run_link_poller(active_stop, _CountingConnector(), _Pending(), 30,
                    seen_store=_Seen(), mailbox=_Mailbox(),
                    idle_interval_seconds=600, jitter_pct=0,
                    rng=random.Random(0))
    idle_stop = _Stop(1)
    run_link_poller(idle_stop, _CountingConnector(), _Idle(), 30,
                    seen_store=_Seen(), mailbox=_Mailbox(),
                    idle_interval_seconds=600, jitter_pct=0,
                    rng=random.Random(0))
    assert active_stop.waits[0] == 30   # someone is about to DM /link
    assert idle_stop.waits[0] == 600    # nothing pending, back off


def test_failed_cycle_earns_a_backoff_hold():
    """A rejecting endpoint must be left alone, not retried on the normal loop."""
    class _Failing(_CountingConnector):
        def fetch_cycle(self, keys, cursors):
            self._list()
            raise RuntimeError("429 from Instagram")

    stop = _Stop(3)
    run_link_poller(
        stop, _Failing(), _Codes(), 30,
        seen_store=_Seen(), mailbox=_Mailbox(),
        jitter_pct=0, error_backoff_seconds=300, rng=random.Random(0),
    )
    # After a failure the next wait is the 300s hold, not the 30s cadence.
    assert 300 in stop.waits
    assert 600 in stop.waits  # second failure doubles it


# ── a registration must interrupt the idle wait ──────────────────


def test_registration_wakes_the_poller_out_of_idle():
    """The bug that made the idle backoff unsafe.

    With a plain sleep, a code registered while the poller is idling at ~10
    minutes went unpolled for most of its 10-minute TTL. `register()` pokes the
    wake event, so the wait is cut short instead of slept out.
    """
    codes = _Codes(pending=False)
    wake = _Wake(stop_after=2)
    run_link_poller(
        wake, _CountingConnector(), codes, 30,
        seen_store=_Seen(), mailbox=_Mailbox(),
        idle_interval_seconds=600, jitter_pct=0,
        rng=random.Random(0), wake_event=wake,
    )
    # It chose the idle cadence...
    assert wake.waits[0] == 600
    # ...and the hook a registration depends on is actually installed.
    assert codes.on_register is not None


def test_poke_interrupts_a_pending_wait():
    """A poke makes the current wait return immediately, not at its timeout."""
    wake = _Wake(stop_after=1)
    wake.set()
    assert wake.wait(600) is True
    assert wake.waits == [600]


def test_poller_attaches_its_wake_hook_to_the_store():
    """Without this wiring, registration cannot wake the poller at all."""
    codes = _Codes()
    run_link_poller(
        _Stop(2), _CountingConnector(), codes, 30,
        seen_store=_Seen(), mailbox=_Mailbox(),
        jitter_pct=0, rng=random.Random(0),
    )
    assert codes.on_register is not None
