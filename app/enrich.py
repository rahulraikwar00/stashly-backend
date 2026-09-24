"""Normalize raw inbound messages into the app's bookmark shape.

This module is platform-agnostic: it works purely on `InboundItem`
(`app/connectors/base.py`), so every connector produces identical
`BookmarkResponse` rows. The response contract lives in
`my-expo-app/docs/03-API-Contract.md` (§3.3) and the D-016 decision.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .connectors.base import InboundItem
from .models import BookmarkResponse

# ─────────────────────────────────────────────
# djb2 urlHash — MUST match utils/hash.ts `urlHashFor`
# ─────────────────────────────────────────────

def djb2_hex(value: str) -> str:
    """Port of `urlHashFor` from the app (trim + lowercase + djb2, hex).

    Verified against Node's `((h << 5) + h + code) >>> 0` semantics: the
    per-iteration mask to 32 bits makes the unbounded Python arithmetic
    identical to JS's uint32 wrap.
    """
    h = 5381
    for ch in value:
        h = (((h << 5) + h) + ord(ch)) & 0xFFFFFFFF
    return format(h, "x")


def url_hash(url: str) -> str:
    return djb2_hex(url.strip().lower())


# ─────────────────────────────────────────────
# URL / media parsing
# ─────────────────────────────────────────────

_MEDIA_URL_RE = re.compile(r"instagram\.com/(reel|p|tv)/([^/?]+)")
_MEDIA_TYPE_FROM_SLUG = {"reel": "reel", "p": "post", "tv": "igtv"}
_APP_TYPE_FROM_MEDIA = {"reel": "video", "igtv": "video", "post": "image"}


def domain_and_path(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return (parts.hostname or "").lower(), parts.path or ""


def parse_media_type_and_code(url: str) -> tuple[str | None, str | None]:
    """From an instagram.com URL return (media_type, shortcode)."""
    m = _MEDIA_URL_RE.search(url or "")
    if not m:
        return None, None
    return _MEDIA_TYPE_FROM_SLUG.get(m.group(1)), m.group(2)


def app_type(media_type: str | None) -> str:
    """Map a raw IG media type to the app's BookmarkType union."""
    return _APP_TYPE_FROM_MEDIA.get((media_type or "").lower(), "link")


# ─────────────────────────────────────────────
# Tags
# ─────────────────────────────────────────────

_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)


def extract_tags(text: str | None) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _HASHTAG_RE.findall(text):
        tag = match.lower()
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


# ─────────────────────────────────────────────
# Caption merging (120s, same sender, concatenation)
# ─────────────────────────────────────────────

def _within_window(a: float | None, b: float | None, window: float) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= window


def pair_captions(
    items: list[InboundItem], window: float
) -> tuple[dict[int, str], set[int]]:
    """Pair captions with links.

    Returns ({link_index: merged_caption}, consumed_text_indices).

    A text message from the SAME sender within `window` seconds of the link
    (before or after) is folded into the link's caption. Multiple texts
    concatenate with a space; a claimed text is never emitted separately.
    `items` must be oldest-first.
    """
    consumed: set[int] = set()
    captions: dict[int, str] = {}
    n = len(items)

    for i, item in enumerate(items):
        if item.type != "link":
            continue
        parts: list[tuple[int, str]] = []
        for j in (i - 1, i + 1):
            if 0 <= j < n and j not in consumed:
                neighbor = items[j]
                if (
                    neighbor.type == "text"
                    and neighbor.sender_id == item.sender_id
                    and _within_window(item.timestamp, neighbor.timestamp, window)
                ):
                    parts.append((j, neighbor.content))
                    consumed.add(j)
        if parts:
            parts.sort(key=lambda t: t[0])
            captions[i] = " ".join(content for _, content in parts)

    return captions, consumed


def _item_timestamp(item: InboundItem) -> int | None:
    return int(item.timestamp * 1000) if item.timestamp is not None else None


def build_response(item: InboundItem, caption: str | None) -> BookmarkResponse:
    """A full bookmark row from a link-type inbound item."""
    url = (item.content or "").split("?")[0]

    media_type, shortcode = item.media_type, item.shortcode
    if not shortcode and url:
        slug_type, code = parse_media_type_and_code(url)
        media_type = media_type or slug_type
        shortcode = code

    domain, path = domain_and_path(url)
    merged = caption or ""

    return BookmarkResponse(
        id=item.id,
        url=url,
        urlHash=url_hash(url),
        domain=domain,
        path=path,
        shortcode=shortcode or "",
        image=item.preview_url or "",
        author=item.author or "",
        type=app_type(media_type),
        mediaType=media_type,
        tags=extract_tags(merged),
        customDescription=merged,
        username=item.username,
        timestamp=_item_timestamp(item),
    )


def text_response(item: InboundItem) -> BookmarkResponse:
    """A passthrough row for standalone text DMs (no URL, not dedupable)."""
    return BookmarkResponse(
        id=item.id,
        url="",
        domain="",
        path="",
        favicon="",
        siteName="",
        type="text",
        mediaType=None,
        tags=extract_tags(item.content or ""),
        customDescription=item.content or "",
        username=item.username,
        timestamp=_item_timestamp(item),
    )


def enrich(
    items: list[InboundItem], window: float
) -> tuple[list[BookmarkResponse], list[BookmarkResponse]]:
    """Return (link_bookmarks, standalone_text_bookmarks), oldest→newest."""
    captions, consumed = pair_captions(items, window)

    links: list[BookmarkResponse] = []
    standalone: list[BookmarkResponse] = []
    for i, item in enumerate(items):
        if item.type == "link":
            links.append(build_response(item, captions.get(i)))
        elif i not in consumed:
            standalone.append(text_response(item))

    return links, standalone