"""Alerting: threshold profiles, incidents, sensor runtime state.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-11

"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

incident_type = sa.Enum("overheat", "overcool", "offline", name="incidenttype")
incident_severity = sa.Enum("warning", "critical", name="incidentseverity")
incident_status = sa.Enum("open", "acknowledged", "resolved", name="incidentstatus")


def upgrade() -> None:
    op.create_table(
        "threshold_profile",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sensor_id",
            sa.Integer(),
            sa.ForeignKey("sensor.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("warn_low", sa.Float(), nullable=False),
        sa.Column("warn_high", sa.Float(), nullable=False),
        sa.Column("crit_low", sa.Float(), nullable=True),
        sa.Column("crit_high", sa.Float(), nullable=True),
        sa.Column("hysteresis", sa.Float(), nullable=False),
        sa.Column("warn_delay_s", sa.Integer(), nullable=False),
        sa.Column("crit_delay_s", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.String(length=200), nullable=True),
        sa.UniqueConstraint("sensor_id", "version", name="uq_threshold_profile_sensor_version"),
    )
    op.create_index("ix_threshold_profile_sensor_id", "threshold_profile", ["sensor_id"])

    op.create_table(
        "incident",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sensor_id",
            sa.Integer(),
            sa.ForeignKey("sensor.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "controller_id",
            sa.Integer(),
            sa.ForeignKey("controller.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("type", incident_type, nullable=False),
        sa.Column("severity", incident_severity, nullable=False),
        sa.Column("status", incident_status, nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("open_value", sa.Float(), nullable=True),
        sa.Column("peak_value", sa.Float(), nullable=True),
        sa.Column(
            "threshold_profile_id",
            sa.Integer(),
            sa.ForeignKey("threshold_profile.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("acknowledged_by", sa.String(length=200), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
    )
    op.create_index("ix_incident_sensor_id", "incident", ["sensor_id"])
    op.create_index("ix_incident_controller_id", "incident", ["controller_id"])
    op.create_index("ix_incident_status", "incident", ["status"])
    op.create_index("ix_incident_opened_at", "incident", ["opened_at"])

    op.create_table(
        "sensor_runtime_state",
        sa.Column(
            "sensor_id",
            sa.Integer(),
            sa.ForeignKey("sensor.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("sensor_runtime_state")
    op.drop_table("incident")
    op.drop_table("threshold_profile")
    bind = op.get_bind()
    incident_type.drop(bind, checkfirst=True)
    incident_severity.drop(bind, checkfirst=True)
    incident_status.drop(bind, checkfirst=True)
