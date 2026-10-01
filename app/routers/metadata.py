"""Metadata extractor endpoint (walled-site og: tag fallback).

Restored from the earlier extractor app: fetches server-side with a crawler
User-Agent so walled platforms (instagram/tiktok/pinterest/youtube) serve their
full og: tags, then regex-parses them. SSRF-guarded in `guards.py`.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from ..extract import _user_agent_for, extract_metadata
from ..guards import validate_public_url

logger = logging.getLogger("metadata-extractor")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s [metadata-extractor] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

router = APIRouter(tags=["metadata"])


def _log_request(started: float, url: str, status: str, result: dict | None) -> None:
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    logger.info(
        "url=%s status=%s host=%s ua=%s elapsed=%sms title=%s description=%s image=%s",
        url,
        status,
        urlsplit(url).hostname or "?",
        _user_agent_for(url),
        elapsed_ms,
        bool(result and result.get("title")),
        bool(result and result.get("description")),
        bool(result and result.get("image")),
    )


@router.get("/metadata")
def metadata(
    url: str = Query(...),
    timeout_ms: int = Query(12000, ge=1000, le=20000),
    debug: bool = Query(False),
    request: Request = None,
) -> dict:
    """Extract page metadata for an arbitrary public http(s) URL.

    Read-through cached when a database is configured (D-018): this is the
    highest-cardinality, slowest, most repeated operation in the service, and the
    only endpoint that could see a traffic spike. A hit costs one primary-key
    lookup instead of a full fetch-and-parse.
    """
    target = validate_public_url(url)
    started = time.perf_counter()

    cache = None
    if request is not None:
        cache = getattr(request.app.state, "metadata_cache", None)

    if cache is not None:
        from ..enrich import url_hash

        try:
            hit = cache.get(url_hash(target))
        except Exception:
            logger.exception("metadata cache read failed; falling through")
            hit = None
        if hit is not None:
            logger.info(
                "url=%s status=cache-hit host=%s elapsed=%sms",
                target,
                urlsplit(target).hostname or "?",
                round((time.perf_counter() - started) * 1000, 1),
            )
            if debug:
                hit = dict(hit)
                hit["trace"] = {"cached": True}
            return hit

    try:
        result = extract_metadata(target, timeout_ms=timeout_ms)
    except httpx.TimeoutException as exc:
        _log_request(started, url, "timeout")
        raise HTTPException(status_code=504, detail=f"Upstream timed out: {url}") from exc
    except httpx.HTTPStatusError as exc:
        _log_request(started, url, f"upstream-{exc.response.status_code}")
        raise HTTPException(
            status_code=502,
            detail=f"Upstream responded with status {exc.response.status_code}: {url}",
        ) from exc
    except httpx.RequestError as exc:
        _log_request(started, url, "request-error")
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}") from exc

    if cache is not None:
        try:
            from ..enrich import url_hash

            cache.put(url_hash(target), target, result)
        except Exception:
            logger.exception("metadata cache write failed; ignoring")

    _log_request(started, url, "ok", result)
    if debug:
        result["trace"] = {
            "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
            "userAgent": _user_agent_for(target),
            "host": urlsplit(target).hostname or "",
            "titlePresent": bool(result.get("title")),
            "descriptionPresent": bool(result.get("description")),
            "imagePresent": bool(result.get("image")),
            "canonicalUrl": result.get("canonicalUrl") or "",
        }
    return result