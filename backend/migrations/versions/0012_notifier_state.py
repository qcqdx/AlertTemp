"""Persist the notification channel's consecutive-failure counter.

An in-memory counter reset on every restart, masking a degraded channel
(proxy dropped CONNECT, etc.) as healthy after each of the frequent
restarts seen on the stand (roadmap stage B.5).

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-13

"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifier_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_error", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("notifier_state")
