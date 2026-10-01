"""End-to-end: poller ingest -> mailbox -> GET /messages/links (D-018).

This is the path that replaces the old live Instagram fetch, so it is exercised
as a whole rather than in pieces: a fake connector feeds the poller, and the app
is called over HTTP exactly as the Expo client calls it.

Requires `TEST_DATABASE_URL`.
"""

from __future__ import annotations

import time

import pytest

from conftest import requires_db

pytestmark = requires_db

# Real elapsed time: with settle=0 the cutoff is "now", so scripted messages
# must be in the past to be bufferable; with settle=120 the same recent
# timestamps must stay held. Both scenarios come from one set of fixtures.
NOW = time.time()


class FakeConnector:
    """Minimal Connector: replays a scripted inbox, counts upstream listings."""

    platform = "fake"

    def __init__(self, threads: dict[str, list]) -> None:
        self._threads = threads
        self.listings = 0

    def prepare(self) -> None:
        pass

    def _listing(self):
        self.listings += 1

        class T:
            def __init__(self, tid, messages):
                self.id = tid
                self.messages = messages
                self.users = []

        return [T(tid, msgs) for tid, msgs in self._threads.items()]

    def scan_for_link_directives(self) -> list:
        return []

    def _cursor_of(self, messages, cursor):
        if not cursor:
            return None
        return next((i for i, m in enumerate(messages) if m.id == cursor), None)

    def _slice(self, messages, cursor):
        idx = self._cursor_of(messages, cursor)
        if idx is None:
            return list(messages), False
        return list(messages[:idx]), True

    def fetch_new(self, thread_key, cursor):
        self._listing()
        messages = self._threads.get(str(thread_key), [])
        fresh, had_boundary = self._slice(messages, cursor)
        items = [] if had_boundary else [m for m in fresh]
        cursor_new = messages[0].id if messages and not had_boundary else None
        from app.connectors.base import FetchResult

        return FetchResult(items=items, cursor=cursor_new)

    def fetch_many(self, thread_keys, cursors):
        """One listing for N threads, mirroring the Instagram implementation."""
        threads = {str(t.id): t for t in self._listing()}
        from app.connectors.base import FetchResult

        out = {}
        for key in thread_keys:
            thread = threads.get(str(key))
            if thread is None:
                out[key] = FetchResult()
                continue
            fresh, had_boundary = self._slice(thread.messages, cursors.get(key))
            out[key] = FetchResult(
                items=[] if had_boundary else list(fresh),
                cursor=thread.messages[0].id if thread.messages and not had_boundary else None,
            )
        return out

    def history(self, thread_key, limit):
        return self._threads.get(str(thread_key), [])[:limit]

    def raw_dump(self, thread_key, count=5):
        return {"thread_id": thread_key or "", "messages": []}


class Msg:
    """Stands in for an instagrapi message."""

    def __init__(self, mid, kind, content, ts, sender="7"):
        self.id = mid
        self.item_type = kind
        self.text = content if kind == "text" else None
        self.user_id = sender
        self.timestamp = _Ts(ts)
        self.xma_share = None
        self.media_share = None
        self.clip = None
        self.reel_share = None
        self.felix_share = None
        self.link = None


class _Ts:
    def __init__(self, value):
        self.value = value

    def timestamp(self):
        return self.value


def _reel_msg(mid, ts, shortcode="ABC123"):
    m = Msg(mid, "text", "", ts)
    # xma_share is what makes a shared reel a "link" with preview + author.
    class Share:
        video_url = f"https://www.instagram.com/reel/{shortcode}/?igsh=1"
        preview_url = "https://example.com/preview.jpg"
        header_title_text = "chef_pizza"
    m.xma_share = Share()
    m.item_type = "clip"
    return m


def _text_msg(mid, content, ts):
    return Msg(mid, "text", content, ts)


@pytest.fixture
def env(pool):
    """A FastAPI app wired to Postgres, with a scripted connector."""
    from fastapi import FastAPI

    from app.auth import build_code_store
    from app.connectors.base import InboundItem
    from app.db.mailbox import PgMailboxStore
    from app.routers import messages as messages_router
    from app.state import build_seen_store

    # Newest-first, as the real connector returns them: the caption (m3) is
    # newer than the reel (m2) because the user typed it after sharing.
    threads = {
        "42": [
            _text_msg("m3", "best pizza #food", NOW - 10),
            _reel_msg("m2", NOW - 30),
        ]
    }
    connector = FakeConnector(threads)

    # The connector yields raw message objects; normalize them the way the real
    # connector does so enrich() sees InboundItems.
    def _normalize(items):
        out = []
        for m in items:
            is_link = m.xma_share is not None
            out.append(
                InboundItem(
                    id=m.id,
                    thread_key="42",
                    sender_id=m.user_id,
                    username="tester",
                    type="link" if is_link else "text",
                    content=(m.xma_share.video_url.split("?")[0] if is_link else (m.text or "")),
                    timestamp=m.timestamp.value,
                    preview_url=m.xma_share.preview_url if is_link else "",
                    author=m.xma_share.header_title_text if is_link else "",
                )
            )
        return out

    original_fetch_many = connector.fetch_many

    def fetch_many_with_normalized(keys, cursors):
        raw = original_fetch_many(keys, cursors)
        from app.connectors.base import FetchResult

        return {
            k: FetchResult(items=_normalize(v.items), cursor=v.cursor)
            for k, v in raw.items()
        }

    connector.fetch_many = fetch_many_with_normalized

    app = FastAPI()

    class Cfg:
        caption_window_seconds = 120
        mailbox_drain_limit = 200
        code_ttl_seconds = 600
        auth_max_failures = 5
        auth_lock_seconds = 900
        database_url = __import__("os").environ.get("TEST_DATABASE_URL", "")

        class links_file:
            pass

    app.state.settings = Cfg()
    app.state.connector = connector
    app.state.mailbox = PgMailboxStore(pool)
    app.state.code_store = build_code_store(Cfg(), pool)
    app.state.seen_store = build_seen_store(Cfg(), pool)
    app.include_router(messages_router.router)

    from app.poller import _ingest_once

    yield {
        "app": app,
        "connector": connector,
        "ingest": lambda settle=120: _ingest_once(
            connector, app.state.seen_store, app.state.mailbox, settle, 120
        ),
    }

    with pool.connection() as conn:
        conn.execute(
            "TRUNCATE TABLE pending_items, thread_links, link_codes, seen_cursors CASCADE"
        )
        conn.commit()


