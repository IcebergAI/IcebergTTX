"""Reads and lookups for `User` (#214).

Nothing owned this model. `auth_service` is 43 lines of pure bcrypt/JWT and takes no
session, so "fetch the user with this email" had been written out longhand eleven times
across the routers, the dependencies, the OIDC service and the bootstrap CLI.

These are deliberately thin. In particular `get_by_email` does **not** normalise its
argument: some callers pass an already-normalised address and some pass raw input, and
quietly lower-casing here would change who can log in. Normalisation stays where it is,
with the caller who knows what the input is.
"""

from collections.abc import Collection

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    """The user with this exact email, or None. Does not normalise — see the module docstring."""
    return (await session.exec(select(User).where(User.email == email))).first()


async def get_by_email_for_update(session: AsyncSession, email: str) -> User | None:
    """As get_by_email, but locks the row so a concurrent bootstrap cannot double-create."""
    return (
        await session.exec(select(User).where(User.email == email).with_for_update())
    ).first()


async def email_exists(session: AsyncSession, email: str) -> bool:
    return await get_by_email(session, email) is not None


async def get_by_ids(session: AsyncSession, ids: Collection[int]) -> list[User]:
    """The users with these ids, in no particular order. Empty ids → no query.

    The name-map read for report/timeline/export projections
    (`timeline_service.load_exercise_bundle`): only the users an exercise actually
    references, not every account on the instance (#245)."""
    if not ids:
        return []
    return list((await session.exec(select(User).where(col(User.id).in_(ids)))).all())


async def list_all(session: AsyncSession) -> list[User]:
    """Every user — the member-enrolment picker. Deliberately unfiltered: the picker
    genuinely needs the whole roster (unlike the report name map, which now scopes to the
    referenced ids via `get_by_ids` — #245)."""
    return list((await session.exec(select(User))).all())
