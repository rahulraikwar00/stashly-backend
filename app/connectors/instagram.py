"""Instagram connector: instagrapi over the `Connector` ABC.

Owns the official-account session, thread fetching, burst fallback, and
`/link <code>` directive scanning. Produces normalized `InboundItem`s only —
all bookmark shaping happens in `app/enrich.py`.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any

from instagrapi import Client
from instagrapi.exceptions import LoginRequired

from .base import Connector, FetchResult, InboundItem, LinkDirective

log = logging.getLogger("connector.instagram")

_LINK_RE = re.compile(r"^/link\s+(\d{6})\b", re.IGNORECASE)


def _share_info(message: Any) -> tuple[str, str, str, str]:
    """Return (kind, content, preview_url, author) for a raw instagrapi message.

    Mirrors the old `extract_content` ladder, plus preview/author from
    `xma_share` for the bookmark image + author fields.
    """
    item_type = getattr(message, "item_type", None)

    xma_share = getattr(message, "xma_share", None)
    if xma_share:
        video_url = getattr(xma_share, "video_url", None)
        if video_url:
            preview = getattr(xma_share, "preview_url", None) or ""
            author = (getattr(xma_share, "header_title_text", None)
                      or "").strip()
            return "link", video_url.split("?")[0], preview, author

    media_share = getattr(message, "media_share", None)
    if item_type in ("media_share", "xma_media_share") and media_share:
        code = getattr(media_share, "code", None)
        if code:
            return "link", f"https://www.instagram.com/p/{code}/", "", ""

    clip = getattr(message, "clip", None)
    if item_type == "clip" and clip and getattr(clip, "clip", None):
        code = getattr(clip.clip, "code", None)
        if code:
            return "link", f"https://www.instagram.com/reel/{code}/", "", ""

    reel_share = getattr(message, "reel_share", None)
    if item_type == "reel_share" and reel_share and getattr(reel_share, "media", None):
        code = getattr(reel_share.media, "code", None)
        if code:
            return "link", f"https://www.instagram.com/reel/{code}/", "", ""

    felix_share = getattr(message, "felix_share", None)  # IGTV
    if item_type == "felix_share" and felix_share and getattr(felix_share, "video", None):
        code = getattr(felix_share.video, "code", None)
        if code:
            return "link", f"https://www.instagram.com/tv/{code}/", "", ""

    link = getattr(message, "link", None)
    if item_type == "link" and link:
        link_context = getattr(link, "link_context", None)
        url = getattr(link_context, "link_url", None) if link_context else None
        if url:
            return "link", url, "", ""

    text = getattr(message, "text", None)
    if text:
        return "text", text, "", ""

    return "unknown", f"[unrecognized message type: item_type={item_type!r}]", "", ""


class InstagramConnector(Connector):
    platform = "instagram"

    def __init__(
        self,
        username: str,
        password: str,
        session_file: Path,
        threads_per_fetch: int = 10,
        thread_message_limit: int = 40,
        burst_fetch_limit: int = 200,
    ) -> None:
        self._username = username
        self._password = password
        self._session_file = session_file
        self._threads_per_fetch = threads_per_fetch
        self._thread_message_limit = thread_message_limit
        self._burst_fetch_limit = burst_fetch_limit
        self._client: Client | None = None
        self._lock = threading.RLock()

    # ── session / auth ─────────────────────────────────────────────

    def _fresh_client(self) -> Client:
        """Login from scratch (ignore a cached session) and persist it."""
        client = Client()
        client.login(self._username, self._password)
        client.dump_settings(self._session_file)
        log.warning("Instagram: logged in fresh and cached session.")
        return client

    def _ensure_client(self) -> Client | None:
        with self._lock:
            if self._client is not None:
                return self._client
            try:
                if self._session_file.exists():
                    cached = Client()
                    cached.load_settings(self._session_file)
                    cached.login(self._username, self._password)
                    cached.get_timeline_feed()  # cheap call proving the session works
                    self._client = cached
                    log.warning("Instagram: reused cached session.")
                    return cached
                self._client = self._fresh_client()
                return self._client
            except LoginRequired:
                log.warning(
                    "Instagram: cached session expired, logging in fresh.")
                self._client = self._fresh_client()
                return self._client
            except Exception:
                log.exception("Instagram: login failed")
                self._client = None
                return None

    def _threads(self, client: Client) -> list[Any]:
        try:
            return list(
                client.direct_threads(
                    amount=self._threads_per_fetch,
                    thread_message_limit=self._thread_message_limit,
                )
            )
        except LoginRequired:
            log.warning("Instagram: session expired mid-fetch, re-logging in.")
            self._client = self._fresh_client()
            return list(
                self._client.direct_threads(
                    amount=self._threads_per_fetch,
                    thread_message_limit=self._thread_message_limit,
                )
            )

    def prepare(self) -> None:
        self._ensure_client()

    # ── normalization ──────────────────────────────────────────────

    def _items_from(self, messages: list[Any], users: list[Any], thread_key: str) -> list[InboundItem]:
        items: list[InboundItem] = []
        for m in messages:
            kind, content, preview, author = _share_info(m)
            sender_id = str(getattr(m, "user_id", ""))
            username = next(
                (u.username for u in users if u.pk == m.user_id), sender_id)
            ts = None
            try:
                ts = m.timestamp.timestamp()
            except Exception:
                ts = None
            items.append(
                InboundItem(
                    id=m.id,
                    thread_key=thread_key,
                    sender_id=sender_id,
                    username=username,
                    type=kind,
                    content=content,
                    timestamp=ts,
                    preview_url=preview,
                    author=author,
                )
            )
        return items

    def raw_dump(self, thread_key: str | None, count: int = 5) -> dict:
        if not thread_key:
            return {"thread_id": "", "messages": []}
        with self._lock:
            client = self._ensure_client()
            if client is None:
                return {"thread_id": thread_key, "messages": []}
            threads = self._threads(client)
            thread = next(
                (t for t in threads if str(t.id) == thread_key), None)
            if thread is None:
                return {"thread_id": thread_key, "messages": []}
            raw = [m.model_dump() for m in thread.messages[:count]]
            return {"thread_id": thread_key, "messages": raw}

    # ── Connector API ──────────────────────────────────────────────

    def fetch_new(self, thread_key: str | None, cursor: str | None) -> FetchResult:
        if not thread_key:
            log.warning("Instagram: fetch requested without a linked thread")
            return FetchResult()

        with self._lock:
            client = self._ensure_client()
            if client is None:
                raise RuntimeError("Instagram client is not configured")

            threads = self._threads(client)
            thread = next(
                (t for t in threads if str(t.id) == thread_key), None)
            if thread is None or not thread.messages:
                log.warning(
                    "Instagram: linked thread %s not in the inbox batch", thread_key)
                return FetchResult()

            raw = thread.messages  # newest-first

            new_raw: list[Any] = []
            boundary = None
            for m in raw:
                if cursor and m.id == cursor:
                    boundary = m
                    break
                new_raw.append(m)

            # Burst fallback: boundary not within the inline batch.
            if boundary is None and cursor:
                log.warning(
                    "Instagram: burst detected in thread %s, fetching deeper.", thread_key)
                deeper = client.direct_messages(
                    thread_key, amount=self._burst_fetch_limit)
                new_raw = []
                boundary = None
                for m in deeper:
                    if m.id == cursor:
                        boundary = m
                        break
                    new_raw.append(m)
                raw = deeper or raw

            if not new_raw:
                return FetchResult()

            context = [m for m in reversed(
                new_raw) if m.user_id != client.user_id]
            if boundary is not None:
                context = [boundary] + context

            items = self._items_from(context, thread.users, thread_key)
            if boundary is not None:
                items = [i for i in items if i.id != boundary.id]

            cursor_new = raw[0].id if raw else None
            return FetchResult(items=items, cursor=cursor_new)

    def history(self, thread_key: str | None, limit: int) -> list[InboundItem]:
        if not thread_key:
            return []

        with self._lock:
            client = self._ensure_client()
            if client is None:
                raise RuntimeError("Instagram client is not configured")

            threads = self._threads(client)
            thread = next(
                (t for t in threads if str(t.id) == thread_key), None)
            if thread is None:
                return []

            raw = thread.messages
            if len(raw) < limit:
                raw = client.direct_messages(thread_key, amount=limit)
            oldest = [m for m in reversed(raw) if m.user_id != client.user_id]
            return self._items_from(oldest, thread.users, thread_key)

    def scan_for_link_directives(self) -> list[LinkDirective]:
        with self._lock:
            client = self._ensure_client()
            if client is None:
                return []

            threads = self._threads(client)
            directives: list[LinkDirective] = []
            own_id = getattr(client, "user_id", None)

            for thread in threads:
                for m in thread.messages or []:
                    if m.user_id == own_id:
                        continue
                    text = (getattr(m, "text", None) or "").strip()
                    match = _LINK_RE.match(text)
                    if not match:
                        continue
                    sender = next(
                        (u.username for u in thread.users if u.pk == m.user_id),
                        str(m.user_id),
                    )
                    directives.append(
                        LinkDirective(
                            thread_key=str(thread.id),
                            code=match.group(1),
                            sender_id=str(m.user_id),
                            username=sender,
                        )
                    )
                    break  # one directive per thread per scan

            return directives
