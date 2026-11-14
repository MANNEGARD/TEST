"""Lösenordshashning med Argon2id."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
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
