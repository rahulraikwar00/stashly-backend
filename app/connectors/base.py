"""Platform-independent connector contract.

To add a new platform (Telegram, Discord, Slack…) implement `Connector` and
register it next to Instagram in `app/connectors/__init__.py`. Everything above
this layer — enrichment, routers, auth, seen-state — works unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class InboundItem:
    """A single normalized inbound message.

    `content` is a clean URL for `type == "link"`, or the message text for
    `text`/`unknown`. `timestamp` is epoch *seconds* (convert to ms at the
    enrichment boundary, per contract §3.4).
    """

    id: str
    thread_key: str
    sender_id: str
    username: str
    type: str  # "link" | "text" | "unknown"
    content: str
    timestamp: float | None = None
    preview_url: str = ""
    author: str = ""
    media_type: str | None = None
    shortcode: str | None = None


@dataclass
class FetchResult:
    """New items since a cursor, plus the cursor to persist.

    `cursor` reflects the newest message id seen in the fetched batch; persist
    it only when `items` is non-empty, and it starts `None` on first run.
    """

    items: list[InboundItem] = field(default_factory=list)
    cursor: str | None = None


@dataclass
class LinkDirective:
    """A "/link <code>" hit found in an inbound thread."""

    thread_key: str
    code: str
    sender_id: str
    username: str


class Connector(ABC):
    platform: str = "generic"

    @abstractmethod
    def prepare(self) -> None:
        """Best-effort login/session setup. Must not raise on failure."""

    @abstractmethod
    def fetch_new(self, thread_key: str | None, cursor: str | None) -> FetchResult:
        """Return items in `thread_key` that are new after `cursor`.

        `thread_key is None` means "the relay thread this account binds to" —
        connectors decide. Never advances any state itself; the caller persists
        `FetchResult.cursor`.
        """

    @abstractmethod
    def history(self, thread_key: str | None, limit: int) -> list[InboundItem]:
        """Return the most recent `limit` items, oldest-first."""

    @abstractmethod
    def scan_for_link_directives(self) -> list[LinkDirective]:
        """Scan every thread for "/link <code>" directives (max one per thread)."""

    def fetch_many(
        self,
        thread_keys: list[str],
        cursors: dict[str, str | None],
    ) -> dict[str, FetchResult]:
        """Fetch new items for several threads at once.

        The poller ingests every linked thread each cycle. Naively calling
        `fetch_new` per thread would re-list the inbox once per thread, which is
        N identical upstream calls per cycle. The default implementation does
        exactly that so a connector keeps working without changes; connectors
        that can serve many threads from one listing should override it.
        """
        return {
            key: self.fetch_new(key, cursors.get(key)) for key in thread_keys
        }

    def raw_dump(self, thread_key: str | None, count: int = 5) -> dict:
        """Raw platform message dump for /debug/raw (best effort, optional)."""
        return {"thread_id": thread_key or "", "messages": []}