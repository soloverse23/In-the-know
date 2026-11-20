"""Sealed tokens: encrypted, authenticated, expiring JSON blobs.

The hosted server keeps no database, so everything it must remember between two HTTP requests
(a registered client, a sign-in in progress, an authorization code, an owner's access) travels
inside a sealed string that only this server can open. AES-256-GCM with a purpose string as
associated data: a token sealed for one purpose ("code") can never be opened as another ("access").

Keys come from ITK_SEAL_KEYS="kid:base64url-32-bytes,kid2:..."; the first key seals, all keys open,
so keys rotate without logging anyone out.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "itk1"


class SealError(Exception):
    """The token is malformed, forged, sealed with an unknown key, for another purpose, or expired."""


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def new_key() -> str:
    return _b64e(AESGCM.generate_key(bit_length=256))


class Sealer:
    def __init__(self, keys: list[tuple[str, bytes]]):
        if not keys:
            raise ValueError("at least one key is required")
        for kid, k in keys:
            if len(k) != 32 or not kid.isalnum():
                raise ValueError("keys must be 32 bytes with an alphanumeric id")
        self._keys = {kid: AESGCM(k) for kid, k in keys}
        self._current = keys[0][0]

    @classmethod
    def from_env(cls, var: str = "ITK_SEAL_KEYS") -> "Sealer":
        raw = os.environ.get(var, "").strip()
        if not raw:
            raise RuntimeError(f"{var} is not set. Generate a key with: python -m intheknow.seal")
        keys = []
        for part in raw.split(","):
            kid, _, k = part.strip().partition(":")
            keys.append((kid, _b64d(k)))
        return cls(keys)

    def seal(self, purpose: str, data: dict[str, Any], ttl: Optional[int]) -> str:
        body = dict(data)
        if ttl is not None:
            body["exp"] = int(time.time()) + ttl
        nonce = os.urandom(12)
        ct = self._keys[self._current].encrypt(nonce, json.dumps(body, separators=(",", ":")).encode(), purpose.encode())
        return f"{PREFIX}.{self._current}.{_b64e(nonce + ct)}"

    def open(self, purpose: str, token: str) -> dict[str, Any]:
        try:
            prefix, kid, blob = token.split(".")
            if prefix != PREFIX or kid not in self._keys:
                raise SealError("unknown token format or key")
            raw = _b64d(blob)
            body = json.loads(self._keys[kid].decrypt(raw[:12], raw[12:], purpose.encode()))
        except SealError:
            raise
        except Exception as exc:  # noqa: BLE001 - every failure looks the same to the caller
            raise SealError("invalid token") from exc
        if "exp" in body and body["exp"] < time.time():
            raise SealError("expired")
        return body


if __name__ == "__main__":
    print(f"ITK_SEAL_KEYS=k1:{new_key()}")
