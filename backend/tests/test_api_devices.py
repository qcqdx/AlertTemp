async def create_controller(client, name="Холодильник №1"):
    response = await client.post("/api/v1/controllers", json={"name": name, "location": "Аптека"})
    assert response.status_code == 201
    return response.json()


async def test_controller_crud(client):
    controller = await create_controller(client)
    assert controller["status"] == "active"

    response = await client.patch(
        f"/api/v1/controllers/{controller['id']}", json={"location": "Процедурная"}
    )
    assert response.status_code == 200
    assert response.json()["location"] == "Процедурная"

    response = await client.get("/api/v1/controllers")
    assert [c["id"] for c in response.json()] == [controller["id"]]


async def test_sensor_binding_flow(client, ingest):
    """Полный путь: датчик появился в эфире → обнаружен → привязан к контроллеру."""
    await ingest.handle_message("28ff6a01/temp", b"4.2")
    await ingest.flush()

    response = await client.get("/api/v1/discovery")
    discovered = response.json()
    assert len(discovered) == 1
    assert discovered[0]["topic"] == "28ff6a01/temp"
    assert discovered[0]["last_value"] == 4.2

    controller = await create_controller(client)
    response = await client.post(
        "/api/v1/sensors",
        json={
            "controller_id": controller["id"],
            "mqtt_topic": "28ff6a01/temp",
            "alias": "Верхняя полка",
            "position": 1,
        },
    )
    assert response.status_code == 201

    # топик помечен как привязанный и исчез из очереди «новых»
    response = await client.get("/api/v1/discovery")
    assert response.json() == []

    # теперь данные с этого топика пишутся как измерения
    await ingest.handle_message("28ff6a01/temp", b"4.5")
    await ingest.flush()
    sensor_id = 1
    response = await client.get(f"/api/v1/sensors/{sensor_id}/measurements")
    points = response.json()
    assert len(points) == 1
    assert points[0]["value"] == 4.5


async def test_max_three_sensors_per_controller(client):
    controller = await create_controller(client)
    for position in (1, 2, 3):
        response = await client.post(
            "/api/v1/sensors",
            json={
                "controller_id": controller["id"],
                "mqtt_topic": f"uid-{position}",
                "alias": f"Датчик {position}",
                "position": position,
            },
        )
        assert response.status_code == 201

    response = await client.post(
        "/api/v1/sensors",
        json={
            "controller_id": controller["id"],
            "mqtt_topic": "uid-4",
            "alias": "Лишний",
            "position": 1,
        },
    )
    assert response.status_code == 409


async def test_position_conflict(client):
    controller = await create_controller(client)
    body = {
        "controller_id": controller["id"],
        "mqtt_topic": "uid-a",
        "alias": "А",
        "position": 2,
    }
    assert (await client.post("/api/v1/sensors", json=body)).status_code == 201
    body["mqtt_topic"] = "uid-b"
    response = await client.post("/api/v1/sensors", json=body)
    assert response.status_code == 409


async def test_duplicate_uid_rejected_until_archived(client):
    controller = await create_controller(client)
    body = {
        "controller_id": controller["id"],
        "mqtt_topic": "uid-x",
        "alias": "X",
        "position": 1,
    }
    response = await client.post("/api/v1/sensors", json=body)
    sensor_id = response.json()["id"]

    body["position"] = 2
    assert (await client.post("/api/v1/sensors", json=body)).status_code == 409

    # после архивирования тот же uid можно привязать заново
    assert (await client.post(f"/api/v1/sensors/{sensor_id}/archive")).status_code == 200
    response = await client.post("/api/v1/sensors", json=body)
    assert response.status_code == 201


async def test_archive_controller_archives_sensors(client):
    controller = await create_controller(client)
    await client.post(
        "/api/v1/sensors",
        json={
            "controller_id": controller["id"],
            "mqtt_topic": "uid-z",
            "alias": "Z",
            "position": 1,
        },
    )
    response = await client.post(f"/api/v1/controllers/{controller['id']}/archive")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "archived"
    assert body["sensors"][0]["status"] == "archived"

    # архивные контроллеры скрыты из основного списка
    response = await client.get("/api/v1/controllers")
    assert response.json() == []


async def test_discovery_ignore_and_restore(client, ingest):
    await ingest.handle_message("neighbours/noise", b"21.0")
    await ingest.flush()

    discovered_id = (await client.get("/api/v1/discovery")).json()[0]["id"]
    assert (await client.post(f"/api/v1/discovery/{discovered_id}/ignore")).status_code == 200
    assert (await client.get("/api/v1/discovery")).json() == []

    assert (await client.post(f"/api/v1/discovery/{discovered_id}/restore")).status_code == 200
    assert len((await client.get("/api/v1/discovery")).json()) == 1


async def test_healthz(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["database"] is True
    assert "ingest" in body


async def test_patch_mqtt_topic_fixes_typo(client, ingest):
    """Опечатка в топике при вводе исправляется через PATCH, без archive+create."""
    controller = await create_controller(client)
    response = await client.post(
        "/api/v1/sensors",
        json={
            "controller_id": controller["id"],
            "mqtt_topic": "temp/28ff6a01-TYPO",
            "alias": "Полка",
            "position": 1,
        },
    )
    sensor_id = response.json()["id"]

    # правильный топик уже виден в очереди обнаружения
    await ingest.handle_message("temp/28ff6a01", b"4.0")
    await ingest.flush()

    response = await client.patch(
        f"/api/v1/sensors/{sensor_id}", json={"mqtt_topic": "temp/28ff6a01"}
    )
    assert response.status_code == 200
    assert response.json()["mqtt_topic"] == "temp/28ff6a01"

    # обнаруженный топик помечен привязанным, данные пишутся в датчик
    assert (await client.get("/api/v1/discovery")).json() == []
    await ingest.handle_message("temp/28ff6a01", b"4.1")
    await ingest.flush()
    points = (await client.get(f"/api/v1/sensors/{sensor_id}/measurements")).json()
    assert [p["value"] for p in points] == [4.1]


async def test_patch_mqtt_topic_conflict(client):
    controller = await create_controller(client)
    for position, topic in ((1, "temp/aaa"), (2, "temp/bbb")):
        await client.post(
            "/api/v1/sensors",
            json={
                "controller_id": controller["id"],
                "mqtt_topic": topic,
                "alias": f"Датчик {position}",
                "position": position,
            },
        )
    response = await client.patch("/api/v1/sensors/2", json={"mqtt_topic": "temp/aaa"})
    assert response.status_code == 409
