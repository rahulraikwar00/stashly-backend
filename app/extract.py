"""Fetch a URL server-side and extract og/twitter metadata with regex parsing.

Tier-1 extractor (no headless browser). The key trick for walled platforms such
as Instagram/TikTok/Pinterest is the crawler User-Agent: plain browser-like UAs
receive a consent/JS shell with no meta tags, while crawler UAs receive the full
statically-rendered HTML including og:title / og:description / og:image.
"""

import re
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .guards import validate_public_url

_TIMEOUT_DEFAULT_MS = 12000
_MAX_TITLE = 500
_MAX_DESCRIPTION = 1000
_MAX_SITE = 200
_MAX_AUTHOR = 200

# ─────────────────────────────────────────────
# HTML utilities
# ─────────────────────────────────────────────

_NAMED_ENTITIES = {
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "apos": "'",
    "nbsp": " ",
    "hellip": "…",
    "mdash": "—",
    "ndash": "–",
    "rsquo": "’",
    "lsquo": "‘",
    "rdquo": "”",
    "ldquo": "“",
}

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)


def decode_entities(value: str | None) -> str:
    if not value:
        return value or ""
    def _replace(match: re.Match) -> str:
        entity = match.group(1)
        if entity.startswith("#"):
            core = entity[2:] if len(entity) > 2 and entity[1] in "xX" else entity[1:]
            base = 16 if len(entity) > 2 and entity[1] in "xX" else 10
            try:
                return chr(int(core, base))
            except (ValueError, OverflowError):
                return match.group(0)
        return _NAMED_ENTITIES.get(entity.lower(), match.group(0))

    return re.sub(r"&(#x?[0-9a-f]+|[a-z]+);", _replace, value)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _read_meta(html: str, values: list[str]) -> str | None:
    for match in _META_TAG_RE.finditer(html):
        tag = match.group(0)
        if not any(
            re.search(rf'(?:property|name|itemprop|http-equiv)\s*=\s*["\']{v}["\']', tag, re.IGNORECASE)
            for v in values
        ):
            continue
        content = re.search(r'(?:content|href|value)\s*=\s*["\']([^"\']*)["\']', tag, re.IGNORECASE)
        if content and content.group(1).strip():
            return content.group(1).strip()
    return None


def _read_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.IGNORECASE)
    return clean_text(decode_entities(match.group(1))) if match else None


def _read_html_lang(html: str) -> str | None:
    match = re.search(r'<html[^>]*\slang\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
    return match.group(1).lower().split("-")[0] if match else None


def _to_absolute(base: str, value: str | None) -> str | None:
    if not value or re.match(r"^(data|blob):", value, re.IGNORECASE):
        return None
    try:
        return urljoin(base, value)
    except ValueError:
        return None


def _first_http_url(base: str, values: list[str | None]) -> str | None:
    for value in values:
        absolute = _to_absolute(base, value)
        if absolute and re.match(r"^https?://", absolute, re.IGNORECASE):
            return absolute
    return None


def _to_timestamp(value: str | None) -> int | None:
    if not value:
        return None
    if value.isdigit():
        return int(value)
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _map_type(raw: str | None) -> str:
    t = (raw or "").lower()
    if "video" in t:
        return "video"
    if "image" in t:
        return "image"
    if "article" in t or "news" in t or "blog" in t:
        return "article"
    return "link"


def favicon_url(domain: str) -> str:
    return f"https://www.google.com/s2/favicons?domain={quote(domain)}&sz=64" if domain else ""


# ─────────────────────────────────────────────
# User-agent strategy
# ─────────────────────────────────────────────

_CRAWLER_UA = "Googlebot/2.1 (+http://www.google.com/bot.html)"
_FB_CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

_GOOGLEBOT_HOSTS = ("instagram.com", "tiktok.com", "pinterest.com")
_FACEBOOKBOT_HOSTS = ("facebook.com", "snapchat.com")


def _user_agent_for(url: str) -> str:
    hostname = (urlsplit(url).hostname or "").lower()
    for suffix in _GOOGLEBOT_HOSTS:
        if hostname == suffix or hostname.endswith("." + suffix):
            return _CRAWLER_UA
    for suffix in _FACEBOOKBOT_HOSTS:
        if hostname == suffix or hostname.endswith("." + suffix):
            return _FB_CRAWLER_UA
    return _BROWSER_UA


# ─────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────


def extract_metadata(url: str, timeout_ms: int = _TIMEOUT_DEFAULT_MS) -> dict:
    validate_public_url(url)  # double safeguard before any network I/O

    headers = {"User-Agent": _user_agent_for(url), "Accept": _ACCEPT}
    timeout = max(1.0, timeout_ms / 1000.0)

    with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
        response = client.get(url)
        response.raise_for_status()
        final_url = str(response.url)
        html = response.text

    parts = urlsplit(final_url)
    domain = (parts.hostname or "").lower()

    og_type = _read_meta(html, ["og:type"])
    image = _first_http_url(
        final_url,
        [
            decode_entities(_read_meta(html, ["og:image", "og:image:url", "twitter:image", "thumbnail"])),
            decode_entities(_read_meta(html, ["og:image:secure_url"])),
        ],
    )
    published = _to_timestamp(
        _read_meta(html, ["article:published_time", "og:article:published_time", "date", "datePublished"])
    )

    title = clean_text(
        decode_entities(_read_meta(html, ["og:title", "twitter:title"]) or _read_title(html) or domain)
    )[:_MAX_TITLE]
    description = clean_text(
        decode_entities(_read_meta(html, ["og:description", "twitter:description", "description"]) or "")
    )[:_MAX_DESCRIPTION]
    site_name = clean_text(decode_entities(_read_meta(html, ["og:site_name", "application-name"]) or domain))[
        :_MAX_SITE
    ]
    author = clean_text(decode_entities(_read_meta(html, ["author", "article:author"]) or ""))[:_MAX_AUTHOR]
    language = (
        _read_html_lang(html)
        or (_read_meta(html, ["language", "content-language"]) or "").lower().split("-")[0]
        or ""
    )

    return {
        "status": 200,
        "url": url,
        "canonicalUrl": final_url,
        "title": title,
        "description": description,
        "image": image or "",
        "favicon": favicon_url(domain),
        "siteName": site_name,
        "author": author,
        "publishedAt": published,
        "language": language,
        "type": _map_type(og_type),
    }