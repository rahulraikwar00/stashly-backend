-- D-018: durable state for the Render deployment.
--
-- Mirrors the three historical JSON documents 1:1 (links.json, seen_messages.json,
-- session.json) so the Python-facing store interfaces barely change:
--
--   link_codes + thread_links  <->  CodeStore (links.json)
--   seen_cursors               <->  SeenStore (seen_messages.json)
--   app_secrets                <->  the Instagram session (session.json, encrypted)
--
-- Everything is ms epoch rather than timestamptz because the D-016 wire contract
-- already speaks in ms and the app's timestamp fields are ms.
--
-- Applied by app/db/migrate.py with IF NOT EXISTS; safe to run on every boot.

CREATE TABLE IF NOT EXISTS link_codes (
  code       text PRIMARY KEY,
  status     text NOT NULL CHECK (status IN ('pending', 'linked', 'unlinked')),
  thread_id  text NOT NULL DEFAULT '',
  sender_id  text NOT NULL DEFAULT '',
  username   text NOT NULL DEFAULT '',
  created_at bigint NOT NULL,
  expires_at bigint,
  linked_at  bigint
);

-- Only pending codes can expire, so the index stays tiny.
CREATE INDEX IF NOT EXISTS link_codes_pending_expiry
  ON link_codes (expires_at) WHERE status = 'pending';

-- The "threads" map: which code owns which thread. First-touch-wins is enforced
-- by the UNIQUE primary key on thread_id, in addition to the transaction in
-- attempt_bind().
CREATE TABLE IF NOT EXISTS thread_links (
  thread_id text PRIMARY KEY,
  code      text NOT NULL REFERENCES link_codes (code) ON DELETE CASCADE
);

-- Per-thread seen cursor. The relay is the only writer.
CREATE TABLE IF NOT EXISTS seen_cursors (
  thread_id  text PRIMARY KEY,
  cursor     text NOT NULL,
  updated_at bigint NOT NULL
);

-- The buffered mailbox. One producer (the poller), one consumer (the app).
-- Drained with DELETE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)
-- RETURNING payload, so delivery and consumption are one atomic statement.
CREATE TABLE IF NOT EXISTS pending_items (
  id         bigserial PRIMARY KEY,
  thread_id  text NOT NULL,
  source_msg text NOT NULL,
  payload    jsonb NOT NULL,
  created_at bigint NOT NULL,
  -- Makes ingest idempotent against the burst re-fetch path in fetch_new().
  UNIQUE (thread_id, source_msg)
);

CREATE INDEX IF NOT EXISTS pending_items_thread
  ON pending_items (thread_id, id);

-- /metadata read-through cache, keyed by the app's djb2 urlHash.
CREATE TABLE IF NOT EXISTS metadata_cache (
  url_hash   text PRIMARY KEY,
  url        text NOT NULL,
  payload    jsonb NOT NULL,
  fetched_at bigint NOT NULL,
  expires_at bigint NOT NULL
);

CREATE INDEX IF NOT EXISTS metadata_cache_expiry
  ON metadata_cache (expires_at);

-- Encrypted secrets. Never store plaintext here: if IG_SESSION_KEY is unset the
-- Instagram connector keeps its session in memory instead of writing to disk.
CREATE TABLE IF NOT EXISTS app_secrets (
  name       text PRIMARY KEY,
  ciphertext bytea NOT NULL,
  updated_at bigint NOT NULL
);

-- Brute-force throttling. Keyed by code, not by IP, so users behind shared NAT
-- are not punished for an attack they did not make.
CREATE TABLE IF NOT EXISTS failed_auth (
  code         text PRIMARY KEY,
  failures     integer NOT NULL DEFAULT 0,
  locked_until bigint
);

CREATE INDEX IF NOT EXISTS failed_auth_locked
  ON failed_auth (locked_until) WHERE locked_until IS NOT NULL;
