"""Persist per-chat Telegram message ids of incident openings.

Reply threads on resolve and ack buttons must survive app restarts
(stand finding, phase 3.7: in-memory thread map lost the chain).

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-12

"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incident_message",
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incident.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("chat_id", sa.String(length=64), primary_key=True),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("incident_message")
