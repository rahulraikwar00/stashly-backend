"""Contract tests for enrich.py — djb2 hash, media/type mapping, tags, captions."""

from app.connectors.base import InboundItem
from app.enrich import (
    app_type,
    djb2_hex,
    enrich,
    extract_tags,
    parse_media_type_and_code,
    url_hash,
)


def item(
    id_: str,
    kind: str,
    content: str,
    sender: str = "alice",
    username: str | None = None,
    ts: float | None = 100.0,
) -> InboundItem:
    return InboundItem(
        id=id_,
        thread_key="t1",
        sender_id=sender,
        username=username or sender,
        type=kind,
        content=content,
        timestamp=ts,
    )


def test_djb2_matches_app_hash_vectors():
    # Vectors computed from utils/hash.ts `urlHashFor` via Node.
    assert url_hash("https://www.instagram.com/reel/Ddm7_DEMQwJ/") == "a932939e"
    assert url_hash("https://example.com/A") == "a8ab3d59"
    assert url_hash("HTTPS://Example.COM/test?q=1") == "a91d3376"
    assert url_hash("") == "1505"
    assert djb2_hex("abc") == url_hash("ABC")


def test_media_type_and_code_parsing():
    assert parse_media_type_and_code("https://www.instagram.com/reel/Ddm7_DEMQwJ/?x=1") == (
        "reel",
        "Ddm7_DEMQwJ",
    )
    assert parse_media_type_and_code("https://www.instagram.com/p/AbC123/") == ("post", "AbC123")
    assert parse_media_type_and_code("https://www.instagram.com/tv/IGTV99/") == ("igtv", "IGTV99")
    assert parse_media_type_and_code("https://example.com/x") == (None, None)


def test_app_type_mapping():
    assert app_type("reel") == "video"
    assert app_type("igtv") == "video"
    assert app_type("post") == "image"
    assert app_type(None) == "link"
    assert app_type("weird") == "link"


def test_extract_tags_dedupes_and_lowercases():
    assert extract_tags("my fab #Pizza #pizza recipe") == ["pizza"]
    assert extract_tags("no tags here") == []
    assert extract_tags(None) == []
    assert extract_tags("a #one b #Two #one") == ["one", "two"]


def test_link_bookmark_full_shape():
    items = [
        item(
            "m1",
            "link",
            "https://www.instagram.com/reel/Ddm7_DEMQwJ/?utm_source=ig",
            ts=1000.0,
        )
    ]
    links, texts = enrich(items, 120)
    assert len(links) == 1
    assert texts == []
    bm = links[0]
    assert bm.id == "m1"
    assert bm.url == "https://www.instagram.com/reel/Ddm7_DEMQwJ/"  # query stripped
    assert bm.urlHash == url_hash(bm.url)
    assert bm.domain == "www.instagram.com"
    assert bm.path == "/reel/Ddm7_DEMQwJ/"
    assert bm.shortcode == "Ddm7_DEMQwJ"
    assert bm.type == "video"
    assert bm.mediaType == "reel"
    assert bm.timestamp == 1_000_000  # seconds -> ms
    assert bm.description == ""  # reserved for auto metadata
    assert bm.favicon == "https://instagram.com/favicon.ico"
    assert bm.siteName == "Instagram"
    assert bm.username == "alice"


def test_caption_merge_concatenates_and_never_leaks():
    # text BEFORE and AFTER the link, same sender, within 120s
    items = [
        item("t1", "text", "this is my fab #Pizza recipe", ts=900.0),
        item("m2", "link", "https://www.instagram.com/reel/Ddm7_DEMQwJ/", ts=1000.0),
        item("t3", "text", "second thought #pizza", ts=1010.0),
    ]
    links, texts = enrich(items, 120)
    assert len(links) == 1
    assert texts == []  # claimed texts never leak as items
    bm = links[0]
    assert bm.customDescription == "this is my fab #Pizza recipe second thought #pizza"
    assert bm.tags == ["pizza"]


def test_caption_window_and_sender_isolation():
    # far-away text + different-sender text are NOT captions -> standalone
    items = [
        item("m1", "link", "https://www.instagram.com/reel/Ddm7_DEMQwJ/", ts=1000.0),
        item("t2", "text", "far away", sender="alice", ts=2000.0),  # 1000s > 120s
        item("t3", "text", "other user", sender="bob", ts=1005.0),  # different sender
    ]
    links, texts = enrich(items, 120)
    assert len(links) == 1
    assert links[0].customDescription == ""
    assert [t.customDescription for t in texts] == ["far away", "other user"]
    assert all(t.type == "text" and t.url == "" for t in texts)


def test_text_standalone_shape():
    items = [item("t1", "text", "hello #world", ts=100.0)]
    links, texts = enrich(items, 120)
    assert links == []
    assert len(texts) == 1
    t = texts[0]
    assert t.type == "text"
    assert t.url == ""
    assert t.urlHash == ""
    assert t.customDescription == "hello #world"
    assert t.tags == ["world"]
    assert t.timestamp == 100_000