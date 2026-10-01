from fastapi import APIRouter, Depends, Query

from ..auth import LinkedAccount, require_code
from ..dependencies import get_connector, get_mailbox
from ..models import DebugResetResponse
from ..state import get_seen_store

router = APIRouter(prefix="/debug", tags=["debug"])


@router.post("/reset", response_model=DebugResetResponse)
def reset(
    account: LinkedAccount = Depends(require_code),
    seen=Depends(get_seen_store),
    mailbox=Depends(get_mailbox),
) -> dict:
    """Clear YOUR thread's seen-state AND its mailbox, so the next
    /messages/links call treats everything in it as new again.

    Both are required: clearing only the seen cursor would leave already-buffered
    rows behind, and the poller would re-ingest them on top of what the app is
    about to receive.
    """
    seen.clear(account.thread_id)
    dropped = 0
    if mailbox is not None:
        dropped = mailbox.clear(account.thread_id)
    return {
        "status": "seen_state cleared",
        "thread": account.thread_id,
        "buffered_dropped": dropped,
    }


@router.get("/raw")
def raw(
    count: int = Query(5, ge=1, le=50),
    account: LinkedAccount = Depends(require_code),
    connector=Depends(get_connector),
) -> dict:
    """Raw dump of YOUR linked thread's most recent messages (diagnostics only)."""
    return connector.raw_dump(account.thread_id, count)