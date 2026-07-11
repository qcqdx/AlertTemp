"""TimescaleDB policies for the real ingest rate (~1 msg/s per sensor).

Field data from a production controller (ESP8266 + 3x DS18B20): a burst of
3 PUBLISH every ~second, ~270k rows/day per controller. At 30 controllers
that is ~8M rows/day, so aggregation, compression and retention are
mandatory, not optional:

- continuous aggregate `measurement_1m` (1-minute buckets: sum/min/max/count),
  refreshed every minute — chart queries never scan raw data for long ranges;
- compression of raw chunks older than 7 days (segmented by sensor);
- retention: raw measurements 365 days, 1-minute aggregate 3 years
  (regulatory storage horizon).

The whole migration is a no-op without the TimescaleDB extension.
To adjust policies later use SQL (documented in docs/CONCEPT.md §9):
  SELECT remove_retention_policy('measurement');
  SELECT add_retention_policy('measurement', INTERVAL '...');

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-11

"""

from alembic import op
from sqlalchemy import text

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

RAW_RETENTION = "365 days"
AGG_RETENTION = "1100 days"
COMPRESS_AFTER = "7 days"


def _timescale_available(bind) -> bool:
    if bind.dialect.name != "postgresql":
        return False
    row = bind.execute(
        text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
    ).first()
    return row is not None


def upgrade() -> None:
    bind = op.get_bind()
    if not _timescale_available(bind):
        return

    # CREATE MATERIALIZED VIEW ... WITH (timescaledb.continuous) не работает
    # внутри транзакции — выходим в autocommit
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE MATERIALIZED VIEW IF NOT EXISTS measurement_1m
            WITH (timescaledb.continuous) AS
            SELECT
                time_bucket(INTERVAL '1 minute', time) AS bucket,
                sensor_id,
                sum(value)   AS sum_value,
                min(value)   AS min_value,
                max(value)   AS max_value,
                count(*)     AS sample_count
            FROM measurement
            GROUP BY bucket, sensor_id
            WITH NO DATA;
            """
        )
        op.execute(
            """
            SELECT add_continuous_aggregate_policy(
                'measurement_1m',
                start_offset      => INTERVAL '1 hour',
                end_offset        => INTERVAL '1 minute',
                schedule_interval => INTERVAL '1 minute',
                if_not_exists     => TRUE
            );
            """
        )
        op.execute(
            """
            ALTER TABLE measurement SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'sensor_id',
                timescaledb.compress_orderby   = 'time'
            );
            """
        )
        op.execute(
            f"""
            SELECT add_compression_policy(
                'measurement', INTERVAL '{COMPRESS_AFTER}', if_not_exists => TRUE
            );
            """
        )
        op.execute(
            f"""
            SELECT add_retention_policy(
                'measurement', INTERVAL '{RAW_RETENTION}', if_not_exists => TRUE
            );
            """
        )
        op.execute(
            f"""
            SELECT add_retention_policy(
                'measurement_1m', INTERVAL '{AGG_RETENTION}', if_not_exists => TRUE
            );
            """
        )
        # заполняем агрегат уже накопленными данными
        op.execute("CALL refresh_continuous_aggregate('measurement_1m', NULL, NULL);")


def downgrade() -> None:
    bind = op.get_bind()
    if not _timescale_available(bind):
        return
    with op.get_context().autocommit_block():
        op.execute("SELECT remove_retention_policy('measurement_1m', if_exists => TRUE);")
        op.execute("SELECT remove_retention_policy('measurement', if_exists => TRUE);")
        op.execute("SELECT remove_compression_policy('measurement', if_exists => TRUE);")
        op.execute("DROP MATERIALIZED VIEW IF EXISTS measurement_1m;")
