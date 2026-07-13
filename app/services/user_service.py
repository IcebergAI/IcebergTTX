"""Reads and lookups for `User` (#214).

Nothing owned this model. `auth_service` is 43 lines of pure bcrypt/JWT and takes no
session, so "fetch the user with this email" had been written out longhand eleven times
across the routers, the dependencies, the OIDC service and the bootstrap CLI.

These are deliberately thin. In particular `get_by_email` does **not** normalise its
argument: some callers pass an already-normalised address and some pass raw input, and
quietly lower-casing here would change who can log in. Normalisation stays where it is,
with the caller who knows what the input is.
"""

from sqlmodel import select
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


async def list_all(session: AsyncSession) -> list[User]:
    """Every user — the member-enrolment picker. Deliberately unfiltered; the one other
    unfiltered read (`timeline_service.load_exercise_bundle`, which loads all users just
    to build a name map) is a separate efficiency bug, not a layering one."""
    return list((await session.exec(select(User))).all())
