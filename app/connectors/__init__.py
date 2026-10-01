from .base import Connector, FetchResult, InboundItem, LinkDirective
from .instagram import InstagramConnector

__all__ = [
    "Connector",
    "FetchResult",
    "InboundItem",
    "LinkDirective",
    "InstagramConnector",
]


def build_connector(settings, secret_store=None) -> Connector | None:
    """Return the configured connector, or None when no platform is configured.

    `secret_store` (D-018) lets the connector persist its session encrypted; when
    it is absent — or carries no key — the connector keeps the session in memory
    only and never writes it to disk.
    """
    if settings.instagram_configured:
        return InstagramConnector(
            username=settings.ig_username,
            password=settings.ig_password,
            session_file=settings.session_file,
            threads_per_fetch=settings.threads_per_fetch,
            thread_message_limit=settings.thread_message_limit,
            burst_fetch_limit=settings.burst_fetch_limit,
            secret_store=secret_store,
        )
    return None