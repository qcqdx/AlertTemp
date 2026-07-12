"""Escalation tiers for recipients, escalated tier tracking on incidents.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-12

"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_recipient",
        sa.Column("tier", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "incident",
        sa.Column("escalated_tier", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("incident", "escalated_tier")
    op.drop_column("notification_recipient", "tier")
