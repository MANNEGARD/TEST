"""Lösenordshashning med Argon2id."""

from __future__ import annotations

import threading

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 10

# Varje Argon2-anrop tar 64 MiB minne; begränsa hur många som körs samtidigt i processen.
MAX_CONCURRENT_HASHES = 4
_hash_slots = threading.BoundedSemaphore(MAX_CONCURRENT_HASHES)


def hash_password(password: str) -> str:
    with _hash_slots:
        return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        with _hash_slots:
            return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def validate_password_strength(password: str) -> str | None:
    """Returnerar ett felmeddelande på svenska, eller None om lösenordet duger."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Lösenordet måste vara minst {MIN_PASSWORD_LENGTH} tecken."
    if password.lower() in {"password123", "lösenord123", "1234567890", "qwertyuiop"}:
        return "Lösenordet är för vanligt."
    return None
