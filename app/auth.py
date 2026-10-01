"""Link-code lifecycle + the `require_code` fastapi dependency.

The 6-digit code is a *channel token*: registering one binds nobody. Binding
only happens when a thread DMs `/link <code>` to the official account (see the
poller and `Connector.scan_for_link_directives`), first-touch-wins. DM endpoints
authenticate with the code and are scoped to the bound thread.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.security.utils import get_authorization_scheme_param

from .connectors.base import LinkDirective

_log = logging.getLogger("auth")


@dataclass(frozen=True)
class LinkedAccount:
    code: str
    thread_id: str
    sender_id: str
    username: str


class CodeStore:
    def __init__(self, path: Path, ttl_seconds: int = 600) -> None:
        self._path = path
        self._ttl = ttl_seconds
        self._lock = threading.RLock()
        self._data: dict = {"threads": {}, "codes": {}}
        self._load()

    # ── persistence ────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            self._data = json.loads(self._path.read_text())
        except (FileNotFoundError, ValueError):
            self._data = {"threads": {}, "codes": {}}
        self._data.setdefault("threads", {})
        self._data.setdefault("codes", {})

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self._path)

    # ── code lifecycle ──────────────────────────────────────────────

    def _expired_pending(self, entry: dict) -> bool:
        return (
            entry.get("status") == "pending"
            and entry.get("expiresAt")
            and time.time() * 1000 > entry["expiresAt"]
        )

    def _prune(self) -> None:
        codes = self._data["codes"]
        for code, entry in list(codes.items()):
            if self._expired_pending(entry):
                del codes[code]
        if len(codes) < len(self._data["codes"]):
            self._save()

    def register(self, code: str) -> dict:
        """Register (or refresh) a pending code. Raises on conflict."""
        with self._lock:
            self._prune()
            now_ms = int(time.time() * 1000)
            existing = self._data["codes"].get(code)
            if existing and existing.get("status") == "linked":
                raise HTTPException(status_code=409, detail="Code is already linked.")
            entry = {
                "createdAt": now_ms,
                "status": "pending",
                "expiresAt": now_ms + self._ttl * 1000,
                "threadId": "",
                "senderId": "",
                "username": "",
                "linkedAt": None,
            }
            self._data["codes"][code] = entry
            self._save()
            return {"code": code, "status": "pending", "expiresAt": entry["expiresAt"]}

    def attempt_bind(self, directive: LinkDirective) -> str:
        """Try to bind a `/link <code>` directive. Never raises.

        Returns one of: "bound" | "rebound" | "unknown" | "expired" |
        "already_linked". A thread that DMs a fresh pending code while already
        bound to a different code takes over the thread ("rebound"): the old
        code's binding is revoked and the new code wins. First-touch-wins still
        applies between *unbound* threads competing for the same pending code.
        Everything except "bound"/"rebound" is ignored silently upstream.
        """
        code = directive.code
        with self._lock:
            code_map = self._data["codes"]
            thread_map = self._data["threads"]

            entry = code_map.get(code)
            if entry is None:
                return "unknown"
            if self._expired_pending(entry):
                return "expired"
            if entry.get("status") == "linked":
                return "already_linked"

            bound_thread = thread_map.get(directive.thread_key)
            if bound_thread and bound_thread != code:
                old_entry = code_map.get(bound_thread)
                if old_entry:
                    old_entry.update(
                        {
                            "status": "unlinked",
                            "threadId": "",
                            "senderId": "",
                            "username": "",
                            "linkedAt": None,
                            "expiresAt": None,
                        }
                    )

            now_ms = int(time.time() * 1000)
            entry.update(
                {
                    "status": "linked",
                    "threadId": directive.thread_key,
                    "senderId": directive.sender_id,
                    "username": directive.username,
                    "linkedAt": now_ms,
                    "expiresAt": None,
                }
            )
            thread_map[directive.thread_key] = code
            self._save()
            return "rebound" if bound_thread and bound_thread != code else "bound"

    def resolve(self, code: str) -> LinkedAccount | None:
        """Linked account for a code, or None (unknown/pending/expired)."""
        with self._lock:
            entry = self._data["codes"].get(code)
            if not entry or entry.get("status") != "linked" or not entry.get("threadId"):
                return None
            return LinkedAccount(
                code=code,
                thread_id=entry["threadId"],
                sender_id=entry.get("senderId", ""),
                username=entry.get("username", ""),
            )

    def status(self, code: str) -> dict | None:
        """Public info about a code: pending / linked / expired."""
        with self._lock:
            entry = self._data["codes"].get(code)
            if entry is None:
                return None
            if self._expired_pending(entry):
                return {"code": code, "status": "expired", "linked": False}
            linked = entry.get("status") == "linked"
            return {
                "code": code,
                "status": entry.get("status", "pending"),
                "linked": linked,
                "username": entry.get("username", ""),
                "threadId": entry.get("threadId", ""),
                "expiresAt": entry.get("expiresAt"),
            }

    def clear(self, code: str) -> bool:
        """Remove a code (debug). Returns True when it existed."""
        with self._lock:
            code_map = self._data["codes"]
            entry = code_map.pop(code, None)
            if entry and entry.get("threadId"):
                self._data["threads"].pop(entry["threadId"], None)
            if entry:
                self._save()
            return entry is not None


def get_code_store(request: Request) -> CodeStore:
    return request.app.state.code_store


def build_code_store(settings, pool):
    """Pick the store: Postgres when `DATABASE_URL` is set, else links.json.

    The JSON store stays the local-development default so the suite runs with no
    database configured. Imports are lazy because `app.db.code_store` needs
    `LinkedAccount` from this module.
    """
    if pool is not None:
        from .db.code_store import PgCodeStore

        return PgCodeStore(pool, ttl_seconds=settings.code_ttl_seconds)
    return CodeStore(settings.links_file, ttl_seconds=settings.code_ttl_seconds)


def require_code(
    request: Request,
    code_store: CodeStore = Depends(get_code_store),
) -> LinkedAccount:
    """Auth dependency: resolve `X-API-Key: <code>` or `Bearer <code>` to a
    linked account. 401 otherwise.

    Every failure mode returns a byte-identical 401 — unknown, pending, expired,
    or throttled — so the endpoint never confirms which codes exist. Throttling
    is per code, not per IP (see `app/db/failed_auth.py`).
    """
    denied = HTTPException(
        status_code=401, detail="Invalid or unlinked API code.")

    x_api_key = request.headers.get("x-api-key", "")
    authorization = request.headers.get("authorization", "")
    code = x_api_key.strip()
    if not code and authorization:
        _, value = get_authorization_scheme_param(authorization)
        code = value.strip()

    if not code:
        raise denied

    settings = getattr(request.app.state, "settings", None)
    failed_auth = getattr(request.app.state, "failed_auth", None)
    max_failures = getattr(settings, "auth_max_failures", 5)
    lock_seconds = getattr(settings, "auth_lock_seconds", 900)

    if failed_auth is not None:
        try:
            if failed_auth.is_locked(code):
                raise denied
        except HTTPException:
            raise
        except Exception:
            # Fail open on a throttling-infrastructure error: a database blip must
            # not lock every real user out of their own bookmarks.
            _log.warning("auth throttling unavailable; proceeding unthrottled")

    account = code_store.resolve(code)
    if account is None:
        if failed_auth is not None:
            try:
                failed_auth.record_failure(code, max_failures, lock_seconds)
            except Exception:
                _log.warning("could not record auth failure for code=%s", code)
        raise denied

    if failed_auth is not None:
        try:
            failed_auth.clear(code)
        except Exception:
            pass
    return account