import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.bus import MeasurementEvent
from app.models import (
    Controller,
    DiscoveredTopic,
    DiscoveredTopicStatus,
    Measurement,
    Sensor,
    SensorStatus,
)


async def make_sensor(session_factory, topic="ctrl1/s1", status=SensorStatus.ACTIVE) -> int:
    async with session_factory() as session:
        controller = Controller(name="Холодильник аптеки")
        session.add(controller)
        await session.flush()
        sensor = Sensor(
            controller_id=controller.id,
            mqtt_topic=topic,
            alias="Верхняя полка",
            position=1,
            status=status,
        )
        session.add(sensor)
        await session.commit()
        return sensor.id


async def test_known_topic_is_stored_and_published(ingest, session_factory, bus):
    sensor_id = await make_sensor(session_factory)
    queue = bus.subscribe()

    await ingest.handle_message("ctrl1/s1", b"5.5")
    await ingest.flush()

    async with session_factory() as session:
        rows = (await session.execute(select(Measurement))).scalars().all()
    assert len(rows) == 1
    assert rows[0].sensor_id == sensor_id
    assert rows[0].value == 5.5

    event = queue.get_nowait()
    assert isinstance(event, MeasurementEvent)
    assert event.value == 5.5


async def test_unknown_topic_goes_to_discovery(ingest, session_factory):
    await ingest.handle_message("stranger/topic", b"7.1")
    await ingest.handle_message("stranger/topic", b"7.2")
    await ingest.flush()

    async with session_factory() as session:
        row = (await session.execute(select(DiscoveredTopic))).scalar_one()
    assert row.topic == "stranger/topic"
    assert row.status == DiscoveredTopicStatus.NEW
    assert row.message_count == 2
    assert row.last_value == 7.2

    async with session_factory() as session:
        measurements = (await session.execute(select(Measurement))).scalars().all()
    assert measurements == []


async def test_invalid_payload_on_unknown_topic_recorded_as_error(ingest, session_factory):
    await ingest.handle_message("noise/topic", b"hello")
    await ingest.flush()

    async with session_factory() as session:
        row = (await session.execute(select(DiscoveredTopic))).scalar_one()
    assert row.error_count == 1
    assert "not a number" in row.last_error
    assert ingest.stats.invalid == 1


async def test_invalid_payload_on_known_topic_not_stored(ingest, session_factory):
    await make_sensor(session_factory)
    await ingest.handle_message("ctrl1/s1", b"not-a-number")
    await ingest.flush()

    async with session_factory() as session:
        measurements = (await session.execute(select(Measurement))).scalars().all()
    assert measurements == []
    assert ingest.stats.invalid == 1


async def test_archived_sensor_topic_reappears_in_discovery(ingest, session_factory):
    await make_sensor(session_factory, status=SensorStatus.ARCHIVED)
    await ingest.handle_message("ctrl1/s1", b"5.0")
    await ingest.flush()

    async with session_factory() as session:
        discovered = (await session.execute(select(DiscoveredTopic))).scalar_one()
        measurements = (await session.execute(select(Measurement))).scalars().all()
    assert discovered.topic == "ctrl1/s1"
    assert measurements == []


async def test_paused_sensor_keeps_recording(ingest, session_factory):
    await make_sensor(session_factory, status=SensorStatus.PAUSED)
    await ingest.handle_message("ctrl1/s1", b"5.0")
    await ingest.flush()

    async with session_factory() as session:
        measurements = (await session.execute(select(Measurement))).scalars().all()
    assert len(measurements) == 1


async def test_flush_loop_writes_periodically(ingest, session_factory):
    await make_sensor(session_factory)
    await ingest.start()
    try:
        await ingest.handle_message("ctrl1/s1", b"4.0")
        await asyncio.sleep(0.2)
        async with session_factory() as session:
            measurements = (await session.execute(select(Measurement))).scalars().all()
        assert len(measurements) == 1
    finally:
        await ingest.stop()


async def test_duplicate_timestamps_do_not_fail(ingest, session_factory):
    sensor_id = await make_sensor(session_factory)
    now = datetime.now(UTC)
    ingest._measurement_buffer = [
        {"sensor_id": sensor_id, "time": now, "value": 1.0},
        {"sensor_id": sensor_id, "time": now, "value": 1.0},
    ]
    await ingest.flush()

    async with session_factory() as session:
        measurements = (await session.execute(select(Measurement))).scalars().all()
    assert len(measurements) == 1


async def test_bound_topic_stays_bound_despite_buffered_messages(ingest, session_factory):
    """Гонка: сообщения попали в буфер обнаружения до привязки датчика —
    сброс буфера не должен возвращать топик из bound в new."""
    await ingest.handle_message("ctrl9/s1", b"4.0")  # ещё не привязан
    sensor_id = await make_sensor(session_factory, topic="ctrl9/s1")
    async with session_factory() as session:
        session.add(
            DiscoveredTopic(
                topic="ctrl9/s1",
                status=DiscoveredTopicStatus.BOUND,
                first_seen=datetime.now(UTC),
                last_seen=datetime.now(UTC),
                message_count=1,
                error_count=0,
            )
        )
        await session.commit()

    await ingest.flush()  # буфер с добиндовыми сообщениями

    async with session_factory() as session:
        row = (await session.execute(select(DiscoveredTopic))).scalar_one()
    assert row.status == DiscoveredTopicStatus.BOUND
    assert sensor_id is not None


async def test_archived_topic_flips_back_to_new(ingest, session_factory):
    """После архивирования датчика его топик снова появляется как новый."""
    await make_sensor(session_factory, topic="ctrl8/s1", status=SensorStatus.ARCHIVED)
    async with session_factory() as session:
        session.add(
            DiscoveredTopic(
                topic="ctrl8/s1",
                status=DiscoveredTopicStatus.BOUND,
                first_seen=datetime.now(UTC),
                last_seen=datetime.now(UTC),
                message_count=1,
                error_count=0,
            )
        )
        await session.commit()

    await ingest.handle_message("ctrl8/s1", b"5.0")
    await ingest.flush()

    async with session_factory() as session:
        row = (await session.execute(select(DiscoveredTopic))).scalar_one()
    assert row.status == DiscoveredTopicStatus.NEW
