"""Unit tests for qrcode_auth helpers (no network, no SDK).

Covers the pure helpers behind the WebUI QR-login flow: QR image
generation, poll-token encode/decode round-trip, AES secret decrypt, and
the handler registry. Handler ``fetch_qrcode``/``poll_status`` methods hit
third-party HTTP endpoints and are intentionally not covered here.
"""

from __future__ import annotations

import base64

import pytest

from erza.channels.qrcode_auth import (
    QRCODE_AUTH_HANDLERS,
    QRCodeAuthHandler,
    _decode_poll_token,
    _decrypt_secret,
    _encode_poll_token,
    _generate_bind_key,
    generate_qrcode_image,
    get_qr_handler,
)

_PNG_MAGIC = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")[
    :8
]


def test_generate_qrcode_image_returns_png_base64() -> None:
    img = generate_qrcode_image("https://example.com/scan?x=1")
    raw = base64.b64decode(img)
    assert raw[:8] == _PNG_MAGIC


def test_generate_qrcode_image_differs_per_url() -> None:
    assert generate_qrcode_image("https://a.example/1") != generate_qrcode_image(
        "https://a.example/2"
    )


def test_poll_token_round_trip() -> None:
    key = _generate_bind_key()
    token = _encode_poll_token("task-123", key)
    task_id, back_key = _decode_poll_token(token)
    assert task_id == "task-123"
    assert back_key == key


def test_decode_poll_token_rejects_bad_format() -> None:
    with pytest.raises(ValueError, match="invalid poll token format"):
        _decode_poll_token("not-a-token!!")


def test_decode_poll_token_rejects_forged_signature() -> None:
    _encode_poll_token("task-456", _generate_bind_key())
    with pytest.raises(ValueError, match="invalid poll token signature"):
        _decode_poll_token("task-456." + "0" * 64)


def test_decode_poll_token_rejects_expired() -> None:
    import time

    import erza.channels.qrcode_auth as qa

    token = qa._encode_poll_token("task-789", _generate_bind_key())
    qa._poll_tokens["task-789"]["expires_at"] = time.monotonic() - 1
    with pytest.raises(ValueError, match="poll token expired"):
        _decode_poll_token(token)


def test_decrypt_secret_round_trip() -> None:
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = os.urandom(32)
    key_b64 = base64.b64encode(key).decode()
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, b"app_secret_123", None)
    enc = base64.b64encode(iv + ct).decode()
    assert _decrypt_secret(enc, key_b64) == "app_secret_123"


def test_decrypt_secret_rejects_short_ciphertext() -> None:
    key_b64 = base64.b64encode(b"0" * 32).decode()
    with pytest.raises(ValueError, match="too short"):
        _decrypt_secret(base64.b64encode(b"short").decode(), key_b64)


def test_registry_covers_all_channels() -> None:
    assert sorted(QRCODE_AUTH_HANDLERS) == [
        "dingtalk",
        "feishu",
        "qq",
        "wecom",
        "weixin",
    ]
    for handler in QRCODE_AUTH_HANDLERS.values():
        assert isinstance(handler, QRCodeAuthHandler)


def test_get_qr_handler_routing() -> None:
    assert get_qr_handler("feishu") is QRCODE_AUTH_HANDLERS["feishu"]
    assert get_qr_handler("unknown-channel") is None
