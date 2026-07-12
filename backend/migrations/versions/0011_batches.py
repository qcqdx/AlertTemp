"""Drug batches: staff mark what was loaded into a fridge and when.

Stability budget and MKT are then computed from the load date instead of
a sliding window (concept §6.3, roadmap stage B.1).

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-12

"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "batch",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "controller_id",
            sa.Integer(),
            sa.ForeignKey("controller.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_batch_controller_id", "batch", ["controller_id"])


def downgrade() -> None:
    op.drop_index("ix_batch_controller_id", table_name="batch")
    op.drop_table("batch")
