"""Initial schema: controllers, sensors, measurements, discovery queue.

On PostgreSQL the measurement table is converted to a TimescaleDB hypertable
when the extension is available; otherwise it stays a regular indexed table.

Revision ID: 0001
Revises:
Create Date: 2026-07-08

"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

controller_status = sa.Enum("active", "archived", name="controllerstatus")
sensor_status = sa.Enum("active", "paused", "archived", name="sensorstatus")
discovered_status = sa.Enum("new", "ignored", "bound", name="discoveredtopicstatus")


def upgrade() -> None:
    op.create_table(
        "controller",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("location", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", controller_status, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "sensor",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "controller_id",
            sa.Integer(),
            sa.ForeignKey("controller.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("hardware_uid", sa.String(length=500), nullable=False),
        sa.Column("alias", sa.String(length=200), nullable=False),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("heartbeat_timeout_s", sa.Integer(), nullable=False),
        sa.Column("status", sensor_status, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("controller_id", "position", name="uq_sensor_controller_position"),
    )
    op.create_index("ix_sensor_controller_id", "sensor", ["controller_id"])
    op.create_index(
        "uq_sensor_hardware_uid_active",
        "sensor",
        ["hardware_uid"],
        unique=True,
        postgresql_where=sa.text("status != 'archived'"),
        sqlite_where=sa.text("status != 'archived'"),
    )

    op.create_table(
        "measurement",
        sa.Column(
            "sensor_id",
            sa.Integer(),
            sa.ForeignKey("sensor.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("time", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("value", sa.Float(), nullable=False),
    )
    op.create_index("ix_measurement_time", "measurement", ["time"])

    op.create_table(
        "discovered_topic",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("topic", sa.String(length=500), nullable=False, unique=True),
        sa.Column("status", discovered_status, nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message_count", sa.BigInteger(), nullable=False),
        sa.Column("last_value", sa.Float(), nullable=True),
        sa.Column("error_count", sa.BigInteger(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
    )

    if op.get_bind().dialect.name == "postgresql":
        # TimescaleDB опционален: без него measurement остаётся обычной таблицей
        op.execute(
            """
            DO $$
            BEGIN
                BEGIN
                    CREATE EXTENSION IF NOT EXISTS timescaledb;
                EXCEPTION WHEN OTHERS THEN
                    RAISE NOTICE 'timescaledb extension unavailable, using plain table';
                    RETURN;
                END;
                PERFORM create_hypertable(
                    'measurement', 'time',
                    if_not_exists => TRUE, migrate_data => TRUE
                );
            END
            $$;
            """
        )


def downgrade() -> None:
    op.drop_table("measurement")
    op.drop_table("discovered_topic")
    op.drop_index("uq_sensor_hardware_uid_active", table_name="sensor")
    op.drop_index("ix_sensor_controller_id", table_name="sensor")
    op.drop_table("sensor")
    op.drop_table("controller")
    bind = op.get_bind()
    controller_status.drop(bind, checkfirst=True)
    sensor_status.drop(bind, checkfirst=True)
    discovered_status.drop(bind, checkfirst=True)
