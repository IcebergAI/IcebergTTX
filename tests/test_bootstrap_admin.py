"""Tests for the operator bootstrap CLI (#65)."""

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.bootstrap_admin import upsert_admin
from app.models.user import User, UserRole
from app.services.auth_service import hash_password, verify_password


async def test_creates_admin_facilitator(session: AsyncSession):
    user, created = await upsert_admin(
        session,
        email="Ops@Example.com",
        display_name="Ops",
        password="a-strong-password",
    )
    assert created is True
    assert user.email == "ops@example.com"  # normalised
    assert user.role is UserRole.facilitator
    assert user.is_admin is True
    assert user.is_active is True
    assert verify_password("a-strong-password", user.hashed_password)


async def test_display_name_defaults_to_local_part(session: AsyncSession):
    user, _ = await upsert_admin(
        session, email="alice@example.com", display_name=None, password="a-strong-password"
    )
    assert user.display_name == "alice"


async def test_create_requires_password(session: AsyncSession):
    with pytest.raises(ValueError, match="password is required"):
        await upsert_admin(session, email="x@example.com", display_name="X", password=None)


async def test_rejects_weak_password(session: AsyncSession):
    with pytest.raises(ValueError, match="at least"):
        await upsert_admin(session, email="x@example.com", display_name="X", password="short")


async def test_rejects_bad_email(session: AsyncSession):
    with pytest.raises(ValueError, match="valid email"):
        await upsert_admin(
            session, email="not-an-email", display_name="X", password="a-strong-password"
        )


async def test_promotes_existing_user_without_touching_password(session: AsyncSession):
    existing = User(
        email="p@example.com",
        display_name="Participant",
        hashed_password=hash_password("original-password"),
        role=UserRole.participant,
        is_admin=False,
    )
    session.add(existing)
    await session.commit()

    user, created = await upsert_admin(
        session, email="p@example.com", display_name=None, password=None
    )
    assert created is False
    assert user.role is UserRole.facilitator
    assert user.is_admin is True
    # Password left untouched when not resetting.
    assert verify_password("original-password", user.hashed_password)
    assert user.token_valid_after is None


async def test_reset_password_revokes_tokens(session: AsyncSession):
    existing = User(
        email="p@example.com",
        display_name="Participant",
        hashed_password=hash_password("original-password"),
        role=UserRole.participant,
    )
    session.add(existing)
    await session.commit()

    user, created = await upsert_admin(
        session,
        email="p@example.com",
        display_name=None,
        password="brand-new-password",
        reset_password=True,
    )
    assert created is False
    assert verify_password("brand-new-password", user.hashed_password)
    assert user.token_valid_after is not None  # tokens revoked (#14)


async def test_reset_password_requires_a_password(session: AsyncSession):
    existing = User(
        email="p@example.com",
        display_name="P",
        hashed_password=hash_password("original-password"),
    )
    session.add(existing)
    await session.commit()

    with pytest.raises(ValueError, match="reset-password requires"):
        await upsert_admin(
            session, email="p@example.com", display_name=None, password=None, reset_password=True
        )


async def test_no_admin_flag_creates_plain_facilitator(session: AsyncSession):
    user, _ = await upsert_admin(
        session,
        email="fac@example.com",
        display_name="Fac",
        password="a-strong-password",
        is_admin=False,
    )
    assert user.role is UserRole.facilitator
    assert user.is_admin is False


async def test_idempotent_rerun(session: AsyncSession):
    await upsert_admin(
        session, email="ops@example.com", display_name="Ops", password="a-strong-password"
    )
    user, created = await upsert_admin(
        session, email="ops@example.com", display_name="Ops", password=None
    )
    assert created is False
    rows = (await session.exec(select(User).where(User.email == "ops@example.com"))).all()
    assert len(rows) == 1
