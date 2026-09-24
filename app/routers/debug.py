from fastapi import APIRouter, Depends, Query

from ..auth import LinkedAccount, require_code
from ..dependencies import get_connector
from ..models import DebugResetResponse
from ..state import get_seen_store

router = APIRouter(prefix="/debug", tags=["debug"])


@router.post("/reset", response_model=DebugResetResponse)
def reset(
    account: LinkedAccount = Depends(require_code),
    seen=Depends(get_seen_store),
) -> dict:
    """Clear YOUR thread's seen-state so the next /messages/links call treats
    everything in it as new again."""
    seen.clear(account.thread_id)
    return {"status": "seen_state cleared", "thread": account.thread_id}


@router.get("/raw")
def raw(
    count: int = Query(5, ge=1, le=50),
    account: LinkedAccount = Depends(require_code),
    connector=Depends(get_connector),
) -> dict:
    """Raw dump of YOUR linked thread's most recent messages (diagnostics only)."""
    return connector.raw_dump(account.thread_id, count)