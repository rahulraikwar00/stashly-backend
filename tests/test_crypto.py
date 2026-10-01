"""Tests for AES-256-GCM secret handling (app/crypto.py).

The Instagram session is equivalent to full control of the official account, so
these guard both directions: that it round-trips, and that the plaintext never
escapes into logs.
"""

from __future__ import annotations

import base64
import logging

import pytest
from cryptography.exceptions import InvalidTag

from app.crypto import KeyError_, decrypt, encrypt, load_key

KEY_HEX = "a" * 64
KEY_B64 = base64.b64encode(bytes(range(32))).decode()
SECRET = b'{"sessionid": "abc123", "csrftoken": "def456"}'


@pytest.mark.parametrize("encoded", [KEY_HEX, KEY_B64])
def test_key_accepts_hex_and_base64(encoded: str):
    assert len(load_key(encoded)) == 32


def test_hex_and_base64_forms_can_differ_but_both_work():
    assert load_key(KEY_HEX) != load_key(KEY_B64)


@pytest.mark.parametrize("bad", ["", "   ", "short", "z" * 64, "!!!not-base64!!!"])
def test_bad_keys_are_rejected(bad: str):
    with pytest.raises(KeyError_):
        load_key(bad)


def test_round_trip():
    key = load_key(KEY_HEX)
    assert decrypt(key, encrypt(key, SECRET)) == SECRET


def test_ciphertext_is_not_the_plaintext():
    key = load_key(KEY_HEX)
    blob = encrypt(key, SECRET)
    assert SECRET not in blob
    assert b"sessionid" not in blob


def test_nonce_makes_every_encryption_unique():
    key = load_key(KEY_HEX)
    a, b = encrypt(key, SECRET), encrypt(key, SECRET)
    assert a != b, "a repeated nonce would leak plaintext equality"
    assert decrypt(key, a) == decrypt(key, b) == SECRET


def test_wrong_key_fails_loudly():
    blob = encrypt(load_key(KEY_HEX), SECRET)
    with pytest.raises(InvalidTag):
        decrypt(load_key("b" * 64), blob)


def test_tampered_ciphertext_is_rejected():
    key = load_key(KEY_HEX)
    blob = bytearray(encrypt(key, SECRET))
    blob[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        decrypt(key, bytes(blob))


def test_truncated_blob_is_rejected():
    key = load_key(KEY_HEX)
    with pytest.raises(InvalidTag):
        decrypt(key, b"short")


def test_aad_binds_the_ciphertext_to_its_name():
    key = load_key(KEY_HEX)
    blob = encrypt(key, SECRET, aad=b"instagram")
    assert decrypt(key, blob, aad=b"instagram") == SECRET
    with pytest.raises(InvalidTag):
        decrypt(key, blob, aad=b"other")


def test_plaintext_never_reaches_a_log_record(caplog):
    """Guard against a future change logging the session by accident."""
    key = load_key(KEY_HEX)
    with caplog.at_level(logging.DEBUG):
        blob = encrypt(key, SECRET)
        decrypt(key, blob)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "sessionid" not in text
    assert "abc123" not in text