def _link_client(env, code="123456"):
    from fastapi.testclient import TestClient

    from app.connectors.base import LinkDirective

    env["app"].state.code_store.register(code)
    env["app"].state.code_store.attempt_bind(
        LinkDirective(thread_key="42", code=code, sender_id="7", username="tester")
    )
    return TestClient(env["app"]), code


def test_ingest_buffers_nothing_until_the_caption_settles(env):
    client, code = _link_client(env)
    # Both messages are younger than the 120s settle window.
    env["ingest"](settle=120)
    r = client.get("/messages/links", headers={"X-API-Key": code})
    assert r.status_code == 200
    assert r.json() == [], "nothing may be delivered while still settling"


def test_settled_reel_and_caption_arrive_as_one_row(env):
    client, code = _link_client(env)
    # Settle window of 0 => both are immediately bufferable.
    env["ingest"](settle=0)

    r = client.get("/messages/links", headers={"X-API-Key": code})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1, "the caption must merge into the bookmark, not add a row"
    row = rows[0]
    assert row["url"] == "https://www.instagram.com/reel/ABC123/"
    assert row["customDescription"] == "best pizza #food"
    assert row["tags"] == ["food"]
    assert row["author"] == "chef_pizza"
    assert row["image"] == "https://example.com/preview.jpg"
    assert row["timestamp"] == int((NOW - 30) * 1000)


def test_delivery_is_consuming(env):
    client, code = _link_client(env)
    env["ingest"](settle=0)
    first = client.get("/messages/links", headers={"X-API-Key": code}).json()
    second = client.get("/messages/links", headers={"X-API-Key": code}).json()
    assert len(first) == 1
    assert second == [], "a repeat poll must not repeat the bookmark"


def test_one_listing_covers_every_thread(env):
    """D-018: ingest must not re-list the inbox once per user."""
    from app.connectors.base import LinkDirective

    code_store = env["app"].state.code_store

    env["connector"]._threads["43"] = [_reel_msg("n1", NOW - 300)]
    for code, tid in (("111111", "42"), ("222222", "43")):
        code_store.register(code)
        code_store.attempt_bind(
            LinkDirective(thread_key=tid, code=code, sender_id="7", username="t")
        )

    before = env["connector"].listings
    env["ingest"](settle=0)
    assert env["connector"].listings - before == 1, "one inbox listing per cycle"


def test_unlinked_thread_is_never_buffered(env):
    env["connector"]._threads["99"] = [_reel_msg("x1", NOW - 300)]
    client, code = _link_client(env)
    env["ingest"](settle=0)
    rows = client.get("/messages/links", headers={"X-API-Key": code}).json()
    assert all(r["url"] != "x1" for r in rows)
    assert env["app"].state.mailbox.depth("99") == 0


def test_threads_do_not_leak_into_each_other(env):
    from app.connectors.base import LinkDirective

    client, code = _link_client(env)  # binds thread 42 -> code
    env["connector"]._threads["43"] = [_reel_msg("n1", NOW - 300, shortcode="XYZ999")]
    env["app"].state.code_store.register("222222")
    env["app"].state.code_store.attempt_bind(
        LinkDirective(thread_key="43", code="222222", sender_id="7", username="t")
    )
    env["ingest"](settle=0)

    a = client.get("/messages/links", headers={"X-API-Key": code}).json()
    b = client.get("/messages/links", headers={"X-API-Key": "222222"}).json()

    assert [r["url"] for r in a] == ["https://www.instagram.com/reel/ABC123/"]
    # Thread 43's own reel is a different URL; what matters is that 42's row
    # never appears in 43's response.
    assert all("ABC123" not in r["url"] for r in b)
    assert env["app"].state.mailbox.depth("43") == 0, "43 was drained by its own code"
    assert len(a) == 1 and len(b) == 1, "each thread received only its own row"