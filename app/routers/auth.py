from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import get_code_store
from ..config import Settings
from ..dependencies import get_settings
from ..models import (
    AuthStatusResponse,
    AuthUnlinkResponse,
    CodeRegisterRequest,
    CodeRegisterResponse,
    ServerConfigResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/config", response_model=ServerConfigResponse)
def server_config(settings: Settings = Depends(get_settings)) -> dict:
    """The official account to DM, and the directive to send it.

    Open by design, for the same reason POST /auth/codes is: the app has to tell
    the user where to send `/link <code>` before it holds any code, so requiring
    auth here would be circular. Exposes only a public handle.
    """
    return {"igUsername": settings.ig_username, "linkCommand": "/link"}


@router.post("/codes", response_model=CodeRegisterResponse, status_code=201)
def register_code(
    body: CodeRegisterRequest,
    code_store=Depends(get_code_store),
) -> dict:
    """Register a fresh 6-digit code for the DM linking flow.

    The code is *pending* until the user DMs the official account with
    `/link <code>`; it expires after `CODE_TTL_SECONDS` (~10 min).
    """
    return code_store.register(body.code)


@router.get("/status", response_model=AuthStatusResponse)
def code_status(
    request: Request,
    code_store=Depends(get_code_store),
) -> dict:
    """Check a code's status (pending / linked / expired).

    The app polls this after nudging the user to send `/link <code>`.
    Accepts the code via `X-API-Key` (also valid while pending).
    """
    code = request.headers.get("x-api-key", "").strip()
    if not code:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header.")
    info = code_store.status(code)
    if info is None:
        raise HTTPException(status_code=404, detail="Code not registered.")
    return info


@router.post("/unlink", response_model=AuthUnlinkResponse, status_code=200)
def unlink_code(
    request: Request,
    code_store=Depends(get_code_store),
) -> dict:
    """Revoke a code and its thread binding (called by the app's "Forget").

    The code is removed entirely, freeing its thread for future `/link` codes.
    Accepts the code via `X-API-Key` (symmetric with /status).
    """
    code = request.headers.get("x-api-key", "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="Missing X-API-Key header.")
    if not code_store.clear(code):
        raise HTTPException(status_code=404, detail="Code not registered.")
    return {"code": code, "status": "unlinked"}