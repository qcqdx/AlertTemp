from datetime import UTC, datetime, timedelta

from app.models import Controller, Measurement, Sensor


async def seed(session_factory, values: list[tuple[datetime, float]]) -> int:
    async with session_factory() as session:
        controller = Controller(name="Холодильник")
        session.add(controller)
        await session.flush()
        sensor = Sensor(
            controller_id=controller.id, mqtt_topic="uid-1", alias="Полка", position=1
        )
        session.add(sensor)
        await session.flush()
        for at, value in values:
            session.add(Measurement(sensor_id=sensor.id, time=at, value=value))
        await session.commit()
        return sensor.id


async def test_raw_measurements_ordered(client, session_factory):
    base = datetime.now(UTC) - timedelta(minutes=10)
    sensor_id = await seed(
        session_factory,
        [(base + timedelta(minutes=i), 4.0 + i) for i in range(5)],
    )
    response = await client.get(f"/api/v1/sensors/{sensor_id}/measurements")
    points = response.json()
    assert [p["value"] for p in points] == [4.0, 5.0, 6.0, 7.0, 8.0]


async def test_bucketed_measurements(client, session_factory):
    base = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=1)
    values = [
        (base + timedelta(seconds=10), 4.0),
        (base + timedelta(seconds=40), 6.0),
        (base + timedelta(minutes=1, seconds=10), 8.0),
    ]
    sensor_id = await seed(session_factory, values)

    response = await client.get(
        f"/api/v1/sensors/{sensor_id}/measurements", params={"bucket": "1m"}
    )
    buckets = response.json()
    assert len(buckets) == 2
    assert buckets[0]["avg"] == 5.0
    assert buckets[0]["min"] == 4.0
    assert buckets[0]["max"] == 6.0
    assert buckets[0]["count"] == 2
    assert buckets[1]["avg"] == 8.0


async def test_time_range_filter(client, session_factory):
    base = datetime.now(UTC) - timedelta(hours=3)
    sensor_id = await seed(
        session_factory,
        [(base + timedelta(hours=i), float(i)) for i in range(3)],
    )
    start = (base + timedelta(minutes=30)).isoformat()
    end = (base + timedelta(hours=2, minutes=30)).isoformat()
    response = await client.get(
        f"/api/v1/sensors/{sensor_id}/measurements", params={"start": start, "end": end}
    )
    assert [p["value"] for p in response.json()] == [1.0, 2.0]


async def test_invalid_range_rejected(client, session_factory):
    sensor_id = await seed(session_factory, [])
    now = datetime.now(UTC)
    response = await client.get(
        f"/api/v1/sensors/{sensor_id}/measurements",
        params={"start": now.isoformat(), "end": (now - timedelta(hours=1)).isoformat()},
    )
    assert response.status_code == 422


async def test_controller_latest(client, session_factory):
    now = datetime.now(UTC)
    sensor_id = await seed(session_factory, [(now - timedelta(seconds=30), 4.7)])
    response = await client.get("/api/v1/controllers/1/latest")
    latest = response.json()
    assert len(latest) == 1
    assert latest[0]["sensor_id"] == sensor_id
    assert latest[0]["value"] == 4.7
    assert latest[0]["online"] is True


async def test_controller_latest_offline(client, session_factory):
    now = datetime.now(UTC)
    await seed(session_factory, [(now - timedelta(minutes=30), 4.7)])
    response = await client.get("/api/v1/controllers/1/latest")
    assert response.json()[0]["online"] is False
