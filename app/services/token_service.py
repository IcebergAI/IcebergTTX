"""Single-use, expiring, hashed link tokens for email flows (#117).

The raw token is a URL-safe random string that only ever travels in the emailed
link; the DB stores just its SHA-256 hash (`AuthToken.token_hash`). Lookup is by
hash, so a database read never exposes a usable token. `consume` enforces
single-use (`used_at`) + expiry (`expires_at`) atomically.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.auth_token import AuthToken, AuthTokenPurpose


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate() -> tuple[str, str]:
    """Return ``(raw_token, token_hash)``. Only the hash is ever persisted."""
    raw = secrets.token_urlsafe(32)
    return raw, _hash(raw)


async def create(
    session: AsyncSession,
    *,
    purpose: AuthTokenPurpose,
    email: str,
    ttl: timedelta,
    user_id: int | None = None,
    team: str | None = None,
    exercise_id: int | None = None,
) -> str:
    """Persist a new token row and return the raw token (for the link only)."""
    raw, token_hash = generate()
    token = AuthToken(
        token_hash=token_hash,
        purpose=purpose,
        email=email,
        user_id=user_id,
        team=team,
        exercise_id=exercise_id,
        expires_at=datetime.now(UTC) + ttl,
    )
    session.add(token)
    await session.commit()
    return raw


async def consume(
    session: AsyncSession, *, raw: str, purpose: AuthTokenPurpose
) -> AuthToken | None:
    """Validate + burn a token. Returns the row on success, else None.

    Rejects a token that is the wrong purpose, already used, or expired. On success
    it stamps ``used_at`` (single-use) and commits before returning.
    """
    row = (
        await session.exec(select(AuthToken).where(AuthToken.token_hash == _hash(raw)))
    ).first()
    if row is None or row.purpose != purpose or row.used_at is not None:
        return None
    if row.expires_at <= datetime.now(UTC):
        return None
    row.used_at = datetime.now(UTC)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row
