"""Tests for the link-code lifecycle (auth.py CodeStore)."""

from pathlib import Path

from fastapi import HTTPException
import pytest

from app.auth import CodeStore
from app.connectors.base import LinkDirective


@pytest.fixture
def store(tmp_path: Path) -> CodeStore:
    return CodeStore(tmp_path / "links.json", ttl_seconds=600)


def directive(thread: str, code: str, sender: str = "111", username: str = "alice") -> LinkDirective:
    return LinkDirective(
        thread_key=thread,
        code=code,
        sender_id=sender,
        username=username,
    )


def test_register_then_bind_and_resolve(store: CodeStore):
    store.register("123456")
    assert store.status("123456")["status"] == "pending"

    assert store.attempt_bind(directive("threadA", "123456", "111", "alice")) == "bound"

    account = store.resolve("123456")
    assert account is not None
    assert account.thread_id == "threadA"
    assert account.username == "alice"
    assert store.status("123456")["status"] == "linked"
    assert store.status("123456")["expiresAt"] is None


def test_resolve_rejects_pending_and_unknown(store: CodeStore):
    assert store.resolve("999999") is None
    store.register("123456")
    assert store.resolve("123456") is None  # pending, not linked


def test_first_touch_wins(store: CodeStore):
    store.register("123456")
    assert store.attempt_bind(directive("threadA", "123456", "111", "alice")) == "bound"
    # a different thread tries the same code -> rejected
    assert store.attempt_bind(directive("threadB", "123456", "222", "bob")) == "already_linked"
    assert store.resolve("123456").thread_id == "threadA"


def test_thread_relinks_to_a_new_pending_code(store: CodeStore):
    store.register("111111")
    assert store.attempt_bind(directive("threadA", "111111", "1", "alice")) == "bound"
    # the SAME thread DMs a fresh pending code -> takeover (rebound)
    store.register("222222")
    assert store.attempt_bind(directive("threadA", "222222", "1", "alice")) == "rebound"
    assert store.resolve("222222") is not None
    assert store.resolve("222222").thread_id == "threadA"
    # the old code was revoked and can no longer authenticate
    assert store.resolve("111111") is None
    assert store.status("111111")["status"] == "unlinked"


def test_unknown_and_expired_are_ignored(store: CodeStore):
    # unregistered code -> silently ignored
    assert store.attempt_bind(directive("threadC", "000000", "333", "carl")) == "unknown"

    store.register("123456")
    store._data["codes"]["123456"]["expiresAt"] = 1  # force expiry (now is seconds->ms)
    assert store.status("123456")["status"] == "expired"
    assert store.attempt_bind(directive("threadC", "123456", "333", "carl")) == "expired"
    assert store.resolve("123456") is None


def test_register_linked_code_conflicts(store: CodeStore):
    store.register("123456")
    store.attempt_bind(directive("threadA", "123456", "111", "alice"))
    with pytest.raises(HTTPException) as exc:
        store.register("123456")
    assert exc.value.status_code == 409


def test_persists_across_reload(tmp_path: Path):
    path = tmp_path / "links.json"
    first = CodeStore(path, ttl_seconds=600)
    first.register("123456")
    first.attempt_bind(directive("threadA", "123456", "111", "alice"))

    second = CodeStore(path, ttl_seconds=600)
    account = second.resolve("123456")
    assert account is not None
    assert account.thread_id == "threadA"


def test_clear(store: CodeStore):
    store.register("123456")
    store.attempt_bind(directive("threadA", "123456", "111", "alice"))
    assert store.clear("123456") is True
    assert store.resolve("123456") is None
    assert store.clear("999999") is False