"""HMAC-SHA256 request signer for Delta Exchange India.

Signature string format (per Delta docs):
    method + timestamp + path_with_querystring + body
where:
    method   = uppercase HTTP verb
    timestamp = unix epoch seconds, as a string
    body      = "" for GET/DELETE without payload, else the exact JSON string sent on the wire
"""

from __future__ import annotations

import hashlib
import hmac
import time


class RequestSigner:
    def __init__(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8") if api_secret else b""

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._api_secret)

    @staticmethod
    def now_timestamp() -> str:
        return str(int(time.time()))

    def sign(
        self,
        method: str,
        path: str,
        *,
        query_string: str = "",
        body: str = "",
        timestamp: str | None = None,
    ) -> tuple[str, str]:
        """Return (timestamp, signature_hex) for an outgoing request."""
        if not self.configured:
            raise RuntimeError("RequestSigner has no api_key/api_secret configured")
        ts = timestamp or self.now_timestamp()
        message = f"{method.upper()}{ts}{path}{query_string}{body}"
        digest = hmac.new(self._api_secret, message.encode("utf-8"), hashlib.sha256).hexdigest()
        return ts, digest

    def headers(
        self,
        method: str,
        path: str,
        *,
        query_string: str = "",
        body: str = "",
        timestamp: str | None = None,
    ) -> dict[str, str]:
        ts, signature = self.sign(method, path, query_string=query_string, body=body, timestamp=timestamp)
        return {
            "api-key": self._api_key,
            "timestamp": ts,
            "signature": signature,
        }
