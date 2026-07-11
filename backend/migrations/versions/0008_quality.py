"""Excursion start vs confirmation values; stability budget.

Field report: with backdated opened_at the single open_value was ambiguous —
it held the value at incident CONFIRMATION (after the delay), not at the
excursion start. Now both are stored:
- open_value    = значение в момент фактического выхода за порог (opened_at)
- confirm_value = значение в момент фиксации инцидента (после задержки)

Historical rows: confirm_value backfilled from open_value (its old
semantics); open_value for old rows remains the confirmation-time value.

stability_budget_h on threshold profiles: суммарно допустимое время вне
диапазона для хранимых препаратов; используется расчётом качества хранения.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-12

"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incident", sa.Column("confirm_value", sa.Float(), nullable=True))
    op.execute("UPDATE incident SET confirm_value = open_value WHERE confirm_value IS NULL")
    op.add_column(
        "threshold_profile", sa.Column("stability_budget_h", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("threshold_profile", "stability_budget_h")
    op.drop_column("incident", "confirm_value")
