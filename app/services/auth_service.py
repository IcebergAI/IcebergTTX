from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.config import settings
from app.schemas.auth import MAX_PASSWORD_BYTES


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        plain_bytes = plain.encode()
    except UnicodeEncodeError:
        return False
    if len(plain_bytes) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(plain_bytes, hashed.encode())
    except (UnicodeEncodeError, ValueError):
        # bcrypt rejects inputs over 72 bytes (and malformed stored hashes). Treat
        # either as an ordinary verification failure so the login route records the
        # attempt and returns the same response as every other invalid credential.
        return False


def create_access_token(subject: str, role: str, is_admin: bool = False) -> str:
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=settings.access_token_expire_minutes)
    # `iat` (issued-at) is required for token revocation (#14): get_current_user
    # rejects tokens whose iat predates the user's token_valid_after cutoff.
    # `is_admin` is a UI convenience only (page/nav gating in ui.py) — API access
    # is always re-checked against the real DB column, so a forged claim is inert.
    payload = {"sub": subject, "role": role, "is_admin": is_admin, "iat": now, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_access_token(token: str) -> dict:
    """Raises PyJWTError if invalid or expired."""
    return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
