"""Rename sensor.hardware_uid to mqtt_topic.

The field always matched the FULL MQTT topic (e.g. temp/<DS18B20 uid>), not a
uid segment — the old name misled operators during sensor onboarding.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-11

"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_sensor_hardware_uid_active", table_name="sensor")
    op.alter_column("sensor", "hardware_uid", new_column_name="mqtt_topic")
    op.create_index(
        "uq_sensor_mqtt_topic_active",
        "sensor",
        ["mqtt_topic"],
        unique=True,
        postgresql_where=sa.text("status != 'archived'"),
        sqlite_where=sa.text("status != 'archived'"),
    )


def downgrade() -> None:
    op.drop_index("uq_sensor_mqtt_topic_active", table_name="sensor")
    op.alter_column("sensor", "mqtt_topic", new_column_name="hardware_uid")
    op.create_index(
        "uq_sensor_hardware_uid_active",
        "sensor",
        ["hardware_uid"],
        unique=True,
        postgresql_where=sa.text("status != 'archived'"),
        sqlite_where=sa.text("status != 'archived'"),
    )
