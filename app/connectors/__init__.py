from .base import Connector, FetchResult, InboundItem, LinkDirective
from .instagram import InstagramConnector

__all__ = [
    "Connector",
    "FetchResult",
    "InboundItem",
    "LinkDirective",
    "InstagramConnector",
]


def build_connector(settings) -> Connector | None:
    """Return the configured connector, or None when no platform is configured."""
    if settings.instagram_configured:
        return InstagramConnector(
            username=settings.ig_username,
            password=settings.ig_password,
            session_file=settings.session_file,
            threads_per_fetch=settings.threads_per_fetch,
            thread_message_limit=settings.thread_message_limit,
            burst_fetch_limit=settings.burst_fetch_limit,
        )
    return None