from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..auth import LinkedAccount, get_code_store, require_code
from ..config import Settings
from ..dependencies import get_connector, get_mailbox, get_settings
from ..enrich import enrich
from ..models import BookmarkResponse
from ..state import get_seen_store

router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("", response_model=list[BookmarkResponse])
def peek(
    account: LinkedAccount = Depends(require_code),
    connector=Depends(get_connector),
    seen=Depends(get_seen_store),
    settings: Settings = Depends(get_settings),
) -> list[BookmarkResponse]:
    """Read-only peek at what's new in YOUR linked thread. Never consumes."""
    result = connector.fetch_new(account.thread_id, seen.get(account.thread_id))
    links, texts = enrich(result.items, settings.caption_window_seconds)
    out = links + texts
    out.sort(key=lambda b: b.timestamp or 0)
    return out


@router.get("/links", response_model=list[BookmarkResponse])
def links(
    account: LinkedAccount = Depends(require_code),
    connector=Depends(get_connector),
    seen=Depends(get_seen_store),
    code_store=Depends(get_code_store),
    settings: Settings = Depends(get_settings),
    mailbox=Depends(get_mailbox),
) -> list[BookmarkResponse]:
    """THE real endpoint: new forwarded reels for your thread, full bookmark
    shape, and consumes them (won't repeat).

    With a mailbox configured (D-018) this is a transactional drain of the
    poller's buffer: no Instagram call, no seen-cursor mutation, ~5 ms. The
    poller ingests once per cycle and advances its own cursor, so N devices
    pulling on refresh no longer mean N instagrapi round-trips.

    Without one — local development, no DATABASE_URL — it falls back to the
    original live fetch so the JSON setup keeps working unchanged.
    """
    if mailbox is not None:
        return [BookmarkResponse(**row) for row in
                mailbox.drain(account.thread_id, settings.mailbox_drain_limit)]

    result = connector.fetch_new(account.thread_id, seen.get(account.thread_id))
    rows, _ = enrich(result.items, settings.caption_window_seconds)
    if result.items and result.cursor:
        seen.set(account.thread_id, result.cursor)
    rows.sort(key=lambda b: b.timestamp or 0)
    return rows


@router.get("/history", response_model=list[BookmarkResponse])
def history(
    username: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    type: Optional[str] = Query(None),
    account: LinkedAccount = Depends(require_code),
    connector=Depends(get_connector),
    seen=Depends(get_seen_store),
    settings: Settings = Depends(get_settings),
) -> list[BookmarkResponse]:
    """Look back through YOUR linked thread. Never consumes.

    `username` filters by the DM sender; `type` filters by the response type
    (`video`/`image`/`link`, or `text` for standalone text DMs).
    """
    items = connector.history(account.thread_id, limit)
    if username:
        wanted = username.lower()
        items = [i for i in items if i.username.lower() == wanted]
    links, texts = enrich(items, settings.caption_window_seconds)
    out = links + texts
    if type:
        out = [b for b in out if b.type == type]
    out.sort(key=lambda b: b.timestamp or 0, reverse=True)
    return out[:limit]