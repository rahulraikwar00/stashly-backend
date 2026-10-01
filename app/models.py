"""Pydantic request/response models exposed by the API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BookmarkResponse(BaseModel):
    """A full, ready-to-save bookmark — the exact shape the app's `insertBookmark`
    expects. Every text field is "" (never null), timestamps are epoch ms, and
    `urlHash` uses the app's djb2 function so cross-tier dedup holds."""

    id: str = ""
    url: str
    urlHash: str = ""
    domain: str = ""
    path: str = ""
    shortcode: str = ""
    title: str = ""
    description: str = ""
    image: str = ""
    favicon: str = "https://instagram.com/favicon.ico"
    siteName: str = "Instagram"
    author: str = ""
    publishedAt: int | None = None
    language: str = ""
    type: str = "link"
    mediaType: str | None = None
    tags: list[str] = Field(default_factory=list)
    notes: str = ""
    isFavorite: bool = False
    isArchived: bool = False
    isRead: bool = False
    customTitle: str = ""
    customDescription: str = ""
    username: str = ""
    timestamp: int | None = None


class CodeRegisterRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class CodeRegisterResponse(BaseModel):
    code: str
    status: str = "pending"
    expiresAt: int | None = None  # epoch ms


class AuthStatusResponse(BaseModel):
    code: str
    status: str  # "pending" | "linked"
    linked: bool
    username: str = ""
    threadId: str = ""
    expiresAt: int | None = None  # epoch ms


class AuthUnlinkResponse(BaseModel):
    code: str
    status: str = "unlinked"


class DebugResetResponse(BaseModel):
    status: str
    thread: str


class ServerConfigResponse(BaseModel):
    """Public server identity — the official account the user must DM.

    Served instead of hardcoded in the client, which is how the handle drifted
    between IG_USERNAME and the app more than once. `igUsername` is "" when
    Instagram is not configured, never null, so the client can always render a
    fallback rather than "@None".
    """

    igUsername: str = ""
    linkCommand: str = "/link"