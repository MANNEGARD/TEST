"""Kryptering av hemligheter i vila (OAuth-tokens, API-nycklar) med Fernet.

Nycklar anges i DRIVELOG_ENCRYPTION_KEYS (kommaseparerade; första krypterar, alla
dekrypterar → nyckelrotation). I utveckling härleds en nyckel ur SECRET_KEY.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import get_config


class SecretDecryptionError(Exception):
    """Hemligheten kunde inte dekrypteras (fel eller roterad nyckel)."""


def _derive_key(secret: str) -> bytes:
    digest = hashlib.sha256(("drivelog-fernet:" + secret).encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> MultiFernet:
    cfg = get_config()
    keys = [k.strip() for k in cfg.encryption_keys.split(",") if k.strip()]
    fernets = [Fernet(k.encode()) for k in keys] if keys else [Fernet(_derive_key(cfg.secret_key))]
    return MultiFernet(fernets)


def encrypt_text(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_text(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretDecryptionError("Kunde inte dekryptera lagrad hemlighet.") from exc


def encrypt_json(data: dict[str, Any]) -> str:
    return encrypt_text(json.dumps(data, separators=(",", ":")))


def decrypt_json(token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    return json.loads(decrypt_text(token))


def generate_key() -> str:
    return Fernet.generate_key().decode()


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def mask_secret(value: str | None, keep: int = 4) -> str | None:
    """Maskerad representation för UI/loggar – aldrig hela värdet."""
    if not value:
        return None
    if len(value) <= keep:
        return "•" * len(value)
    return "•" * 8 + value[-keep:]
