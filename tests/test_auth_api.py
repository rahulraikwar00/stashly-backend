"""API tests for the /auth router: the express unlink endpoint and the
re-link (takeover) behaviour, both through the real dependency chain."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.auth import CodeStore
from app.config import load_settings
from app.connectors.base import LinkDirective
from app.routers import auth as auth_router

CODE = "123456"
OTHER = "654321"
THREAD = "threadA"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.state.settings = load_settings()
    app.state.code_store = CodeStore(tmp_path / "links.json", ttl_seconds=600)
    app.include_router(auth_router.router)
    return TestClient(app)


def test_unlink_revokes_binding_and_frees_thread(client: TestClient):
    store: CodeStore = client.app.state.code_store
    store.register(CODE)
    assert store.attempt_bind(LinkDirective(THREAD, CODE, "111", "alice")) == "bound"

    res = client.post("/auth/unlink", headers={"X-API-Key": CODE})
    assert res.status_code == 200, res.text
    assert res.json() == {"code": CODE, "status": "unlinked"}

    assert store.resolve(CODE) is None
    assert client.get("/auth/status", headers={"X-API-Key": CODE}).status_code == 404

    # the thread is free again: a fresh code now binds first-touch-wins
    store.register(OTHER)
    assert store.attempt_bind(LinkDirective(THREAD, OTHER, "222", "bob")) == "bound"


def test_unlink_rejects_unknown_code(client: TestClient):
    res = client.post("/auth/unlink", headers={"X-API-Key": "999999"})
    assert res.status_code == 404

    res = client.post("/auth/unlink")
    assert res.status_code == 400


def test_link_endpoint_reflects_takeover(client: TestClient):
    store: CodeStore = client.app.state.code_store
    store.register(CODE)
    assert store.attempt_bind(LinkDirective(THREAD, CODE, "111", "alice")) == "bound"
    store.register(OTHER)
    assert store.attempt_bind(LinkDirective(THREAD, OTHER, "111", "alice")) == "rebound"

    res = client.get("/auth/status", headers={"X-API-Key": OTHER})
    assert res.status_code == 200
    assert res.json()["status"] == "linked"

    # the revoked old code no longer sees a linked account
    assert client.get("/auth/status", headers={"X-API-Key": CODE}).json()["status"] == "unlinked"