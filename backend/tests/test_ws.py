"""WebSocket-раздача событий (этап B.3): контракт сериализации, аутентификация,
живая доставка измерений и инцидентов."""

from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.routes.ws import serialize_event
from app.core.bus import IncidentEvent, MeasurementEvent
from tests.test_stand_findings import lifespan_settings

T0 = datetime(2026, 7, 13, 9, 0, 0, tzinfo=UTC)


# ---------- контракт сериализации (чистые юниты) ----------


def test_serialize_measurement():
    payload = serialize_event(
        MeasurementEvent(sensor_id=5, topic="temp/abc", value=4.5, at=T0)
    )
    assert payload == {
        "type": "measurement",
        "sensor_id": 5,
        "topic": "temp/abc",
        "value": 4.5,
        "at": "2026-07-13T09:00:00Z",  # Z-суффикс, не +00:00 (паттерн §2.8)
    }


def test_serialize_incident():
    payload = serialize_event(
        IncidentEvent(
            kind="opened",
            incident_id=7,
            sensor_id=5,
            sensor_alias="Полка",
            controller_id=2,
            controller_name="Холодильник",
            type="overheat",
            severity="warning",
            value=9.4,
            opened_at=T0,
        )
    )
    assert payload["type"] == "incident"
    assert payload["kind"] == "opened"
    assert payload["incident_id"] == 7
    # тип инцидента под ключом itype, чтобы не конфликтовать с дискриминатором type
    assert payload["itype"] == "overheat"
    assert payload["opened_at"] == "2026-07-13T09:00:00Z"
    assert payload["closed_at"] is None


def test_serialize_unknown_event_is_none():
    assert serialize_event(object()) is None


# ---------- транспорт (TestClient) ----------


async def test_ws_requires_authentication(settings, tmp_path):
    """Аноним не подключается — сокет закрывается кодом 4401 (симметрично P2)."""
    settings = await lifespan_settings(settings, tmp_path)
    from app.main import create_app

    with TestClient(create_app(settings)) as client:
        with pytest.raises(WebSocketDisconnect):  # закрытие до accept
            with client.websocket_connect("/api/v1/ws"):
                pass


async def test_ws_streams_measurement_and_incident(settings, tmp_path):
    """Живой поток: аутентифицированный клиент получает опубликованные в шину
    измерение и переход инцидента."""
    settings = await lifespan_settings(settings, tmp_path)
    settings.admin_password = "admin-pass-123"
    settings.mqtt_enabled = False
    settings.rules_enabled = False  # чтобы движок не потреблял/не публиковал лишнего
    from app.main import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "admin-pass-123"},
        )
        assert login.status_code == 200

        with client.websocket_connect("/api/v1/ws") as ws:
            app.state.bus.publish(
                MeasurementEvent(sensor_id=1, topic="temp/x", value=5.0, at=T0)
            )
            measurement = ws.receive_json()
            assert measurement["type"] == "measurement"
            assert measurement["sensor_id"] == 1
            assert measurement["value"] == 5.0

            app.state.bus.publish(
                IncidentEvent(
                    kind="opened",
                    incident_id=3,
                    sensor_id=1,
                    sensor_alias="Полка",
                    controller_id=1,
                    controller_name="Х",
                    type="overheat",
                    severity="critical",
                    value=12.0,
                    opened_at=T0,
                )
            )
            incident = ws.receive_json()
            assert incident["type"] == "incident"
            assert incident["incident_id"] == 3
            assert incident["severity"] == "critical"
