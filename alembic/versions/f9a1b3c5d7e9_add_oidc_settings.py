"""add oidc settings

Revision ID: f9a1b3c5d7e9
Revises: e8f0a2b4c6d8
Create Date: 2026-07-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9a1b3c5d7e9"
down_revision: str | None = "e8f0a2b4c6d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oidcsettings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("auth_mode", sa.String(), nullable=False),
        sa.Column("oidc_redirect_base_url", sa.String(), nullable=False),
        sa.Column("oidc_entra_enabled", sa.Boolean(), nullable=False),
        sa.Column("oidc_entra_client_id", sa.String(), nullable=False),
        sa.Column("oidc_entra_tenant_id", sa.String(), nullable=False),
        sa.Column("oidc_entra_scopes", sa.String(), nullable=False),
        sa.Column("oidc_entra_role_claim", sa.String(), nullable=False),
        sa.Column("oidc_entra_role_map", sa.String(), nullable=False),
        sa.Column("oidc_authentik_enabled", sa.Boolean(), nullable=False),
        sa.Column("oidc_authentik_client_id", sa.String(), nullable=False),
        sa.Column("oidc_authentik_base_url", sa.String(), nullable=False),
        sa.Column("oidc_authentik_app_slug", sa.String(), nullable=False),
        sa.Column("oidc_authentik_scopes", sa.String(), nullable=False),
        sa.Column("oidc_authentik_role_claim", sa.String(), nullable=False),
        sa.Column("oidc_authentik_role_map", sa.String(), nullable=False),
        sa.Column("oidc_auth0_enabled", sa.Boolean(), nullable=False),
        sa.Column("oidc_auth0_client_id", sa.String(), nullable=False),
        sa.Column("oidc_auth0_domain", sa.String(), nullable=False),
        sa.Column("oidc_auth0_scopes", sa.String(), nullable=False),
        sa.Column("oidc_auth0_role_claim", sa.String(), nullable=False),
        sa.Column("oidc_auth0_role_map", sa.String(), nullable=False),
        sa.Column("oidc_okta_enabled", sa.Boolean(), nullable=False),
        sa.Column("oidc_okta_client_id", sa.String(), nullable=False),
        sa.Column("oidc_okta_domain", sa.String(), nullable=False),
        sa.Column("oidc_okta_auth_server", sa.String(), nullable=False),
        sa.Column("oidc_okta_scopes", sa.String(), nullable=False),
        sa.Column("oidc_okta_role_claim", sa.String(), nullable=False),
        sa.Column("oidc_okta_role_map", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("oidcsettings")
