"""
core/security.py — Credential encryption at rest using Fernet symmetric encryption.

Encrypts sensitive values (JWT tokens, passwords) before storing in DB.
Uses the SECRET_KEY from settings as the encryption key seed.
"""
import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from core.config import get_settings
from utils.logger import get_logger

settings = get_settings()
logger = get_logger("security")


def _get_fernet() -> Fernet:
    """
    Derive a 32-byte Fernet key from the SECRET_KEY env var.
    SHA-256 hash ensures it's always exactly 32 bytes regardless of input length.
    """
    key_bytes = hashlib.sha256(settings.secret_key.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns base64-encoded ciphertext."""
    if not plaintext:
        return ""
    try:
        f = _get_fernet()
        return f.encrypt(plaintext.encode()).decode()
    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        raise


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted string. Returns original plaintext."""
    if not ciphertext:
        return ""
    try:
        f = _get_fernet()
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.error("Decryption failed: invalid token or wrong SECRET_KEY.")
        raise ValueError("Cannot decrypt — SECRET_KEY may have changed.")
    except Exception as e:
        logger.error(f"Decryption error: {e}")
        raise


def mask(value: str, visible: int = 4) -> str:
    """
    Mask a sensitive string for display/logging purposes.
    e.g. mask("ABCDEF1234", 4) → "ABCD••••••"
    """
    if not value or len(value) <= visible:
        return "•" * len(value)
    return value[:visible] + "•" * (len(value) - visible)


def hash_password(password: str) -> str:
    """
    One-way hash for passwords stored in DB.
    Not used for Angel One login (that needs plaintext), but
    useful for securing the dashboard admin password if added later.
    """
    import hashlib
    return hashlib.sha256(
        (password + settings.secret_key).encode()
    ).hexdigest()


def verify_api_token(token: str, expected_hash: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    import hmac
    return hmac.compare_digest(
        hash_password(token).encode(),
        expected_hash.encode(),
    )


# ── Convenience wrappers for session token storage ────────────────────────────

def encrypt_session_tokens(jwt: str, refresh: str, feed: str) -> tuple[str, str, str]:
    """Encrypt all three Angel One session tokens for DB storage."""
    return encrypt(jwt), encrypt(refresh), encrypt(feed)


def decrypt_session_tokens(jwt_enc: str, refresh_enc: str, feed_enc: str) -> tuple[str, str, str]:
    """Decrypt stored session tokens for use."""
    return decrypt(jwt_enc), decrypt(refresh_enc), decrypt(feed_enc)