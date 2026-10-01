"""API tests for the /auth router: the express unlink endpoint and the
re-link (takeover) behaviour, both through the real dependency chain."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.auth import CodeStore
from app.config import Settings, load_settings
from app.connectors.base import LinkDirective
from app.routers import auth as auth_router

CODE = "123456"
OTHER = "654321"
THREAD = "threadA"


def _client(tmp_path: Path, settings: Settings) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    app.state.code_store = CodeStore(tmp_path / "links.json", ttl_seconds=600)
    app.include_router(auth_router.router)
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return _client(tmp_path, load_settings())


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


def test_register_then_status_returns_pending(client: TestClient):
    """Register a code via POST /auth/codes, then GET /auth/status returns pending."""
    res = client.post("/auth/codes", json={"code": "999999"})
    assert res.status_code == 201
    data = res.json()
    assert data["code"] == "999999"
    assert data["status"] == "pending"
    assert "expiresAt" in data

    # Now check status with the registered code
    res = client.get("/auth/status", headers={"X-API-Key": "999999"})
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == "999999"
    assert data["status"] == "pending"
    assert data["linked"] is False
    assert data["threadId"] == ""
    assert data["username"] == ""
    assert data["expiresAt"] is not None


def test_config_serves_the_configured_handle(tmp_path: Path):
    """The official account comes from the backend, not a client-side constant."""
    client = _client(tmp_path, Settings(ig_username="stashlyhq", ig_password="pw"))
    res = client.get("/auth/config")
    assert res.status_code == 200
    assert res.json() == {
        "igUsername": "stashlyhq",
        "linkCommand": "/link",
        "instagramConfigured": True,
    }


def test_config_needs_no_api_key(tmp_path: Path):
    """Open by design: the app must know where to DM *before* it holds a code."""
    client = _client(tmp_path, Settings(ig_username="stashlyhq", ig_password="pw"))
    assert client.get("/auth/config").status_code == 200


def test_config_handle_is_empty_when_instagram_unconfigured(tmp_path: Path):
    """No creds => empty handle, never None, so the client can render a fallback."""
    res = _client(tmp_path, Settings()).get("/auth/config")
    assert res.status_code == 200
    assert res.json() == {
        "igUsername": "",
        "linkCommand": "/link",
        "instagramConfigured": False,
    }


def test_config_flags_half_configured_instagram(tmp_path: Path):
    """A username without a password cannot log in, so nothing would ever bind.

    `igUsername` alone cannot express this — the client needs the explicit flag
    to say "this server isn't linked to an account" rather than opening a DM that
    the backend will never read.
    """
    res = _client(tmp_path, Settings(ig_username="stashlyhq")).get("/auth/config")
    assert res.status_code == 200
    body = res.json()
    assert body["igUsername"] == "stashlyhq"
    assert body["instagramConfigured"] is False


def test_register_returns_server_identity(tmp_path: Path):
    """The DM target arrives with the code, so no second request is needed."""
    client = _client(tmp_path, Settings(ig_username="stashlyhq", ig_password="pw"))
    res = client.post("/auth/codes", json={"code": "999999"})
    assert res.status_code == 201
    body = res.json()
    assert body["code"] == "999999"
    assert body["igUsername"] == "stashlyhq"
    assert body["linkCommand"] == "/link"
    assert body["instagramConfigured"] is True


def test_register_identity_matches_config_endpoint(tmp_path: Path):
    """Both endpoints must agree, or the restart fallback contradicts Connect."""
    client = _client(tmp_path, Settings(ig_username="stashlyhq", ig_password="pw"))
    registered = client.post("/auth/codes", json={"code": "999999"}).json()
    config = client.get("/auth/config").json()
    for key in ("igUsername", "linkCommand", "instagramConfigured"):
        assert registered[key] == config[key]