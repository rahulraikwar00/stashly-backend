"""Environment-driven settings for the bookmark backend."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Settings:
    # ── Serving ──
    # Which TCP port to serve on. Read from .env (`PORT`), default 8000.
    port: int = 8000

    # ── Instagram (the official account that receives /link <code> DMs) ──
    ig_username: str = ""
    ig_password: str = ""

    # ── Relay / linking ──
    code_ttl_seconds: int = 600  # pending link codes expire after 10 min
    link_scan_seconds: int = 20  # poller cadence for detecting /link directives
    threads_per_fetch: int = 10
    thread_message_limit: int = 40
    burst_fetch_limit: int = 200
    caption_window_seconds: int = 120

    # ── Paths (all in the app folder, gitignored) ──
    session_file: Path = field(default_factory=lambda: APP_DIR / "session.json")
    seen_file: Path = field(default_factory=lambda: APP_DIR / "seen_messages.json")
    links_file: Path = field(default_factory=lambda: APP_DIR / "links.json")

    @property
    def instagram_configured(self) -> bool:
        return bool(self.ig_username and self.ig_password)


def load_settings() -> Settings:
    from dotenv import load_dotenv

    load_dotenv(APP_DIR.parent / ".env")

    return Settings(
        port=max(1, min(65535, _env_int("PORT", 8000))),
        ig_username=os.getenv("IG_USERNAME", "").strip(),
        ig_password=os.getenv("IG_PASSWORD", "").strip(),
        code_ttl_seconds=_env_int("CODE_TTL_SECONDS", 600),
        link_scan_seconds=max(5, _env_int("LINK_SCAN_SECONDS", 20)),
        threads_per_fetch=max(1, _env_int("THREADS_PER_FETCH", 10)),
        thread_message_limit=max(5, _env_int("THREAD_MESSAGE_LIMIT", 40)),
        burst_fetch_limit=max(50, _env_int("BURST_FETCH_LIMIT", 200)),
        caption_window_seconds=max(5, _env_int("CAPTION_WINDOW_SECONDS", 120)),
    )