# Bookmark Backend

FastAPI service behind the Instagram DM → bookmark relay, plus the metadata
extractor the app falls back to on walled sites (Instagram/TikTok/Pinterest).

Architecture and design decisions live in the app repo:

- `my-expo-app/docs/02-Architecture.md` §2.3 — the full flow
- `my-expo-app/docs/03-API-Contract.md` — the wire contract (§3.3 response shape)
- `my-expo-app/DesingDecision/decisions.md` **D-016** — this service's design

## Run

```bash
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env   # set PORT (default 8000) + IG_USERNAME / IG_PASSWORD
.venv/bin/python -m app
```

The serving port comes from `PORT` in `.env` (default 8000); the app's "Self-hosted
server" URL / dev fallback must match it.

`/metadata` and `/health` work with no Instagram credentials. Setting
`IG_USERNAME`/`IG_PASSWORD` enables the relay + link-poller (credentials load
from `.env`, session cached to `app/session.json` — **gitignored, treat it like
a password**).

## Endpoints

| Method | Path | Auth | Purpose | Consumes seen? |
| --- | --- | --- | --- | --- |
| GET | `/health` | open | liveness | no |
| POST | `/auth/codes` | open | register a pending 6-digit code | — |
| GET | `/auth/status` | code | is my code linked? | no |
| GET | `/messages/links` | code | **new forwarded reels, full bookmark shape** | **yes** |
| GET | `/messages` | code | peek (read-only) | no |
| GET | `/messages/history?username&limit&type` | code | look back in my thread | no |
| GET | `/metadata?url&timeout_ms&debug` | open | og: tag extractor | — |
| POST | `/debug/reset` | code | clear my thread's seen-state | resets it |
| GET | `/debug/raw?count` | code | raw DM dump (my thread) | no |

Auth = the 6-digit link code, sent as `X-API-Key: <code>` or
`Authorization: Bearer <code>`.

## The linking flow (the whole auth model)

1. App registers a code: `POST /auth/codes {"code":"123456"}` (pending, ~10 min TTL).
2. User opens Instagram and DMs the official account: `/link 123456`.
3. The poller (every ~20s) sees the directive and binds **code → that thread**
   (first-touch wins; the code expires on bind).
4. App calls `GET /messages/links` with `X-API-Key: 123456` → the backend
   relays **only that thread's** forwarded reels as bookmark-shaped rows and
   advances only that thread's seen-state.

**Unregistered DMs are invisible:** the poller reads text only to spot
`/link <code>`; everything else in an unlinked thread is ignored — never
relayed, never persisted, never advances seen-state. Bad/unregistered `/link`
attempts are ignored silently (no auto-reply).

## What `/messages/links` returns

A `BookmarkResponse[]` matching the app's insert shape exactly:

```json
{
  "id": "…",
  "url": "https://www.instagram.com/reel/Ddm7_DEMQwJ/",
  "urlHash": "a932939e",
  "domain": "instagram.com",
  "path": "/reel/Ddm7_DEMQwJ/",
  "shortcode": "Ddm7_DEMQwJ",
  "title": "", "description": "",
  "image": "<xma_share.preview_url>",
  "favicon": "https://instagram.com/favicon.ico",
  "siteName": "Instagram",
  "author": "<reel-creator>",
  "publishedAt": null, "language": "",
  "type": "video", "mediaType": "reel",
  "tags": ["pizza"], "notes": "",
  "isFavorite": false, "isArchived": false, "isRead": false,
  "customTitle": "",
  "customDescription": "sender's caption with #pizza",
  "username": "sender",
  "timestamp": 1790200129000
}
```

Contract details: `urlHash` is the app's **djb2** (port of `urlHashFor`);
timestamps are **ms**; text is `""` never `null`; `description` stays empty
(sender text → `customDescription`); `tags` is a real array (hashtags extracted
from the caption); caption window **120s**, adjacent texts concatenate.

## Metadata extractor

The trick is the crawler User-Agent — walled sites serve full `og:` tags to
`Googlebot`/`facebookexternalhit`, but a consent/JS shell to browsers.

```bash
curl http://localhost:8000/health
curl "http://localhost:8000/metadata?url=https%3A%2F%2Fwww.instagram.com%2Freel%2FDdjqhjPhkm7%2F"
# optional per-request diagnostics:
curl "http://localhost:8000/metadata?url=…&debug=1"
```

The app points at it via Profile → Self-hosted server, or falls back to
`http://<dev-machine-ip>:8000` in dev builds (see D-013).

## Adding a platform (Telegram/Discord/Slack…)

Implement `Connector` (`app/connectors/base.py`), register it in
`app/connectors/__init__.py:build_connector`, and the routers/auth/enrichment
work unchanged — that's the whole point of the ABC split.

## Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -q
```

## Safety

- `guards.py` rejects URLs resolving to private/reserved addresses (basic SSRF
  protection, incl. the cloud metadata endpoint) before any fetch.
- `.env`, `session.json`, `links.json`, `seen_messages.json` are gitignored —
  never commit credentials or account state.
- Each link code is scoped to one thread; callers never see other threads.