# Stashly Backend

FastAPI service behind the Instagram DM → bookmark relay, plus the metadata
extractor the app falls back to on walled sites (Instagram/TikTok/Pinterest).

Architecture and design decisions live in the app repo
(<https://github.com/rahulraikwar00/stashly>):

- `docs/02-Architecture.md` §2.3 — the full flow
- `docs/03-API-Contract.md` — the wire contract (§3.3 response shape)
- `DesingDecision/decisions.md` **D-016** — this service's design
- `DesingDecision/decisions.md` **D-018** — Render deploy, Postgres state,
  buffered mailbox, encrypted session

## One primary store: Neon

Neon is the primary store for every durable fact. `DATABASE_URL` is **required** —
if it is missing the app raises at startup rather than quietly running on storage
it would lose on the next restart:

| | Value | Notes |
| --- | --- | --- |
| State | Postgres (`link_codes`, `thread_links`, `seen_cursors`, `pending_items`, `metadata_cache`, `app_secrets`, `failed_auth`) | survives restarts and container rebuilds |
| `/messages/links` | drains the poller's buffered mailbox | no live Instagram fetch per request |
| Instagram session | encrypted in `app_secrets` under `IG_SESSION_KEY` | never written in plaintext |

`DB_BACKEND` selects which Postgres, and is validated at startup:

| `DB_BACKEND` | Meaning |
| --- | --- |
| `neon` (default) | `DATABASE_URL` must name a Neon pooled DSN. Missing → startup error. |
| `local` | the only way to use a throwaway Postgres; pair it with a local `DATABASE_URL`. |

Any other value is a startup error, so a typo like `DB_BACKEND=postgres` fails
loudly instead of silently writing to Neon.

To run against a local Postgres instead of Neon, use the compose override — it is
the only file that mentions one:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build -d
```

The plain `docker compose up` uses Neon, because that is the point: reaching
disposable storage is something you ask for, not something you inherit.

## Run

```bash
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env   # set DATABASE_URL, PORT, IG_USERNAME / IG_PASSWORD
.venv/bin/python -m app
```

The serving port comes from `PORT` in `.env` (default 8000); the app's "Self-hosted
server" URL / dev fallback must match it.

`/metadata` and `/health` work with no Instagram credentials. Setting
`IG_USERNAME`/`IG_PASSWORD` enables the relay + link-poller + ingest. The
connector persists its session **encrypted** under `IG_SESSION_KEY`; without that
key it stays in memory only, which means a fresh login on every deploy and is
dangerous on Render.

## Account safety: why the poller is not a fixed interval

A `/link <code>` DM can only arrive while a user is mid-link — they register a
code, then DM it. Polling at a constant short interval to catch that one event
spends nearly all of its calls on empty inboxes, and an evenly spaced interval
is a machine signature Instagram can pick up trivially. So the cadence is
derived from the work actually outstanding:

| Mode | Cadence | When |
| --- | --- | --- |
| Active | `LINK_SCAN_SECONDS` (30) ± `LINK_JITTER_PCT` (40) | a registered code is still pending |
| Idle | `LINK_SCAN_IDLE_SECONDS` (600) ± jitter | nothing pending |
| Backoff | `LINK_ERROR_BACKOFF_SECONDS` (300), doubling, 1h cap | the last cycle raised |

Three further choices in the same spirit:

- **One inbox listing per cycle.** The scan and the ingest used to each call
  `direct_threads` independently, doubling the request count for identical data.
  `Connector.fetch_cycle` serves both from one listing; connectors that don't
  implement it keep the old two-call path.
- **Expiring codes don't pin the fast cadence.** `has_pending()` is expiry-aware
  on both stores, so an abandoned code falls back to idle instead of polling hard
  for the rest of the process's life.
- **A checkpoint is not retried into it.** `ChallengeRequired`, `FeedbackRequired`,
  `RateLimitError` and `ProxyAddressIsBlocked` propagate out of the listing call
  so the poller backs off. Only `LoginRequired` triggers a re-login. A challenge
  means the account is under scrutiny, and hammering through it is how a session
  gets lost.

The floor on `LINK_SCAN_SECONDS` is 15s, not 5: a misconfigured value should not
be able to turn this into a fast poller.

None of this is a guarantee — Instagram weighs IP reputation, login frequency and
interaction patterns too. What is load-bearing is that the account is dedicated,
has 2FA, and that `IG_SESSION_KEY` keeps the session across restarts so the app
reuses a persisted session rather than re-logging-in. `IG_SESSION_KEY` must be
**stable**: rotating it makes the stored session undecryptable and forces a fresh
login, which is the single most likely cause of a checkpoint.

## Deploying to Render (free tier)

`render.yaml` is a blueprint, so the service is reproducible from the repo rather
than from a dashboard ritual. Push the repo to GitHub, connect it in Render, and
it prompts for the four secrets.

1. Create a Neon project, copy the **pooled** connection string (hostname
   contains `-pooler`).
2. Render → Blueprint, pointing at the repo, or Web Service with root directory
   `.`, runtime Docker, start command `python -m app`, health check `/health`.
3. Env vars: `DATABASE_URL`, `IG_USERNAME`, `IG_PASSWORD`, `IG_SESSION_KEY`
   (`openssl rand -hex 32`). **Do not set `PORT`** — Render injects 10000 and the
   Dockerfile already defaults to it.
4. UptimeRobot monitor on `https://<service>.onrender.com/health`, 5-minute
   interval.

Free-tier behaviours to know:

- The service sleeps after 15 minutes idle. UptimeRobot prevents that, and even
  if it fails, linking only *delays* — `scan_for_link_directives` re-reads recent
  threads each cycle, so a `/link` sent while asleep is found on wake. With the
  idle backoff the instance has almost nothing to do anyway, so sleeping costs
  nothing real.
- Free instances have an **ephemeral disk**, which is why all state lives in
  Postgres and the session is encrypted there rather than in a file. A cold start
  reuses the persisted session, so it does not re-authenticate against Instagram.
- `/health` deliberately returns 200 while degraded, because Render restarts an
  instance after 60s of failing checks and a restart means a fresh Instagram
  login. Use `?strict=1` for a hard gate in monitoring.

Migrating existing local state once:

```bash
DATABASE_URL=postgresql://... .venv/bin/python -m scripts.seed_from_json
```

## Endpoints

| Method | Path | Auth | Purpose | Consumes seen? |
| --- | --- | --- | --- | --- |
| GET | `/health` | open | liveness + database round-trip | no |
| POST | `/auth/codes` | open | register a pending 6-digit code (+ the account to DM) | — |
| GET | `/auth/config` | open | which account to DM (`igUsername`, `linkCommand`) | — |
| GET | `/auth/status` | code | is my code linked? | no |
| GET | `/messages/links` | code | **new forwarded reels, full bookmark shape** | **yes (drains buffer)** |
| GET | `/messages` | code | peek (read-only) | no |
| GET | `/messages/history?username&limit&type` | code | look back in my thread | no |
| GET | `/metadata?url&timeout_ms&debug` | open | og: tag extractor (cached) | — |
| POST | `/debug/reset` | code | clear my thread's seen-state **and mailbox** | resets it |
| GET | `/debug/raw?count` | code | raw DM dump (my thread) | no |

Auth = the 6-digit link code, sent as `X-API-Key: <code>` or
`Authorization: Bearer <code>`. Five failed attempts lock a code for 15
minutes; every failure returns an identical 401 so the endpoint never reveals
which codes exist.

## The linking flow (the whole auth model)

1. App registers a code: `POST /auth/codes {"code":"123456"}` (pending, ~10 min TTL).
2. User opens Instagram and DMs the official account: `/link 123456`.
   The account handle comes from `GET /auth/config` (`IG_USERNAME` server-side) —
   the client no longer hardcodes it, so the two cannot drift.
3. The poller sees the directive and binds **code → that thread**
   (first-touch-wins; the code expires on bind). It scans every ~30s ±40% while
   a code is pending — the only window in which a `/link` can arrive — and backs
   off to ~10 min ±40% when nothing is pending, so the account is not polled for
   events that cannot occur.
4. App calls `GET /messages/links` with `X-API-Key: 123456` → the backend relays
   **only that thread's** buffered reels as bookmark-shaped rows and consumes them.

**Unregistered DMs are invisible:** the poller reads text only to spot
`/link <code>`; everything else in an unlinked thread is ignored — never
relayed, never buffered, never relayed onward. Bad/unregistered `/link`
attempts are ignored silently (no auto-reply).

## Delivery latency and captions

The poller buffers only messages older than `INGEST_SETTLE_SECONDS` (default =
the 120 s caption window). This is deliberate: `pair_captions` matches a reel to
its caption by position within one batch, so buffering a reel the instant it
arrives would drop the caption — and the hashtags derived from it — permanently.

The cost is that a forwarded reel appears up to ~140 s later (120 s settle + one
poll interval). Until the app's background fetch lands, pull-to-refresh may
return nothing on the first attempt after forwarding. Lower
`INGEST_SETTLE_SECONDS` only if you accept losing captions.

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

Results are cached in `metadata_cache` for `METADATA_CACHE_DAYS` (default 7),
keyed by the app's djb2 `urlHash`. This is the only endpoint worth caching and
the only one that could see a traffic spike.

The app points at it via Profile → Self-hosted server, or falls back to
`http://<dev-machine-ip>:8000` in dev builds (see D-013).

## Adding a platform (Telegram/Discord/Slack…)

Implement `Connector` (`app/connectors/base.py`), register it in
`app/connectors/__init__.py:build_connector`, and the routers/auth/enrichment
work unchanged — that's the whole point of the ABC split. Override
`fetch_many` if the platform can serve several threads from one listing; the
default implementation falls back to per-thread `fetch_new` calls.

## Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -q
```

Database-backed tests skip unless `TEST_DATABASE_URL` is set.

⚠️ **Never point `TEST_DATABASE_URL` at your main Neon branch.** `tests/conftest.py`
calls `truncate_all()` on it, which deletes every link code and the stored
Instagram session. Use a separate Neon `test` branch, or a local Postgres:

```bash
docker run -d --name d018test -e POSTGRES_PASSWORD=testpass \
  -e POSTGRES_DB=testdb -p 55432:5432 postgres:16-alpine
TEST_DATABASE_URL=postgresql://postgres:testpass@localhost:55432/testdb \
  python -m pytest tests/ -q
docker rm -f d018test
```

## Safety

- `guards.py` rejects URLs resolving to private/reserved addresses (basic SSRF
  protection, incl. the cloud metadata endpoint) before any fetch.
- `.env`, `session.json`, `links.json`, `seen_messages.json` are gitignored —
  never commit credentials or account state.
- The Instagram session is encrypted with AES-256-GCM before it is stored. With
  no `IG_SESSION_KEY` it is held in memory and **never** written to disk.
- Each link code is scoped to one thread; callers never see other threads, and
  the mailbox is drained per thread under `FOR UPDATE SKIP LOCKED`.
- Failed auth locks a code after 5 attempts; CORS is disabled unless
  `CORS_ORIGINS` is set.