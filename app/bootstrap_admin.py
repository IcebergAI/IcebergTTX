"""CLI to create or promote the first admin / facilitator account (#65).

Self-registration only ever mints participants (#8), and privileged roles are
assigned out-of-band. This module *is* that out-of-band path: run it once against
a fresh database to create an operator account, or re-run it to promote / re-enable
an existing user (optionally resetting their password). It is idempotent.

    # local (venv active, DATABASE_URL / .env pointing at the DB):
    python -m app.bootstrap_admin --email ops@example.com --name "Ops"

    # Docker Compose:
    docker compose exec app python -m app.bootstrap_admin --email ops@example.com

    # Kubernetes:
    kubectl exec -n iceberg-ttx deploy/iceberg-ttx-app -- \
        python -m app.bootstrap_admin --email ops@example.com

The password is read from --password, else the ADMIN_PASSWORD env var, else an
interactive prompt (never echoed). It is checked against the #13 password policy.
By default the account is a global admin (`is_admin=True`) with the `facilitator`
role; pass --no-admin for a plain facilitator.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from getpass import getpass

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User, UserRole
from app.schemas.auth import EMAIL_RE, validate_password_strength
from app.services import audit_service
from app.services.auth_service import hash_password


async def upsert_admin(
    session: AsyncSession,
    *,
    email: str,
    display_name: str,
    password: str | None,
    role: UserRole = UserRole.facilitator,
    is_admin: bool = True,
    reset_password: bool = False,
) -> tuple[User, bool]:
    """Create the user if absent, else promote them. Pure — no I/O beyond the session.

    Returns ``(user, created)``. A password is required to create a new user and to
    reset an existing one; promoting an existing user without ``reset_password``
    leaves their password untouched. Resetting bumps ``token_valid_after`` so any
    previously-issued tokens are revoked (#14). Raises ``ValueError`` on invalid input.
    """
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError(f"Not a valid email address: {email!r}")

    existing = (await session.exec(select(User).where(User.email == email))).first()

    if existing is None:
        if not password:
            raise ValueError("A password is required to create a new account.")
        validate_password_strength(password)
        user = User(
            email=email,
            display_name=display_name or email.split("@", 1)[0],
            hashed_password=hash_password(password),
            role=role,
            is_admin=is_admin,
            is_active=True,
        )
        session.add(user)
        created = True
    else:
        user = existing
        user.role = role
        user.is_admin = is_admin
        user.is_active = True
        if display_name:
            user.display_name = display_name
        if reset_password:
            if not password:
                raise ValueError("--reset-password requires a password.")
            validate_password_strength(password)
            user.hashed_password = hash_password(password)
            # Revoke all previously-issued tokens (truncate to whole seconds so a
            # freshly-minted token is not self-revoked), mirroring update_me (#14).
            user.token_valid_after = datetime.now(UTC).replace(microsecond=0)
        session.add(user)
        created = False

    await session.commit()
    await session.refresh(user)

    audit_service.emit(
        "admin.bootstrap",
        actor=user,
        target_type="user",
        target_id=user.id,
        reason="created" if created else "promoted",
        severity="warning",
    )
    return user, created


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.bootstrap_admin",
        description="Create or promote an admin / facilitator account.",
    )
    p.add_argument("--email", required=True, help="account email (unique, lowercased)")
    p.add_argument(
        "--name",
        "--display-name",
        dest="display_name",
        default=None,
        help="display name (defaults to the email local-part on create)",
    )
    p.add_argument(
        "--password",
        default=None,
        help="account password; falls back to the ADMIN_PASSWORD env var, else prompts",
    )
    p.add_argument(
        "--role",
        choices=[r.value for r in UserRole],
        default=UserRole.facilitator.value,
        help="role to assign (default: facilitator)",
    )
    admin = p.add_mutually_exclusive_group()
    admin.add_argument(
        "--admin",
        dest="is_admin",
        action="store_true",
        default=True,
        help="grant global-admin (default)",
    )
    admin.add_argument(
        "--no-admin",
        dest="is_admin",
        action="store_false",
        help="plain role, no global-admin flag",
    )
    p.add_argument(
        "--reset-password",
        action="store_true",
        help="reset the password of an existing account (revokes its tokens)",
    )
    return p


def _resolve_password(args: argparse.Namespace, *, needed: bool) -> str | None:
    """Resolve the password from --password, ADMIN_PASSWORD, or an interactive prompt."""
    if args.password:
        return args.password
    env = os.environ.get("ADMIN_PASSWORD")
    if env:
        return env
    if not needed:
        return None
    first = getpass("New password: ")
    second = getpass("Confirm password: ")
    if first != second:
        raise ValueError("Passwords did not match.")
    return first


async def _run(args: argparse.Namespace) -> int:
    from app.database import engine

    async with AsyncSession(engine, expire_on_commit=False) as session:
        existing = (
            await session.exec(select(User).where(User.email == args.email.strip().lower()))
        ).first()
        needed = existing is None or args.reset_password
        try:
            password = _resolve_password(args, needed=needed)
            user, created = await upsert_admin(
                session,
                email=args.email,
                display_name=args.display_name,
                password=password,
                role=UserRole(args.role),
                is_admin=args.is_admin,
                reset_password=args.reset_password,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    verb = "Created" if created else "Updated"
    flags = f"role={user.role.value}" + (", admin" if user.is_admin else "")
    print(f"{verb} {user.email} ({flags}).")
    return 0


def main() -> None:
    args = _build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
