"""Regression: the full /messages dependency chain must never 422.

This boots a hermetic FastAPI app with the real routers and a stub connector,
then hits /messages/links with a linked code. It guards the DI wiring (e.g. an
untyped `request` param in a dependency becomes a required query param and
breaks every request past auth) and the BookmarkResponse contract.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.auth import CodeStore
from app.config import load_settings
from app.connectors.base import Connector, FetchResult, InboundItem, LinkDirective
from app.enrich import url_hash
from app.routers import messages as messages_router
from app.state import SeenStore

CODE = "123456"
THREAD = "threadA"
REEL = "https://www.instagram.com/reel/AbC123XYZ/"


class StubConnector(Connector):
    platform = "stub"

    def prepare(self) -> None:
        pass

    def fetch_new(self, thread_key: str | None, cursor: str | None) -> FetchResult:
        return FetchResult(
            items=[
                InboundItem(
                    id="m1",
                    thread_key=THREAD,
                    sender_id="42",
                    username="alice",
                    type="link",
                    content=REEL,
                    timestamp=1_000.0,
                    preview_url="https://cdn.example/preview.jpg",
                    author="creator",
                    media_type="reel",
                    shortcode="AbC123XYZ",
                )
            ],
            cursor="m1",
        )

    def history(self, thread_key: str | None, limit: int) -> list[InboundItem]:
        return []

    def scan_for_link_directives(self) -> list[LinkDirective]:
        return []


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.state.settings = load_settings()
    app.state.connector = StubConnector()
    app.state.code_store = CodeStore(tmp_path / "links.json", ttl_seconds=600)
    app.state.seen_store = SeenStore(tmp_path / "seen.json")
    app.include_router(messages_router.router)

    store = app.state.code_store
    store.register(CODE)
    assert store.attempt_bind(LinkDirective(THREAD, CODE, "42", "alice")) == "bound"

    return TestClient(app)


def test_links_returns_ok_for_linked_code(client: TestClient):
    res = client.get("/messages/links", headers={"X-API-Key": CODE})
    assert res.status_code == 200, res.text  # was 422: missing query 'request'
    body = res.json()
    assert isinstance(body, list)
    assert len(body) == 1
    row = body[0]
    assert row["url"] == REEL
    assert row["urlHash"] == url_hash(REEL)
    assert row["type"] == "video"
    assert row["username"] == "alice"


def test_peek_returns_ok_for_linked_code(client: TestClient):
    res = client.get("/messages", headers={"X-API-Key": CODE})
    assert res.status_code == 200, res.text
    assert isinstance(res.json(), list)


def test_links_rejects_unlinked_code(client: TestClient):
    res = client.get("/messages/links", headers={"X-API-Key": "000000"})
    assert res.status_code == 401