"""Tests for HMAC request signer."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from bot.exchange.auth import RequestSigner


def test_sign_returns_hex_digest_matching_reference() -> None:
    secret = "topsecret"
    signer = RequestSigner("k", secret)
    ts = "1700000000"
    method, path = "GET", "/v2/products"
    _, sig = signer.sign(method, path, timestamp=ts)
    expected = hmac.new(secret.encode(), f"{method}{ts}{path}".encode(), hashlib.sha256).hexdigest()
    assert sig == expected


def test_sign_includes_query_and_body_in_message() -> None:
    signer = RequestSigner("k", "s")
    ts = "1700000000"
    _, sig_a = signer.sign("POST", "/v2/orders", query_string="", body='{"a":1}', timestamp=ts)
    _, sig_b = signer.sign("POST", "/v2/orders", query_string="?x=1", body='{"a":1}', timestamp=ts)
    _, sig_c = signer.sign("POST", "/v2/orders", query_string="", body='{"a":2}', timestamp=ts)
    assert sig_a != sig_b, "different query strings should yield different signatures"
    assert sig_a != sig_c, "different bodies should yield different signatures"


def test_headers_contains_api_key_timestamp_signature() -> None:
    signer = RequestSigner("test-key", "test-secret")
    headers = signer.headers("GET", "/v2/wallet/balances", timestamp="1700000000")
    assert headers["api-key"] == "test-key"
    assert headers["timestamp"] == "1700000000"
    assert len(headers["signature"]) == 64, "sha256 hex digest is 64 chars"


def test_sign_without_credentials_raises() -> None:
    signer = RequestSigner("", "")
    assert signer.configured is False
    with pytest.raises(RuntimeError):
        signer.sign("GET", "/v2/products")


def test_now_timestamp_is_unix_seconds_str() -> None:
    ts = RequestSigner.now_timestamp()
    assert ts.isdigit()
    assert 1_500_000_000 < int(ts) < 5_000_000_000
