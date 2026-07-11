"""Incident reminders and audit log.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-12

"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "incident",
        sa.Column("reminder_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "incident", sa.Column("last_reminder_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.String(length=40), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_column("incident", "last_reminder_at")
    op.drop_column("incident", "reminder_count")
